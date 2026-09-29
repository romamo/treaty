"""Static audit of an ``App``: ordered rules over the registry with generated fixes.

Rules see declarations, not behaviour. Runtime guarantees (timeouts, signals,
stream purity) are the conformance kit's job; the last rule points there.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import importlib.util
import inspect
import io
import json
import re
import shlex
import sys
import time
import types
import typing
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._command import Command, DangerLevel, OptionPlacement
from ._deps import Endpoint
from ._env import AUDIT_LOG, UNPREFIXED, app_var
from ._errors import Exit
from ._exit import ExitCodeRegistry, FrameworkCode
from ._out import out_spec
from ._redact import secret_field
from ._scan import (
    clear_caches,
    ctx_calls,
    direct_subprocess_calls,
    reached_functions,
    resolve_name,
    source_tree,
)
from ._types import FlagType, is_dataclass_type, resolve_alias, strip_optional
from ._values import InvalidValue, SchemaVersion

if TYPE_CHECKING:
    from ._app import App

_DESTRUCTIVE_VERBS = ("delete", "remove", "destroy", "drop", "purge", "reset", "rollback", "wipe")
_MUTATING_VERBS = ("create", "update", "set", "add", "apply", "deploy", "write", "push", "start")
_NETWORK_HINTS = re.compile(r"\b(socket|http\.client|urllib|requests|httpx|aiohttp|grpc)\b")


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    ADVICE = "advice"


_RANK = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.ADVICE: 2}
"""The order of the next steps: what fails --strict before what only advises"""


@dataclass(frozen=True, slots=True)
class Finding:
    rule: str
    severity: Severity
    command: str | None
    message: str
    fix: str


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    title: str
    severity: Severity
    check: Callable[[App], Iterator[Finding]]


@dataclass(frozen=True, slots=True)
class RuleResult:
    id: str
    title: str
    severity: str
    passed: bool
    findings: tuple[Finding, ...]


@dataclass(frozen=True, slots=True)
class AuditReport:
    target: str
    rules: tuple[RuleResult, ...]
    next_steps: tuple[Finding, ...]

    @property
    def passed(self) -> int:
        return sum(1 for r in self.rules if r.passed)

    @property
    def failed(self) -> int:
        return len(self.rules) - self.passed


def user_commands(app: App) -> list[Command]:
    return [
        c
        for p, c in sorted(app.commands.items(), key=lambda kv: kv[0].value)
        if p not in app.builtins
    ]


_PRESET_SAMPLES = {
    "alphanumeric_id": "abc123",
    "uuid": "00000000-0000-0000-0000-000000000000",
    "semver": "1.0.0",
    "url": "https://example.com",
}


def _sample(name: str, classified: object, spec: object) -> str:
    """A value of the field's type that its checks accept; a custom pattern keeps a
    placeholder, since no value can be guessed for it"""
    kind = getattr(classified, "flag_type", None)
    enum_values = getattr(classified, "enum_values", ())
    pattern_type = getattr(spec, "pattern_type", None)
    if pattern_type in _PRESET_SAMPLES:
        return _PRESET_SAMPLES[pattern_type]
    if getattr(spec, "pattern", None) is not None:
        return f"<{name}>"
    if kind is FlagType.INTEGER:
        return "1"
    if kind is FlagType.NUMBER:
        return "1.5"
    if kind is FlagType.ENUM and enum_values:
        return str(enum_values[0])
    return name.replace("_", "-")


def _samples(app: App, c: Command) -> list[str]:
    """Each required field as the example the describe fix suggests: a value its checks
    accept where one can be guessed, and a secret from its variable"""
    out: list[str] = []
    for f in c.fields:
        if not f.required:
            continue
        if f.secret:
            out.append(f"--{f.flag}-from-env {app_var(app.name, f.name)}")
            continue
        item = f.classified.item
        value = _sample(f.flag, item if item is not None else f.classified, f.spec)
        out.append(value if f.positional else f"--{f.flag} {value}")
    return out


def _describe(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        if not c.examples:
            example = " ".join([app.name, *c.path.parts, *_samples(app, c)])
            yield Finding(
                "describe",
                Severity.ADVICE,
                c.path.value,
                "no example invocation; agents copy examples verbatim as starting points",
                f'examples=[("What it does", "{example}")]',
            )
        for given in c.examples:
            problem = _example_problem(app, c, given.command)
            if problem is not None:
                yield Finding(
                    "describe",
                    Severity.ERROR,
                    c.path.value,
                    f"the example {given.command!r} does not parse: {problem}",
                    "fix the example, which agents copy verbatim, so it passes --validate-only",
                )


# Framework flags whose value is checked against the world the example runs in, not its
# spelling: a directory or a file. They are dropped, with their value, as are a secret
# field's --<name>-from-env and --<name>-from-file, which name a variable or a file
_WORLD_FLAGS = ("--cwd", "--input-file", "--config")
_WRAPPERS = (("uv", "run"), ("uvx",), ("sudo",))


def _example_problem(app: App, command: Command, example: str) -> str | None:
    """Why an example fails ``--validate-only``, or None when it parses or cannot be judged
    here. Phase 1 only: no handler, no idempotency store, the audit log off, and an empty
    stdin. What depends on the caller's world (a variable, a file, a directory, piped
    input, a shell pipeline or redirect) is left out: only the spelling is checked"""
    lexer = shlex.shlex(example, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    words = list(lexer)
    if any(set(w) <= set("|&;<>()") for w in words):
        return None  # a pipeline, a redirect, or a list of commands: the shell's, not ours
    while words and "=" in words[0] and not words[0].startswith("-"):
        words = words[1:]  # VAR=value before the command
    for wrapper in _WRAPPERS:
        if tuple(words[: len(wrapper)]) == wrapper:
            words = words[len(wrapper) :]
    if not words or words[0].rsplit("/", 1)[-1] != app.name or "-" in words:
        return None
    if {"--help", "-h", "--version"} & set(words):
        return None
    sources = {
        f"--{f.flag}{suffix}"
        for f in command.fields
        if f.secret
        for suffix in ("-from-env", "-from-file")
    }
    argv: list[str] = []
    rest = iter(words[1:])
    for word in rest:
        if word == "--":
            argv.append(word)
            argv.extend(rest)
            break
        name = word.split("=", 1)[0]
        if name in _WORLD_FLAGS or name in sources:
            if "=" not in word:
                next(rest, None)
            continue
        argv.append(word)
    at = argv.index("--") if "--" in argv else len(argv)
    argv[at:at] = ["--validate-only"]
    out, err = io.StringIO(), io.StringIO()
    env = {app_var(app.name, AUDIT_LOG.key): "off"}
    code = app.run(argv, stdout=out, stderr=err, stdin=io.StringIO(""), env=env)
    if code == 0:
        return None
    try:
        error = json.loads(out.getvalue()).get("error") or {}
    except ValueError:
        text = err.getvalue().strip()
        return text.splitlines()[0] if text else f"exit {code}"
    each = [str(e.get("message")) for e in error.get("errors") or [] if isinstance(e, dict)]
    return "; ".join(each) if each else str(error.get("message", f"exit {code}"))


def _danger_level(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        if c.danger_level is not DangerLevel.SAFE:
            continue
        # The leading word only: "settings" is not "set", "dropdown" is not "drop"
        verb = re.split(r"[-_]", c.path.parts[-1])[0]
        if verb in _DESTRUCTIVE_VERBS:
            yield Finding(
                "danger-level",
                Severity.WARNING,
                c.path.value,
                "name suggests a destructive operation but danger_level is safe",
                'danger_level="destructive" and add dry_run: bool = Flag(default=False, ...)',
            )
        elif verb in _MUTATING_VERBS:
            yield Finding(
                "danger-level",
                Severity.WARNING,
                c.path.value,
                "name suggests a mutating operation but danger_level is safe",
                'danger_level="mutating"',
            )


def _exit_codes(app: App) -> Iterator[Finding]:
    # Each finding suggests its own free code, so applying every fix as written registers
    free = app.exits.unused()
    for c in user_commands(app):
        if c.danger_level is DangerLevel.SAFE or c.exit_codes:
            continue
        name = c.path.parts[-1].upper().replace("-", "_") + "_FAILED"
        code = next(free, None)
        fix = (
            f'app.exit_code("{name}", {code}, description="<what failed and what it left>", '
            f'retryable=False, side_effects="none") then exit_codes=["{name}"]'
            if code is not None
            else "every code from 79 to 125 is registered: list the ones this command "
            "exits with in exit_codes=[...]"
        )
        yield Finding(
            "exit-codes",
            Severity.WARNING,
            c.path.value,
            "non-safe command declares no command-specific exit codes; "
            "agents cannot tell failures apart",
            fix,
        )


FRAMEWORK_NAMES = frozenset(c.name for c in FrameworkCode)


def _retryable(app: App) -> Iterator[Finding]:
    exits: ExitCodeRegistry = app.exits
    for c in user_commands(app):
        if c.danger_level is DangerLevel.SAFE:
            continue
        for name in c.exit_codes:
            # A framework code cannot be redeclared, and a RATE_LIMITED or UNAVAILABLE call
            # did nothing (side_effects "none"), so retrying it is safe on any command
            if name.value in FRAMEWORK_NAMES:
                continue
            if exits.by_name(name).retryable:
                yield Finding(
                    "retryable",
                    Severity.WARNING,
                    c.path.value,
                    f"{name} is retryable on a {c.danger_level.value} command; "
                    "only safe if the handler is idempotent",
                    f"confirm {c.path.parts[-1]} is idempotent, "
                    f"or declare {name} with retryable=False",
                )


def _exit_code_suggestion(app: App) -> Iterator[Finding]:
    seen: set[str] = set()
    for c in user_commands(app):
        for name in c.exit_codes:
            entry = app.exits.by_name(name)
            if name.value in seen or not entry.retryable or entry.suggestion is not None:
                continue
            seen.add(name.value)
            yield Finding(
                "exit-code-suggestion",
                Severity.ADVICE,
                c.path.value,
                f"{name} is retryable but names no next step; "
                "agents get only the generic retry suggestion",
                f'app.exit_code("{name}", {entry.code.value}, ..., '
                'suggestion="wait for the upstream to recover, then retry")',
            )


def _typed_output(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        schema = c.output_schema
        if schema.get("type") == "object" and "properties" not in schema:
            yield Finding(
                "typed-output",
                Severity.ADVICE,
                c.path.value,
                "return type is an untyped dict, so output_schema tells agents nothing",
                "return a frozen dataclass; its fields become output_schema automatically",
            )


_OWN_PAGING = frozenset(
    {"page", "offset", "per_page", "page_size", "page_number", "page_token", "next_token", "skip"}
)


def _paginated_list(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        if c.output_schema.get("type") == "array" and not (c.paginated or c.streaming):
            yield Finding(
                "paginated-list",
                Severity.ADVICE,
                c.path.value,
                "returns a list with paginated=False, so it has no default limit, "
                "--limit, --cursor, or meta.pagination (REQ-F-018, REQ-F-019)",
                "drop paginated=False unless the list is small and bounded by design",
            )
        own = sorted(f.flag for f in c.fields if f.name in _OWN_PAGING)
        if c.paginated and own:
            # The framework's --limit and --cursor page what the handler returns; a page flag
            # of its own picks a page before that, which meta.pagination knows nothing of
            yield Finding(
                "paginated-list",
                Severity.WARNING,
                c.path.value,
                f"--{own[0]} pages the list itself, beside the framework's --limit and "
                "--cursor, so meta.pagination describes only the page it was given and says "
                "has_more: false while more remain (REQ-F-019)",
                f"drop --{own[0]}: return a Page with next_cursor from the source's own "
                "continuation, or paginated=False to keep your paging and lose meta.pagination",
            )


def _called(c: Command, fn: Callable[..., object]) -> str:
    """How a finding names where it looked: the handler, or a function it calls"""
    return "handler" if fn is c.handler else f"function {getattr(fn, '__name__', fn)}"


def _network_io(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        if c.has_network_io:
            continue
        # The handler's source is searched as text; a helper or a resource (where a migrated
        # CLI keeps its HTTP client, as ctx.obj was) only for calls into a network module, so
        # a docstring that says "requests" or a urllib.parse.quote does not count
        label = "handler" if _mentions_network(c.handler) else ""
        places: list[tuple[str, Callable[..., object] | type]] = [
            (_called(c, f), f) for f in reached_functions(c.handler)[1:]
        ]
        places += [(f"resource {r.__name__}", r) for r in c.resource_graph]
        for where, code in places:
            if label:
                break
            if _calls_network(code):
                label = where
        if label:
            yield Finding(
                "network-io",
                Severity.WARNING,
                c.path.value,
                f"{label} source mentions a network library "
                "but has_network_io is not declared (heuristic)",
                "has_network_io=True, then call out through ctx.http, which keeps to "
                "the command's --timeout",
            )


def _mentions_network(fn: Callable[..., object]) -> bool:
    try:
        source = inspect.getsource(fn)
    except OSError, TypeError:
        return False  # no source to scan (REPL, exec, C extension); the heuristic cannot apply
    return _NETWORK_HINTS.search(source) is not None


_NETWORK_PACKAGES = (
    "socket",
    "http.client",
    "urllib.request",
    "requests",
    "httpx",
    "aiohttp",
    "grpc",
)


def _absolute(code: Callable[..., object] | type, node: ast.ImportFrom) -> str | None:
    """The module an ``from ... import`` names, a relative one resolved against the
    package ``code`` belongs to"""
    if not node.level:
        return node.module
    package = getattr(sys.modules.get(getattr(code, "__module__", "") or ""), "__package__", None)
    if not package:
        return None
    return importlib.util.resolve_name("." * node.level + (node.module or ""), package)


def _calls_network(code: Callable[..., object] | type) -> bool:
    """A call in ``code``'s source to something a network module defines"""
    tree = source_tree(code)
    if tree is None:
        return False
    # A module imported inside the function, as `import requests as r`, by its local name
    # and `from requests import get as g` by the full name it stands for
    local: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            local.update({a.asname: a.name for a in node.names if a.asname})
        elif isinstance(node, ast.ImportFrom):
            module = _absolute(code, node)
            if module is not None:
                local.update({a.asname or a.name: f"{module}.{a.name}" for a in node.names})
    for node in ast.walk(tree):
        name = _dotted(node.func) if isinstance(node, ast.Call) else None
        if name is None:
            continue
        root, _, rest = name.partition(".")
        if root in local:
            name = f"{local[root]}.{rest}" if rest else local[root]
        target = resolve_name(code, name)
        if target is None and any(name.startswith(f"{p}.") for p in _NETWORK_PACKAGES):
            return True  # a call spelled through a network package imported out of sight
        module = (
            target.__name__
            if isinstance(target, types.ModuleType)
            else getattr(target, "__module__", None)
        )
        if isinstance(module, str) and any(
            module == p or module.startswith(f"{p}.") for p in _NETWORK_PACKAGES
        ):
            return True
    return False


# Calls that open a connection and take timeout=: by name, and the request verbs of
# requests and httpx (not their clients, sessions, or exception classes)
_NETWORK_CALLS = frozenset({"urlopen", "HTTPConnection", "HTTPSConnection"})
_CONNECT = frozenset({"create_connection", "socket.create_connection"})
_NETWORK_MODULES = frozenset({"requests", "httpx"})
_REQUEST_VERBS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "request", "stream"}
)


def _dotted(node: ast.expr) -> str | None:
    """``a.b.c`` for a name or attribute chain, else None"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


def untimed_network_calls(handler: Callable[..., object]) -> list[str]:
    """Network calls in the handler's source without ``timeout=`` (REQ-C-012)"""
    tree = source_tree(handler)
    if tree is None:
        return []  # no source to scan (REPL, exec, C extension)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
        if name is None:
            continue
        parts = name.split(".")
        network = (
            parts[-1] in _NETWORK_CALLS
            or name in _CONNECT
            or (len(parts) == 2 and parts[0] in _NETWORK_MODULES and parts[1] in _REQUEST_VERBS)
        )
        has_timeout = any(k.arg == "timeout" or k.arg is None for k in node.keywords)
        if network and not has_timeout:
            found.append(name)
    return found


def _network_timeout(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        if not c.has_network_io:
            continue
        for name in (n for f in reached_functions(c.handler) for n in untimed_network_calls(f)):
            yield Finding(
                "network-timeout",
                Severity.WARNING,
                c.path.value,
                f"{name}(...) has no timeout=, so it can outlive --timeout",
                f"{name}(..., timeout=ctx.timeout.seconds)",
            )


def _subprocess_declared(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        direct = [
            (f, call) for f in reached_functions(c.handler) for call in direct_subprocess_calls(f)
        ]
        if direct:
            fn, call = direct[0]
            # Outside ctx.run no declaration can describe the child, so this comes first
            yield Finding(
                "subprocess-declared",
                Severity.WARNING,
                c.path.value,
                f"{call.name}() on line {call.line} of the {_called(c, fn)} runs a program "
                "outside ctx.run, so it has no time limit, locale, or argv the manifest can "
                "show, and doctor cannot check the program is installed (REQ-C-019)",
                "ctx.run([...]) with the same argument list, check=False where the code reads "
                "returncode itself, and no capture_output= or text= (ctx.run captures text); "
                'then subprocess=treaty.Subprocess("<binary>", ...) and required_tools=',
            )
            continue
        if c.subprocess is not None:
            continue
        runs = [call for call in ctx_calls(c.handler) if call.method in ("run", "pipeline")]
        if runs:
            yield Finding(
                "subprocess-declared",
                Severity.WARNING,
                c.path.value,
                f"ctx.{runs[0].method}() on line {runs[0].line} of the handler builds its "
                "argument list at run time, so the manifest cannot say which binary gets "
                "which argument (REQ-C-019)",
                'subprocess=treaty.Subprocess("<binary>", user_controlled_args=("<field>",))',
            )


_WRITES = frozenset({"write_text", "write_bytes", "mkdir", "makedirs", "mkdtemp", "mkstemp"})


def disk_writes(handler: Callable[..., object]) -> list[str]:
    """Calls in the handler's source that write to disk (REQ-C-011, heuristic)"""
    tree = source_tree(handler)
    if tree is None:
        return []
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func) or (
            node.func.attr if isinstance(node.func, ast.Attribute) else None
        )
        if name is None:
            continue
        last = name.rpartition(".")[2]
        if last in _WRITES:
            found.append(name)
        elif last == "open":
            mode = node.args[1] if len(node.args) > 1 else None
            mode = next((k.value for k in node.keywords if k.arg == "mode"), mode)
            if isinstance(mode, ast.Constant) and set(str(mode.value)) & set("wax+"):
                found.append(name)
    return found


def _fs_side_effects(app: App) -> Iterator[Finding]:
    # A safe command changes nothing, so what it writes is a cache, log, or temp file; a
    # mutating command's writes may be its effect, such as the project init creates
    for c in user_commands(app):
        if c.danger_level is not DangerLevel.SAFE or c.filesystem_side_effects or c.output_file:
            continue
        writes = disk_writes(c.handler)
        if writes:
            yield Finding(
                "fs-side-effects",
                Severity.ADVICE,
                c.path.value,
                f"{writes[0]}(...) writes to disk, and the safe command declares no "
                "filesystem_side_effects, so agents cannot find or clean up what it leaves "
                "(REQ-C-011, heuristic)",
                'filesystem_side_effects=[treaty.SideEffect("~/.cache/<tool>/", "cache")]',
            )


_CACHE_HINTS = frozenset({"XDG_CACHE_HOME", ".cache"})


def writes_cache(handler: Callable[..., object]) -> bool:
    """The handler writes to disk and names a cache location (REQ-O-018, heuristic)"""
    tree = source_tree(handler)
    if tree is None or not disk_writes(handler):
        return False
    return any(
        isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and any(h in n.value for h in _CACHE_HINTS)
        for n in ast.walk(tree)
    )


def _cache_declared(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        if c.cache is None and writes_cache(c.handler):
            yield Finding(
                "cache-declared",
                Severity.WARNING,
                c.path.value,
                "the handler writes a cache by hand, so --no-cache and --cache-ttl cannot "
                "reach it and agents get stale data they cannot refuse (REQ-O-018, heuristic)",
                "cache=treaty.CachePolicy(ttl_seconds=3600), then ctx.cache.get(key) and "
                "ctx.cache.put(key, data)",
            )


_TREE_CALLS = frozenset(
    {"os.walk", "os.fwalk", "shutil.rmtree", "shutil.copytree", "rmtree", "copytree"}
)


def traversal_calls(handler: Callable[..., object]) -> list[str]:
    """Recursive walks in the handler's source that ``ctx.walk`` would protect from a
    circular symlink and a runaway depth (REQ-F-061, heuristic)"""
    tree = source_tree(handler)
    if tree is None:
        return []
    params = list(inspect.signature(handler).parameters)
    ctx_name = params[1] if len(params) > 1 else None
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        attr = func.attr if isinstance(func, ast.Attribute) else _dotted(func) or ""
        name = _dotted(func) or attr
        if isinstance(func, ast.Attribute) and _dotted(func.value) == ctx_name:
            continue  # ctx.walk itself
        recursive_glob = attr in ("glob", "iglob") and any(
            k.arg == "recursive" and not (isinstance(k.value, ast.Constant) and not k.value.value)
            for k in node.keywords
        )
        if name in _TREE_CALLS or attr in ("rglob", "walk") or recursive_glob:
            found.append(name)
    return found


def _recursive_traversal(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        calls = traversal_calls(c.handler)
        if calls:
            declare = "" if c.recursive_traversal else "recursive_traversal=True, then "
            yield Finding(
                "recursive-traversal",
                Severity.WARNING,
                c.path.value,
                f"{calls[0]}() walks a directory tree that a circular symlink can loop and "
                "no --max-depth bounds (REQ-F-061, REQ-O-040, heuristic)",
                f"{declare}for entry in ctx.walk(root): ...",
            )


def direct_http_calls(handler: Callable[..., object]) -> list[str]:
    """HTTP calls in the handler's source that bypass ``ctx.http`` (REQ-F-037)"""
    tree = source_tree(handler)
    if tree is None:
        return []
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func) or ""
        parts = name.split(".")
        if parts[-1] == "urlopen" or (len(parts) > 1 and parts[0] in _NETWORK_MODULES):
            found.append(name)
    return found


def _http_client(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        if not c.has_network_io:
            continue
        calls = [call for f in reached_functions(c.handler) for call in direct_http_calls(f)]
        if calls:
            yield Finding(
                "http-client",
                Severity.WARNING,
                c.path.value,
                f"{calls[0]}() skips ctx.http, so --proxy, --no-proxy, and the CA bundle "
                "variables do not reach it and a failure has no error.network_context "
                "(REQ-F-036, REQ-F-037)",
                "response = ctx.http.get(url)",
            )


def _declared_commands(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        for problem in app._named_commands(c):
            yield Finding(
                "declared-commands",
                Severity.ERROR,
                c.path.value,
                f"{problem}; the manifest and every run fail until it does (08-D2)",
                "name a registered command, such as " + f"{app.name} cleanup",
            )


def detaches(handler: Callable[..., object]) -> str | None:
    """A call in the handler's source that starts a process outliving it (REQ-C-010)"""
    tree = source_tree(handler)
    if tree is None:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func) or ""
        if name in ("os.fork", "fork", "os.setsid", "os.daemon"):
            return name
        detached = any(
            k.arg in ("start_new_session", "process_group")
            and not (isinstance(k.value, ast.Constant) and not k.value.value)
            for k in node.keywords
        )
        if name.rpartition(".")[2] == "Popen" and detached:
            return name
    return None


def _background_declared(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        name = None if c.background is not None else detaches(c.handler)
        if name is not None:
            yield Finding(
                "background-declared",
                Severity.WARNING,
                c.path.value,
                f"{name}(...) starts a process that outlives the run, undeclared, so agents "
                "cannot find or stop it (REQ-C-010)",
                'ctx.spawn([...]) with background=treaty.Background("<tool> <stop command>", '
                "max_lifetime_seconds=3600)",
            )


def _preserve_locale(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        if c.preserve_locale:
            yield Finding(
                "preserve-locale",
                Severity.ADVICE,
                c.path.value,
                "children run in the user's locale, so their messages and numbers vary by "
                "machine and an agent's parsing breaks on some (REQ-F-066)",
                "remove preserve_locale=True, or say in description why child output is localized",
            )


def _builtin_shadowed(app: App) -> Iterator[Finding]:
    for path in app.shadowed_builtins:
        yield Finding(
            "builtin-shadowed",
            Severity.ADVICE,
            path.value,
            f"an app command or group named {path.value} replaces the built-in {path.value}, "
            "which agents look for on every treaty app (13-D1)",
            f"rename the app's {path.value}, such as {app.name}-{path.value}, to keep the built-in",
        )


def unfixed_checks(check: Callable[..., object]) -> list[int]:
    """Lines of ``Check(...)`` calls in a doctor check's source that may fail (``ok`` is
    not the literal True) and pass no ``fix=`` (REQ-O-026)"""
    tree = source_tree(check)
    if tree is None:
        return []
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _dotted(node.func) not in ("Check", "treaty.Check"):
            continue
        ok = node.args[1] if len(node.args) > 1 else None
        ok = next((k.value for k in node.keywords if k.arg == "ok"), ok)
        passes = isinstance(ok, ast.Constant) and ok.value is True
        has_fix = len(node.args) > 2 or any(k.arg in ("fix", None) for k in node.keywords)
        if not passes and not has_fix:
            found.append(node.lineno)
    return found


def _doctor_fix(app: App) -> Iterator[Finding]:
    for check in app.checks:
        if isinstance(check, Endpoint):
            continue  # endpoint() requires fix=
        name = getattr(check, "__qualname__", type(check).__name__)
        for line in unfixed_checks(check):
            yield Finding(
                "doctor-fix",
                Severity.ADVICE,
                None,
                f"the doctor check {name} builds a Check that may fail without fix= (line "
                f"{line}), so doctor would answer INVALID_OUTPUT instead of a fix (REQ-O-026)",
                'Check(name, ok, fix="<the shell command that resolves it>")',
            )


def _schema_changelog(app: App) -> Iterator[Finding]:
    if app.schema_changelog is None:
        return
    latest = app.changelog[0] if app.changelog else None
    if latest is None or latest.etag != app.manifest()["etag"]:
        since = "has no entries" if latest is None else f"ends at {latest.version}"
        yield Finding(
            "schema-changelog",
            Severity.WARNING,
            None,
            f"the manifest changed since the schema changelog, which {since}, so changelog "
            "does not tell callers what changed (REQ-O-029)",
            "uv run treaty changelog-add module:app",
        )


def _required_tools(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        seen: set[str] = set()
        for call in ctx_calls(c.handler):
            binary = None if not call.argv else call.argv[0].literal
            if binary is None or binary in c.required_tools or binary in seen:
                continue
            seen.add(binary)
            yield Finding(
                "required-tools",
                Severity.ADVICE,
                c.path.value,
                f"runs {binary!r} (line {call.line} of the handler), which required_tools does "
                "not list, so doctor cannot check it is installed (REQ-C-018)",
                f'required_tools={{"{binary}": "<minimum version>"}}',
            )


# Whole words, plus the common fused forms; "profile" and "tempo" are not paths
_PATH_NAME_HINTS = re.compile(
    r"(^|_)(path|dir|directory|file|folder|filepath|dirpath|filename|dirname)($|_)"
    r"|^(out|in|src|dst|log|config|work)(file|dir|path)$"
)


def _path_typed(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        for f in c.fields:
            if f.flag_type is FlagType.STRING and not f.path and _PATH_NAME_HINTS.search(f.name):
                yield Finding(
                    "path-typed",
                    Severity.WARNING,
                    c.path.value,
                    f"{f.name} looks like a path but is a str; traversal and encoded bytes "
                    "are not rejected (heuristic)",
                    f"{f.name}: Path = ... so the framework rejects '..', %XX and null bytes",
                )
        for where, out, hint in output_fields(c.output_type):
            if resolve_alias(hint) is str and _PATH_NAME_HINTS.search(out.name):
                yield Finding(
                    "path-typed",
                    Severity.WARNING,
                    c.path.value,
                    f"output field {where} looks like a path but is a str, so a relative "
                    "path reaches the caller as given (heuristic, REQ-F-040)",
                    f"{out.name}: Path, which treaty writes as an absolute path",
                )


_ID_NAMES = re.compile(r"(^|_)(id|slug|ref)$")


def _id_pattern(app: App) -> Iterator[Finding]:
    """REQ-C-020's registration warning (04-D1): an identifier field with nothing that
    rejects a hallucinated value such as ``prod/../evil``"""
    for c in user_commands(app):
        for f in c.fields:
            text = f.classified.item or f.classified
            if (
                text.flag_type is FlagType.STRING
                and not text.path
                and text.scalar is None
                and not f.secret
                and f.spec.pattern is None
                and f.spec.pattern_type is None
                and _ID_NAMES.search(f.name)
            ):
                marker = "Arg" if f.positional else "Flag"
                yield Finding(
                    "id-pattern",
                    Severity.WARNING,
                    c.path.value,
                    f"{f.name} looks like a resource identifier but declares no pattern, so "
                    "a value with / . ? # or % reaches the handler (heuristic, REQ-C-020)",
                    f'{f.name}: str = {marker}(..., pattern_type="alphanumeric_id"), or '
                    'pattern="^...$" for another shape',
                )


_MULTILINE_NAMES = re.compile(r"(^|_)(message|body|description|text)($|_)")


def _multiline_flag(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        for f in c.fields:
            if (
                f.flag_type is FlagType.STRING
                and not f.path
                and not f.secret
                and not f.spec.multiline
                and _MULTILINE_NAMES.search(f.name)
            ):
                yield Finding(
                    "multiline-flag",
                    Severity.ADVICE,
                    c.path.value,
                    f"{f.name} looks like free text, but newlines in it are refused (heuristic)",
                    f"{f.name}: str = {'Arg' if f.positional else 'Flag'}(..., multiline=True) "
                    "if it may span lines",
                )


def _raw_payload(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        if c.danger_level is DangerLevel.SAFE or c.supports_raw_payload:
            continue
        if len([f for f in c.fields if f.flag_type is not FlagType.BOOLEAN]) > 3:
            yield Finding(
                "raw-payload",
                Severity.ADVICE,
                c.path.value,
                "mutating command with many fields; "
                "agents pass API-shaped JSON with less translation loss",
                "supports_raw_payload=True",
            )


def _cleanup(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        if c.has_network_io and c.cleanup is None:
            yield Finding(
                "cleanup",
                Severity.ADVICE,
                c.path.value,
                "network command has no cleanup hook for when the run ends, by any exit",
                "cleanup=release_resources where the function "
                "closes connections and removes temp files, and is safe to call twice",
            )


# Methods that show a resource holds something the run must give back (REQ-C-017)
_HOLDS = ("close", "__exit__", "terminate", "unlink")


def _resource_release(app: App) -> Iterator[Finding]:
    seen: set[type] = set()
    for c in user_commands(app):
        for cls, spec in c.resource_graph.items():
            held = next((m for m in _HOLDS if hasattr(cls, m)), None)
            if cls in seen or spec.releases or held is None:
                continue
            seen.add(cls)
            call = "self.__exit__(None, None, None)" if held == "__exit__" else f"self.{held}()"
            yield Finding(
                "resource-release",
                Severity.WARNING,
                c.path.value,
                f"resource {cls.__qualname__} has {held}() but no release(); treaty releases "
                "a resource when the run ends, by any exit, only through release()",
                f"def release(self) -> None: {call}  # in class {cls.__qualname__}",
            )


# A whole scope word: "admin:org", "root", "*"; "administrators:read" is not one
_BROAD_SCOPE = re.compile(r"(^|[:._/-])(admin|owner|root|\*)($|[:._/-])", re.IGNORECASE)
_LOGIN_NAMES = frozenset({"login", "signin", "sign-in", "authenticate"})


def _broad_scope(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        for scope in c.required_scopes:
            if _BROAD_SCOPE.search(scope.value) and scope.value not in c.description:
                yield Finding(
                    "broad-scope",
                    Severity.WARNING,
                    c.path.value,
                    f"requires the blanket scope {scope.value!r}; required_scopes lists only "
                    "what the command uses (REQ-C-029)",
                    f"a narrower scope, or name {scope.value!r} and why in the description",
                )


def _auth_declared(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        if c.auth is None and c.path.parts[-1] in _LOGIN_NAMES:
            yield Finding(
                "auth-declared",
                Severity.WARNING,
                c.path.value,
                "looks like a login command but declares no auth=; agents cannot tell whether "
                "it works without a browser (REQ-C-021)",
                'auth="browser" (or "device" for a device code flow)',
            )


def _refresh_declared(app: App) -> Iterator[Finding]:
    if app.credentials is None or any(c.refreshes_auth for c in app.commands.values()):
        return
    logins = [c for c in user_commands(app) if c.auth is not None]
    target = logins[0].path.value if logins else None
    yield Finding(
        "refresh-declared",
        Severity.ADVICE,
        target,
        "no command declares refreshes_auth=True, so CREDENTIALS_EXPIRED carries no "
        "refresh_command (REQ-F-063)",
        "refreshes_auth=True on the command that renews the credential"
        + (f", such as {target}" if target else ""),
    )


_ASYNC_VERBS = frozenset({"start", "submit", "enqueue", "launch", "trigger"})


def _async_job(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        if not c.async_job and c.path.parts[-1] in _ASYNC_VERBS:
            yield Finding(
                "async-job",
                Severity.ADVICE,
                c.path.value,
                "name suggests work that goes on after the command returns; agents cannot "
                "poll it without a job descriptor (REQ-C-022)",
                "async_job=True returning treaty.Job, with App(jobs=...) to answer job status",
            )


def _config_write_scope(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        if c.config_write_scope is not None or c.danger_level is DangerLevel.SAFE:
            continue
        if "config" in c.path.parts:
            yield Finding(
                "config-write-scope",
                Severity.WARNING,
                c.path.value,
                "looks like a config write but declares no config_write_scope; agents cannot "
                "tell whether it changes a shared user file (REQ-C-025)",
                'config_write_scope="global" to write the user file, <config home>/<app>/'
                'config.toml, which every call then passes --global for, or "local" for '
                "./.<app>.toml in the working directory; then "
                "write through ctx.write_config",
            )


def _self_attrs(node: ast.AST) -> list[str]:
    """``self.<name>`` reads under ``node``, in source order, each once"""
    names = [
        n.attr
        for n in ast.walk(node)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self"
    ]
    return list(dict.fromkeys(names))


def cross_field_checks(args_type: type) -> list[tuple[str, str, object]]:
    """``(first, second, compared)`` for each ``if`` in the args ``__post_init__`` that
    reads two fields and raises: ``compared`` is the constant ``first`` is compared
    with (``self.first == "csv"``), or ``...`` when there is none"""
    post_init = args_type.__dict__.get("__post_init__")
    tree = None if post_init is None else source_tree(post_init)
    if tree is None:
        return []
    found: list[tuple[str, str, object]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not any(
            isinstance(n, ast.Raise) for b in node.body for n in ast.walk(b)
        ):
            continue
        attrs = _self_attrs(node.test)
        if len(attrs) < 2:
            continue
        compared: object = ...
        for cmp in ast.walk(node.test):
            if (
                isinstance(cmp, ast.Compare)
                and _self_attrs(cmp.left) == [attrs[0]]
                and isinstance(cmp.ops[0], ast.Eq)
                and isinstance(cmp.comparators[0], ast.Constant)
            ):
                compared = cmp.comparators[0].value
        found.append((attrs[0], attrs[1], compared))
    return found


def _conditional_rules(app: App) -> Iterator[Finding]:
    """REQ-C-026: a cross-field check only __post_init__ knows is invisible to --schema"""
    for c in user_commands(app):
        declared = {r.field.name for r in c.requires}
        names = {f.name: f.flag for f in c.fields}
        for first, second, compared in cross_field_checks(c.args_type):
            if first in declared or first not in names or second not in names:
                continue
            if compared is ...:
                fix = (
                    f'requires=[treaty.Excludes("{names[first]}", prohibited=("{names[second]}",))]'
                )
            else:
                fix = (
                    f'requires=[treaty.RequiredWhen("{names[first]}", {compared!r}, '
                    f'then=("{names[second]}",))]'
                )
            yield Finding(
                "conditional-rules",
                Severity.ADVICE,
                c.path.value,
                f"__post_init__ checks {first} against {second}, which --schema cannot show, "
                "so an agent learns the rule from a failing call (heuristic, REQ-C-026)",
                f"{fix}, if that is the rule; then drop the check from __post_init__",
            )


def _option_placement(app: App) -> Iterator[Finding]:
    """REQ-C-027: a variadic positional passed on to a child is where an agent's
    ``--child-flag`` goes; under ``any`` treaty parses it instead"""
    for c in user_commands(app):
        if c.option_placement is OptionPlacement.STRICT:
            continue
        variadic = {f.name for f in c.fields if f.positional and f.flag_type is FlagType.ARRAY}
        for call in ctx_calls(c.handler):
            forwarded = [f for f in call.fields if f in variadic]
            if call.method in ("run", "pipeline") and forwarded:
                yield Finding(
                    "option-placement",
                    Severity.WARNING,
                    c.path.value,
                    f"ctx.{call.method}() on line {call.line} forwards {forwarded[0]} to a "
                    "child, but options among those values are parsed as the command's own",
                    'option_placement="strict", so options end at the first positional and '
                    "the rest reach the child verbatim",
                )
                break


# REQ-F-021: names of values that differ on every call; created_at is a fact of the record
_VOLATILE_NAMES = re.compile(
    r"^((fetched|generated|retrieved|requested|queried|rendered)_at|timestamp|now|"
    r"request_id|trace_id|duration(_ms|_s)?|elapsed(_ms|_s)?)$"
)


def _volatile_fields(schema: object, where: str = "") -> Iterator[tuple[str, bool]]:
    """Every output field that looks volatile, with whether its name says so"""
    if not isinstance(schema, dict):
        return
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, prop in properties.items():
            path = f"{where}.{name}" if where else name
            if isinstance(prop, dict) and prop.get("x-volatile"):
                continue  # declared: --stable-output leaves it out
            if _VOLATILE_NAMES.match(name):
                yield path, True
            elif isinstance(prop, dict) and prop.get("format") == "date-time":
                yield path, False
            yield from _volatile_fields(prop, path)
    for key in ("items", "anyOf", "oneOf"):
        nested = schema.get(key)
        for part in nested if isinstance(nested, list) else [nested]:
            yield from _volatile_fields(part, f"{where}[]" if key == "items" else where)


def _volatile_data(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        for field_path, named in _volatile_fields(c.output_schema):
            if named:
                yield Finding(
                    "volatile-data",
                    Severity.WARNING,
                    c.path.value,
                    f"output field {field_path} changes on every call, so two identical calls "
                    "no longer return identical data (REQ-F-021)",
                    f"{field_path.rpartition('.')[2]}: ... = treaty.Out(volatile=True), which "
                    "--stable-output leaves out; or drop it, since meta carries timestamp, "
                    "request_id, and duration_ms",
                )
            else:
                yield Finding(
                    "volatile-data",
                    Severity.ADVICE,
                    c.path.value,
                    f"output field {field_path} is a datetime; if it is when the response was "
                    "made rather than a fact of the record, it breaks caching (heuristic)",
                    f"if it is the time of the call, drop {field_path} (meta.timestamp has it) "
                    "or declare it treaty.Out(volatile=True)",
                )


def output_fields(
    tp: object, where: str = "", seen: frozenset[type] = frozenset()
) -> Iterator[tuple[str, dataclasses.Field[Any], object]]:
    """Every field of every dataclass in an output type, with its path and annotation"""
    base, _ = strip_optional(resolve_alias(tp))
    for arg in typing.get_args(base):
        if arg is not Ellipsis:
            inner = f"{where}[]" if typing.get_origin(base) in (list, tuple) else where
            yield from output_fields(arg, inner, seen)
    if not is_dataclass_type(base) or base in seen:
        return
    assert isinstance(base, type)
    hints = typing.get_type_hints(base)
    for f in dataclasses.fields(base):
        path = f"{where}.{f.name}" if where else f.name
        yield path, f, hints[f.name]
        yield from output_fields(hints[f.name], path, seen | {base})


def _object_items(tp: object) -> type | None:
    """The dataclass items of a ``list[T]`` or ``tuple[T, ...]``, which treaty sorts"""
    base, _ = strip_optional(resolve_alias(tp))
    args = typing.get_args(base)
    origin = typing.get_origin(base)
    item = None
    if origin is list and args:
        item = resolve_alias(args[0])
    elif origin is tuple and len(args) == 2 and args[1] is Ellipsis:
        item = resolve_alias(args[0])
    return item if isinstance(item, type) and is_dataclass_type(item) else None


def _id_like(cls: type) -> str:
    """The field an array of ``cls`` is most likely keyed by"""
    names = [f.name for f in dataclasses.fields(cls)]
    for pattern in (r"^id$", r"_id$", r"^(key|name|slug)$"):
        found = next((n for n in names if re.search(pattern, n)), None)
        if found is not None:
            return found
    return names[0] if names else "id"


_ID_SUFFIXES = ("_id", "uuid", "slug")


def _id_field(app: App) -> Iterator[Finding]:
    """REQ-O-005: an output whose one identifier is not named ``id`` declares it, so
    ``--format id`` can pipe it"""
    for c in user_commands(app):
        if c.id_field is not None or c.batch:
            continue
        schema = c.output_schema
        item = schema.get("items", {}) if schema.get("type") == "array" else schema
        properties = item.get("properties") or {}
        named = [
            name
            for name, prop in properties.items()
            if name.endswith(_ID_SUFFIXES) and prop.get("type") in ("string", "integer")
        ]
        if len(named) == 1:
            yield Finding(
                "id-field",
                Severity.ADVICE,
                c.path.value,
                f"output field {named[0]} looks like the primary id, but --format id cannot "
                "write it until it is declared",
                f'id_field="{named[0]}"',
            )


def _stable_order(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        item = _object_items(c.output_type)
        if item is not None and c.order.sort_key is None and not c.order.ordered:
            yield Finding(
                "stable-order",
                Severity.ADVICE,
                c.path.value,
                "returns an array of objects with no declared order, so treaty sorts it by "
                "each item's JSON text (REQ-F-020)",
                f'sort_key="{_id_like(item)}", or ordered=True if the order is a ranking',
            )
        for where, f, hint in output_fields(c.output_type):
            spec = out_spec(f)
            item = _object_items(hint)
            if item is not None and spec.sort_key is None and not spec.ordered:
                yield Finding(
                    "stable-order",
                    Severity.ADVICE,
                    c.path.value,
                    f"output field {where} is an array of objects with no declared order, so "
                    "treaty sorts it by each item's JSON text (REQ-F-020)",
                    f'{f.name}: ... = treaty.Out(sort_key="{_id_like(item)}"), or '
                    "treaty.Out(ordered=True) if the order is a ranking",
                )


def _external_data(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        if c.external is not None or any(
            out_spec(f).external for _, f, _ in output_fields(c.output_type)
        ):
            continue
        # A file read is too often the tool's own state to flag; a child's output and a
        # network response come from outside by definition
        if c.has_network_io:
            source = "has_network_io=True"
        elif any(call.method in ("run", "pipeline") for call in ctx_calls(c.handler)):
            source = "runs a child process through ctx.run"
        else:
            continue
        yield Finding(
            "external-data",
            Severity.WARNING,
            c.path.value,
            f"{source} but declares no external content, so what it returns reaches the "
            "agent without _trusted: false (REQ-F-035)",
            "external=True on the command, or treaty.Out(external=True) on the field that "
            "holds the content; external=False when it returns only values it computed",
        )


def _textual(tp: object) -> bool:
    base, _ = strip_optional(resolve_alias(tp))
    if base is str:
        return True
    args = typing.get_args(base)
    return typing.get_origin(base) in (list, tuple) and bool(args) and _textual(args[0])


def _high_entropy(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        for where, f, hint in output_fields(c.output_type):
            if out_spec(f).high_entropy is None and secret_field(f.name) and _textual(hint):
                yield Finding(
                    "high-entropy",
                    Severity.ADVICE,
                    c.path.value,
                    f"output field {where} is masked as [KEY: ...] unless --unmask, because "
                    "its name says credential (REQ-F-058)",
                    f"{f.name}: str = treaty.Out(high_entropy=False) if it is not a secret; "
                    "treaty.Out(high_entropy=True) to keep it masked whatever its name",
                )


_BASE64_CALLS = frozenset(
    {"base64.b64encode", "b64encode", "base64.standard_b64encode", "base64.encodebytes"}
)


def _binary_output(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        tree = source_tree(c.handler)
        if tree is None:
            continue
        if any(
            isinstance(n, ast.Call) and _dotted(n.func) in _BASE64_CALLS for n in ast.walk(tree)
        ):
            yield Finding(
                "binary-output",
                Severity.ADVICE,
                c.path.value,
                "handler base64-encodes a value itself, so callers get a bare string with "
                "no size or content type (REQ-F-017)",
                "return the bytes, or treaty.Binary(data, content_type=...); treaty writes "
                "the base64 wrapper with size_bytes",
            )


def _len_limits(tree: ast.AST) -> Iterator[str]:
    """``len(args.x)`` compared with a number: a size limit checked by hand"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        sides = [node.left, *node.comparators]
        if not any(isinstance(s, ast.Constant) and isinstance(s.value, int) for s in sides):
            continue
        for side in sides:
            if (
                isinstance(side, ast.Call)
                and _dotted(side.func) == "len"
                and len(side.args) == 1
                and isinstance(side.args[0], ast.Attribute)
                and _dotted(side.args[0]) == f"args.{side.args[0].attr}"
            ):
                yield side.args[0].attr


def _field_limits(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        tree = source_tree(c.handler)
        if tree is None:
            continue
        for name in dict.fromkeys(_len_limits(tree)):
            yield Finding(
                "field-limits",
                Severity.ADVICE,
                c.path.value,
                f"handler checks len(args.{name}) itself, after phase 1, so an agent learns "
                "the limit only by failing (REQ-F-064)",
                f"{name}: str = Flag(..., max_bytes=N), which the manifest lists and phase 1 "
                "enforces with FIELD_TOO_LARGE",
            )
        cuts = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Subscript)
            and isinstance(n.value, ast.Attribute)
            and isinstance(n.slice, ast.Slice)
            and n.slice.lower is None
            and n.slice.step is None
            and isinstance(n.slice.upper, ast.Constant)
            and isinstance(n.slice.upper.value, int)
        ]
        if cuts:
            yield Finding(
                "field-limits",
                Severity.ADVICE,
                c.path.value,
                "handler cuts a value to a fixed length, and nothing tells the caller it is "
                "incomplete (heuristic, REQ-F-064)",
                'ctx.truncated(value, field="<name>", original_length=len(full)), which '
                "adds the marker, a FIELD_TRUNCATED warning, and meta.truncated",
            )


LOCK_FILE = Path("treaty-schema.lock")
"""The output contracts ``treaty schema-lock`` recorded, which ``schema-version`` diffs"""


def schema_lock(app: App) -> dict[str, object]:
    """Each command's schema version and output schema, as ``treaty schema-lock`` writes them"""
    return {
        "commands": {
            c.path.value: {
                "schema_version": c.schema_version.value,
                "output_schema": c.output_schema,
            }
            for c in user_commands(app)
        }
    }


class Change(StrEnum):
    NONE = "none"
    ADDITIVE = "additive"
    BREAKING = "breaking"


_NOTES = ("description", "title", "examples")
_BRANCHES = ("anyOf", "oneOf", "allOf")


def schema_change(old: object, new: object) -> Change:
    """How an output schema changed for a reader: a removed or retyped field breaks it,
    an added field does not; a field that became optional breaks it too"""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return Change.NONE if old == new else Change.BREAKING
    shape_keys = ("properties", "required", "items", *_BRANCHES, *_NOTES)
    if {k: v for k, v in old.items() if k not in shape_keys} != {
        k: v for k, v in new.items() if k not in shape_keys
    }:
        return Change.BREAKING
    changes: list[Change] = []
    if "items" in old or "items" in new:
        changes.append(schema_change(old.get("items"), new.get("items")))
    for key in _BRANCHES:
        # A union, such as a Batch result or an optional object, changes branch by branch;
        # a branch added or removed is a shape the reader did not know
        old_branches, new_branches = old.get(key), new.get(key)
        if old_branches is None and new_branches is None:
            continue
        if (
            not isinstance(old_branches, list)
            or not isinstance(new_branches, list)
            or len(old_branches) != len(new_branches)
        ):
            return Change.BREAKING
        changes += [schema_change(o, n) for o, n in zip(old_branches, new_branches, strict=True)]
    old_props, new_props = old.get("properties", {}), new.get("properties", {})
    if not isinstance(old_props, dict) or not isinstance(new_props, dict):
        return Change.BREAKING
    if set(old_props) - set(new_props):
        return Change.BREAKING
    old_required, new_required = set(old.get("required", ())), set(new.get("required", ()))
    if old_required - new_required:
        return Change.BREAKING
    changes += [schema_change(old_props[k], new_props[k]) for k in old_props]
    if set(new_props) - set(old_props) or new_required - old_required:
        changes.append(Change.ADDITIVE)
    if Change.BREAKING in changes:
        return Change.BREAKING
    return Change.ADDITIVE if Change.ADDITIVE in changes else Change.NONE


def _read_lock(root: Path) -> dict[str, dict[str, object]] | None:
    lock = root / LOCK_FILE
    if not lock.is_file():
        return None
    try:
        commands = json.loads(lock.read_text(encoding="utf-8"))["commands"]
        if not isinstance(commands, dict):
            raise TypeError("commands is not an object")
        return commands
    except (ValueError, KeyError, TypeError) as exc:
        raise Exit.PRECONDITION(
            f"{LOCK_FILE} is not a schema lock: {exc}",
            context={"lock": str(lock)},
            fix_required="delete it and run treaty schema-lock module:app again",
        ) from None


def _schema_version(app: App, root: Path = Path(".")) -> Iterator[Finding]:
    locked = _read_lock(root)
    if locked is None:
        return
    for c in user_commands(app):
        entry = locked.get(c.path.value)
        if entry is None:
            continue
        try:
            was = SchemaVersion(str(entry["schema_version"]))
        except (KeyError, InvalidValue) as exc:
            raise Exit.PRECONDITION(
                f"{LOCK_FILE} has no valid schema_version for {c.path}: {exc}",
                context={"lock": str(LOCK_FILE), "command": c.path.value},
                fix_required="run treaty schema-lock module:app again",
            ) from None
        change = schema_change(entry.get("output_schema"), c.output_schema)
        now = c.schema_version
        if change is Change.BREAKING and now.major <= was.major:
            yield Finding(
                "schema-version",
                Severity.ERROR,
                c.path.value,
                f"output schema changed in a breaking way since {LOCK_FILE}, but schema_version "
                f"is {now}, not a new major (REQ-F-022)",
                f'schema_version="{was.major + 1}.0", then treaty schema-lock to record it',
            )
        elif change is Change.ADDITIVE and now.key <= was.key:
            yield Finding(
                "schema-version",
                Severity.WARNING,
                c.path.value,
                f"output schema gained fields since {LOCK_FILE}, but schema_version is still "
                f"{now} (REQ-F-022)",
                f'schema_version="{was.major}.{was.minor + 1}", '
                "then treaty schema-lock to record it",
            )


_CWD_CALLS = frozenset({"Path.cwd", "pathlib.Path.cwd", "os.getcwd", "getcwd"})
_ROOT_MARKERS = (".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod", ".hg")


def walks_up_from_cwd(handler: Callable[..., object]) -> str | None:
    """The marker a handler looks for walking up from the cwd itself, ``.git`` when it
    names none; None when it does not walk up"""
    tree = source_tree(handler)
    if tree is None:
        return None
    nodes = list(ast.walk(tree))
    cwd = any(isinstance(n, ast.Call) and _dotted(n.func) in _CWD_CALLS for n in nodes)
    up = any(isinstance(n, ast.Attribute) and n.attr in ("parent", "parents") for n in nodes)
    if not (cwd and up):
        return None
    named = {n.value for n in nodes if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    return next((m for m in _ROOT_MARKERS if m in named), ".git")


_CHDIR_CALLS = frozenset(
    {"os.chdir", "chdir", "os.fchdir", "fchdir", "contextlib.chdir", "os.chroot"}
)


def changes_cwd(fn: Callable[..., object]) -> str | None:
    """A call in ``fn``'s source that changes the process working directory (REQ-F-041)"""
    tree = source_tree(fn)
    if tree is None:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (name := _dotted(node.func)) in _CHDIR_CALLS:
            return name
    return None


def _no_chdir(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        where = [("the handler", c.handler)] + [
            (f"{cls.__qualname__}.acquire", spec.acquire) for cls, spec in c.resource_graph.items()
        ]
        for label, fn in where:
            name = changes_cwd(fn)
            if name is not None:
                yield Finding(
                    "no-chdir",
                    Severity.WARNING,
                    c.path.value,
                    f"{label} calls {name}(), which changes the working directory of the whole "
                    "process; treaty changes it back with a CWD_CHANGED warning (REQ-F-041)",
                    "build paths from ctx.cwd, or pass cwd= to ctx.run",
                )
                break


_PRINT_CALLS = frozenset({"print", "sys.stderr.write", "sys.stdout.write", "sys.stderr.writelines"})


def prints(fn: Callable[..., object]) -> str | None:
    """A call in ``fn``'s source that writes diagnostics around ``ctx.log`` (REQ-F-038)"""
    tree = source_tree(fn)
    if tree is None:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (name := _dotted(node.func)) in _PRINT_CALLS:
            return name
    return None


def _log_not_print(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        name = prints(c.handler)
        if name is not None:
            yield Finding(
                "log-not-print",
                Severity.WARNING,
                c.path.value,
                f"handler calls {name}(), which --quiet cannot silence and an agent pays "
                "tokens to read; ctx.log is silent off a terminal unless --verbose (REQ-F-038)",
                "ctx.log(...) for info, ctx.progress(...) for progress, ctx.log_error(...) "
                "for errors, ctx.debug(...) for --debug",
            )


def _project_root(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        if c.project_root:
            continue
        marker = walks_up_from_cwd(c.handler)
        if marker is not None:
            yield Finding(
                "project-root",
                Severity.WARNING,
                c.path.value,
                "handler walks up from the working directory itself, so meta.project_root "
                "does not say which root it found (REQ-F-027, heuristic)",
                f'project_root=("{marker}",), then read ctx.project_root',
            )


_ENV_MAPS = frozenset({"ctx.env", "os.environ", "environ"})
_ENV_GETS = frozenset({"ctx.env.get", "os.environ.get", "environ.get", "os.getenv", "getenv"})


def env_reads(fn: Callable[..., object]) -> list[str]:
    """Literal variable names ``fn`` reads from ``ctx.env`` or ``os.environ``"""
    tree = source_tree(fn)
    if tree is None:
        return []
    names: list[str] = []
    for node in ast.walk(tree):
        key: ast.expr | None = None
        if isinstance(node, ast.Subscript) and _dotted(node.value) in _ENV_MAPS:
            key = node.slice
        elif isinstance(node, ast.Call) and _dotted(node.func) in _ENV_GETS and node.args:
            key = node.args[0]
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            names.append(key.value)
    return names


def _env_prefix(app: App) -> Iterator[Finding]:
    prefix = app_var(app.name, "") + "_"
    for c in user_commands(app):
        code = [c.handler, *(spec.acquire for spec in c.resource_graph.values())]
        seen = {n for fn in code for n in env_reads(fn)}
        for name in sorted(seen):
            if name.startswith(prefix) or name in UNPREFIXED or name in c.token_env_vars:
                continue
            yield Finding(
                "env-prefix",
                Severity.WARNING,
                c.path.value,
                f"reads the unprefixed variable {name}, which an agent may set for another "
                "tool in the same session (REQ-F-073)",
                f"read {app_var(app.name, name)} instead of {name}",
            )


_CONFIG_READS = frozenset(
    {"tomllib.load", "tomllib.loads", "tomli.load", "tomli.loads", "configparser.ConfigParser"}
)


def reads_config_by_hand(fn: Callable[..., object]) -> str | None:
    """The call ``fn`` parses a config file with, when it does so itself"""
    tree = source_tree(fn)
    if tree is None:
        return None
    calls = (_dotted(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call))
    return next((c for c in calls if c in _CONFIG_READS), None)


def _settings_declared(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        call = reads_config_by_hand(c.handler)
        if call is not None:
            yield Finding(
                "settings-declared",
                Severity.WARNING,
                c.path.value,
                f"handler parses a config file with {call}, so meta.config_sources, "
                "--show-config, --no-config, and --config cannot see it (REQ-F-028)",
                "declare the keys as a frozen dataclass with defaults, pass "
                "App(settings=Settings), and take settings: Settings in the handler",
            )


_SETUP_CALLS = frozenset({"mkdir", "makedirs", "write_text", "write_bytes", "write_config"})


def first_run_setup(fn: Callable[..., object]) -> str | None:
    """A setup call ``fn`` makes under ``if not <x>.exists():``, the shape of a silent
    first-run init; None when it has none"""
    tree = source_tree(fn)
    if tree is None:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)):
            continue
        probe = test.operand
        if not (isinstance(probe, ast.Call) and isinstance(probe.func, ast.Attribute)):
            continue
        if probe.func.attr not in ("exists", "is_file", "is_dir"):
            continue
        for inner in (n for stmt in node.body for n in ast.walk(stmt)):
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                if inner.func.attr in _SETUP_CALLS:
                    return inner.func.attr
    return None


def _init_isolated(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        call = first_run_setup(c.handler)
        if call is not None:
            yield Finding(
                "init-isolated",
                Severity.WARNING,
                c.path.value,
                f"handler calls {call} the first time it runs, so a first-run failure looks "
                "like a failure of this command (REQ-F-076, heuristic)",
                "move the setup into App(init=Setup()), whose initialized() checks and run() "
                "creates; the init built-in runs it and other commands exit INIT_REQUIRED",
            )


_SLEEPS = frozenset({"time.sleep", "sleep", "asyncio.sleep"})


def retries_by_hand(handler: Callable[..., object]) -> bool:
    """A loop that sleeps and is left from inside a ``try`` once the call succeeds, by a
    ``return`` or a ``break`` in its body or ``else``: a retry the framework cannot count.
    A loop that throttles between items, polls, or runs events has no such exit"""
    tree = source_tree(handler)
    if tree is None:
        return False
    for loop in ast.walk(tree):
        if not isinstance(loop, (ast.For, ast.AsyncFor, ast.While)):
            continue
        inner = list(ast.walk(loop))
        if not any(_sleeps(handler, n) for n in inner):
            continue
        for attempt in (n for n in inner if isinstance(n, ast.Try)):
            leaves = [m for part in (*attempt.body, *attempt.orelse) for m in ast.walk(part)]
            if any(isinstance(m, (ast.Return, ast.Break)) for m in leaves):
                return True
    return False


def _sleeps(handler: Callable[..., object], node: ast.AST) -> bool:
    """A call to time.sleep or asyncio.sleep, however it was imported"""
    if not isinstance(node, ast.Call):
        return False
    name = _dotted(node.func)
    if name is None:
        return False
    return name in _SLEEPS or any(
        resolve_name(handler, name) is f for f in (time.sleep, asyncio.sleep)
    )


def _retry_declared(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        if c.retry is None and retries_by_hand(c.handler):
            yield Finding(
                "retry-declared",
                Severity.WARNING,
                c.path.value,
                "handler retries in a loop with time.sleep, so meta.retries cannot report it "
                "and --retries cannot turn it off (REQ-F-078, heuristic)",
                "retry=treaty.Retry(on=(ConnectionError,)), then ctx.retry(lambda: call(...)) "
                "in place of the loop",
            )


def unguarded_steps(handler: Callable[..., object]) -> list[str]:
    """``ctx.step(...)`` calls whose result no ``if`` tests, so a resumed run would still
    do the skipped steps' work"""
    tree = source_tree(handler)
    params = list(inspect.signature(handler).parameters)
    if tree is None or len(params) < 2:
        return []
    tested: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.IfExp, ast.While)):
            tested.update(id(n) for n in ast.walk(node.test))
    return [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _dotted(node.func) == f"{params[1]}.step"
        and id(node) not in tested
    ]


def _resume_guard(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        if not c.resumable:
            continue
        for call in unguarded_steps(c.handler):
            yield Finding(
                "resume-guard",
                Severity.WARNING,
                c.path.value,
                f"{call} is not an if test: under --resume-from it returns False for a step "
                "to skip, and the step's work still runs (REQ-O-010)",
                f"if {call}:\n    <the step's work>",
            )


def raises_without(handler: Callable[..., object], exit_name: str, keyword: str) -> bool:
    """A call of ``Exit.<exit_name>(...)`` in the handler without ``keyword=``"""
    tree = source_tree(handler)
    if tree is None:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
        if name is None or not (name == exit_name or name.endswith(f"Exit.{exit_name}")):
            continue
        if not any(k.arg in (keyword, None) for k in node.keywords):
            return True
    return False


def _retry_hint(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        if raises_without(c.handler, "RATE_LIMITED", "retry_after_ms"):
            yield Finding(
                "retry-hint",
                Severity.WARNING,
                c.path.value,
                "raises RATE_LIMITED without retry_after_ms, which ends the run as "
                "INVALID_EXIT (REQ-C-014)",
                "Exit.RATE_LIMITED(..., retry_after_ms=<the Retry-After header in ms>)",
            )


def constant_fixes(handler: Callable[..., object]) -> list[tuple[str, str]]:
    """``(code, fix)`` of each raise in the handler with a literal ``fix_command=``;
    the code is the ``code=`` literal, else the ``Exit.<NAME>`` it calls"""
    tree = source_tree(handler)
    if tree is None:
        return []
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func) or ""
        keywords = {k.arg: k.value for k in node.keywords if k.arg is not None}
        fix = keywords.get("fix_command")
        if not isinstance(fix, ast.Constant) or not isinstance(fix.value, str):
            continue
        code = keywords.get("code")
        if isinstance(code, ast.Constant) and isinstance(code.value, str):
            found.append((code.value, fix.value))
        elif ".Exit." in f".{name}":
            found.append((name.rsplit(".", 1)[-1], fix.value))
    return found


def _fix_declared(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        for code, fix in constant_fixes(c.handler):
            yield Finding(
                "fix-declared",
                Severity.ADVICE,
                c.path.value,
                f"{code} raises a fixed fix_command, checked only when it is raised; a "
                "declared one is checked at startup (REQ-C-030)",
                f"fix_commands={{{code!r}: {fix!r}}}, and drop fix_command= from the raise",
            )


_LOCK_CALLS = frozenset({"flock", "lockf", "locking", "FileLock", "SoftFileLock"})


def locks_by_hand(handler: Callable[..., object]) -> str | None:
    """The first file-lock call in the handler's source, such as ``fcntl.flock``"""
    tree = source_tree(handler)
    if tree is None:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name is not None and name.rsplit(".", 1)[-1] in _LOCK_CALLS:
                return name
    return None


def _lock_declared(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        call = locks_by_hand(c.handler)
        if call is not None:
            yield Finding(
                "lock-declared",
                Severity.WARNING,
                c.path.value,
                f"handler locks a file with {call}(...), so a waiting run blocks with no "
                "LOCK_HELD error or retry_after_ms (REQ-F-033, heuristic)",
                'with ctx.lock("<name>", retry_after_ms=1000): in place of the file lock',
            )


def _effects(schema: object) -> set[str] | None:
    """The ``effect`` values an output schema admits; None when it is open or absent"""
    if not isinstance(schema, dict):
        return None
    if "anyOf" in schema:
        found = [_effects(s) for s in schema["anyOf"]]
        values = [v for v in found if v is not None]
        return set().union(*values) if values else None
    effect = schema.get("properties", {}).get("effect")
    if not isinstance(effect, dict):
        return None
    if "anyOf" in effect:
        return _effects({"anyOf": [{"properties": {"effect": e}} for e in effect["anyOf"]]})
    enum = effect.get("enum")
    return {str(v) for v in enum} if isinstance(enum, list) else None


def creates(command: Command) -> bool:
    """The output admits ``effect: "created"``, or, when it does not say, the name is a
    create verb"""
    effects = _effects(command.output_schema)
    if effects is not None:
        return "created" in effects
    return command.path.parts[-1].split("-")[0] in ("create", "add", "new", "register")


_DELETE_VERBS = (
    "delete",
    "del",
    "remove",
    "rm",
    "drop",
    "destroy",
    "purge",
    "erase",
    "wipe",
    "clear",
    "prune",
    "forget",
    "unset",
    "unregister",
)


def deletes(command: Command) -> bool:
    """The output admits ``effect: "deleted"``, or, when it does not say, the name is a
    delete verb: a restore or an overwrite is destructive without deleting what it names"""
    effects = _effects(command.output_schema)
    if effects is not None:
        return "deleted" in effects
    return command.path.parts[-1].split("-")[0] in _DELETE_VERBS


def _already_exists(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        if c.danger_level is DangerLevel.SAFE or not creates(c):
            continue
        if "CONFLICT" not in {n.value for n in c.exit_codes}:
            yield Finding(
                "already-exists",
                Severity.ADVICE,
                c.path.value,
                "a create command does not list CONFLICT in its own exit_codes (the manifest's "
                "entry every mutating command gets does not count), so a retried create cannot "
                "answer with the resource that already exists (REQ-C-028, heuristic)",
                'exit_codes=("CONFLICT",), then raise treaty.already_exists(existing, '
                "conflict_id=existing.id) when the resource is there",
            )


def _delete_not_found(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        if c.danger_level is not DangerLevel.DESTRUCTIVE or not deletes(c):
            continue
        if "NOT_FOUND" in {n.value for n in c.exit_codes}:
            yield Finding(
                "delete-not-found",
                Severity.WARNING,
                c.path.value,
                "a delete declares NOT_FOUND; deleting what is already gone succeeds, so a "
                "retried delete does not fail (REQ-C-028)",
                'when the resource is gone, return effect "noop", and on a dry run '
                '"would_delete" with would_affect=Affects("Deletes nothing: ...", (), 0); '
                "drop NOT_FOUND from exit_codes",
            )


def _profile(app: App, root: Path = Path(".")) -> Iterator[Finding]:
    if not any((root / "conformance").glob("*.json")):
        yield Finding(
            "profile",
            Severity.ADVICE,
            None,
            "no conformance profile under ./conformance; runtime guarantees are unverified",
            f"write conformance/{app.name}.json and run the spec kit: "
            "uv run conformance/run.py <profile>",
        )


RULES: tuple[Rule, ...] = (
    Rule("describe", "Every command has an example invocation", Severity.ADVICE, _describe),
    Rule(
        "danger-level",
        "Danger levels match what command names imply",
        Severity.WARNING,
        _danger_level,
    ),
    Rule(
        "exit-codes",
        "Non-safe commands declare their own exit codes",
        Severity.WARNING,
        _exit_codes,
    ),
    Rule("retryable", "Retryable codes only on idempotent commands", Severity.WARNING, _retryable),
    Rule(
        "exit-code-suggestion",
        "Retryable exit codes name a next step",
        Severity.ADVICE,
        _exit_code_suggestion,
    ),
    Rule(
        "typed-output",
        "Outputs are typed so output_schema is informative",
        Severity.ADVICE,
        _typed_output,
    ),
    Rule(
        "paginated-list",
        "List commands keep the framework's pagination",
        Severity.ADVICE,
        _paginated_list,
    ),
    Rule("network-io", "Network commands declare has_network_io", Severity.WARNING, _network_io),
    Rule(
        "network-timeout",
        "Network calls pass the command timeout",
        Severity.WARNING,
        _network_timeout,
    ),
    Rule(
        "subprocess-declared",
        "Commands that run a child declare its arguments",
        Severity.WARNING,
        _subprocess_declared,
    ),
    Rule(
        "background-declared",
        "Background processes are declared and started with ctx.spawn",
        Severity.WARNING,
        _background_declared,
    ),
    Rule(
        "fs-side-effects",
        "Commands that write to disk declare it",
        Severity.ADVICE,
        _fs_side_effects,
    ),
    Rule(
        "cache-declared",
        "Caches are declared with cache=",
        Severity.WARNING,
        _cache_declared,
    ),
    Rule(
        "recursive-traversal",
        "Recursive walks use ctx.walk",
        Severity.WARNING,
        _recursive_traversal,
    ),
    Rule(
        "http-client",
        "Network commands use ctx.http",
        Severity.WARNING,
        _http_client,
    ),
    Rule(
        "declared-commands",
        "Declared cleanup commands exist",
        Severity.ERROR,
        _declared_commands,
    ),
    Rule(
        "required-tools",
        "Programs a command runs are in required_tools",
        Severity.ADVICE,
        _required_tools,
    ),
    Rule(
        "preserve-locale",
        "Children run in the C locale",
        Severity.ADVICE,
        _preserve_locale,
    ),
    Rule(
        "builtin-shadowed",
        "App commands keep the yielding built-ins' names free",
        Severity.ADVICE,
        _builtin_shadowed,
    ),
    Rule(
        "doctor-fix",
        "Every doctor check that can fail gives a fix",
        Severity.ADVICE,
        _doctor_fix,
    ),
    Rule(
        "schema-changelog",
        "The schema changelog records the current manifest",
        Severity.WARNING,
        _schema_changelog,
    ),
    Rule("path-typed", "Path-like fields are typed Path", Severity.WARNING, _path_typed),
    Rule("id-pattern", "Identifier fields declare a pattern", Severity.WARNING, _id_pattern),
    Rule(
        "option-placement",
        "Commands that forward arguments declare strict placement",
        Severity.WARNING,
        _option_placement,
    ),
    Rule(
        "conditional-rules",
        "Cross-field checks are declared with requires=",
        Severity.ADVICE,
        _conditional_rules,
    ),
    Rule(
        "multiline-flag",
        "Free-text fields that may span lines declare multiline",
        Severity.ADVICE,
        _multiline_flag,
    ),
    Rule(
        "raw-payload", "Wide mutating commands accept --raw-payload", Severity.ADVICE, _raw_payload
    ),
    Rule("cleanup", "Network commands register a cleanup hook", Severity.ADVICE, _cleanup),
    Rule(
        "resource-release",
        "Resources that hold something define release",
        Severity.WARNING,
        _resource_release,
    ),
    Rule("broad-scope", "Required scopes are narrow", Severity.WARNING, _broad_scope),
    Rule("auth-declared", "Login commands declare auth", Severity.WARNING, _auth_declared),
    Rule(
        "refresh-declared",
        "Expired credentials name the command that renews them",
        Severity.ADVICE,
        _refresh_declared,
    ),
    Rule("async-job", "Commands that start work return a job", Severity.ADVICE, _async_job),
    Rule(
        "config-write-scope",
        "Config writes declare their scope",
        Severity.WARNING,
        _config_write_scope,
    ),
    Rule(
        "volatile-data",
        "Output data carries no per-call values",
        Severity.WARNING,
        _volatile_data,
    ),
    Rule(
        "id-field",
        "An output's primary id is declared for --format id",
        Severity.ADVICE,
        _id_field,
    ),
    Rule(
        "stable-order",
        "Arrays of objects in output declare their order",
        Severity.ADVICE,
        _stable_order,
    ),
    Rule(
        "external-data",
        "Commands that return outside content declare it external",
        Severity.WARNING,
        _external_data,
    ),
    Rule(
        "high-entropy",
        "Credential-named output fields are masked on purpose",
        Severity.ADVICE,
        _high_entropy,
    ),
    Rule(
        "binary-output",
        "Binary output is returned as bytes, not encoded by hand",
        Severity.ADVICE,
        _binary_output,
    ),
    Rule(
        "field-limits",
        "Size limits are declared, and backend cuts reported",
        Severity.ADVICE,
        _field_limits,
    ),
    Rule(
        "schema-version",
        "Output schema changes bump schema_version",
        Severity.ERROR,
        _schema_version,
    ),
    Rule(
        "no-chdir",
        "Commands never change the working directory",
        Severity.WARNING,
        _no_chdir,
    ),
    Rule(
        "log-not-print",
        "Handlers write diagnostics with ctx.log, not print",
        Severity.WARNING,
        _log_not_print,
    ),
    Rule(
        "project-root",
        "Commands that find a project root declare project_root",
        Severity.WARNING,
        _project_root,
    ),
    Rule(
        "resume-guard",
        "Resumable handlers skip the steps ctx.step says to",
        Severity.WARNING,
        _resume_guard,
    ),
    Rule(
        "retry-declared",
        "Retries go through ctx.retry",
        Severity.WARNING,
        _retry_declared,
    ),
    Rule(
        "settings-declared",
        "Config files are read through App(settings=)",
        Severity.WARNING,
        _settings_declared,
    ),
    Rule(
        "init-isolated",
        "First-run setup is an explicit init command",
        Severity.WARNING,
        _init_isolated,
    ),
    Rule(
        "env-prefix",
        "Handlers read only the tool's own environment variables",
        Severity.WARNING,
        _env_prefix,
    ),
    Rule("retry-hint", "Rate-limit errors say how long to wait", Severity.WARNING, _retry_hint),
    Rule(
        "lock-declared",
        "Handlers lock through ctx.lock",
        Severity.WARNING,
        _lock_declared,
    ),
    Rule(
        "fix-declared",
        "Fixed fix commands are declared, so startup checks them",
        Severity.ADVICE,
        _fix_declared,
    ),
    Rule(
        "already-exists",
        "Create commands answer a repeat with the existing resource",
        Severity.ADVICE,
        _already_exists,
    ),
    Rule(
        "delete-not-found",
        "Deleting a missing resource succeeds as a noop",
        Severity.WARNING,
        _delete_not_found,
    ),
    Rule("profile", "A conformance profile exists for the spec kit", Severity.ADVICE, _profile),
)


_DEPRECATED_MARK = re.compile(r"\(deprecated since [^)]*\)$")


def removals(
    app: App, baseline: Mapping[str, object], released: str | None = None
) -> Iterator[Finding]:
    """REQ-F-075 (04-D3): what the released manifest ``baseline`` lists that the app no
    longer has. A command may go once redirected, or once the baseline showed it
    deprecated; a flag once the baseline showed it deprecated; anything at a new major
    than ``released``, the app version that served the baseline (its ``meta.tool_version``)"""
    if released is not None and _major(app.version) > _major(released):
        return
    new = app.manifest()
    old_commands = baseline["commands"]
    new_commands = new["commands"]
    assert isinstance(old_commands, dict) and isinstance(new_commands, dict)
    for path, entry in sorted(old_commands.items()):
        current = new_commands.get(path)
        if current is None:
            if app._moved(tuple(path.split("."))) is None and not _deprecated(entry):
                yield Finding(
                    "additive",
                    Severity.ERROR,
                    path,
                    "the command is gone without a redirect or a release that deprecated it, "
                    "so an agent's saved invocation now fails as an unknown command",
                    f'app.redirect("{path}", to="<its replacement>"), or restore it with '
                    'deprecated=treaty.Deprecated("<this version>", replacement=...) for a release',
                )
            continue
        for flag, flag_entry in sorted(entry.get("flags", {}).items()):
            if flag not in current["flags"] and not _deprecated(flag_entry):
                yield Finding(
                    "additive",
                    Severity.ERROR,
                    path,
                    f"--{flag} is gone without a release that deprecated it",
                    f'restore --{flag} with Flag(..., deprecated=treaty.Deprecated("<this '
                    'version>", replacement=...)) for a release before removing it',
                )
        was = {**baseline.get("exit_codes", {}), **entry.get("exit_codes", {})}  # type: ignore[dict-item]
        now = {**new["exit_codes"], **current.get("exit_codes", {})}  # type: ignore[dict-item]
        for code in sorted(set(was) - set(now), key=int):
            yield Finding(
                "additive",
                Severity.ERROR,
                path,
                f"exit code {code} ({was[code]['name']}) is no longer declared, so an agent "
                "that handles it is now wrong",
                f'exit_codes=[..., "{was[code]["name"]}"] until the next major version',
            )


def _major(version: str) -> int:
    return int(version.partition(".")[0])


def _deprecated(entry: object) -> bool:
    description = entry.get("description") if isinstance(entry, dict) else None
    return isinstance(description, str) and bool(_DEPRECATED_MARK.search(description))


ADDITIVE = Rule(
    "additive",
    "Nothing the baseline manifest lists was removed without deprecation (--baseline)",
    Severity.ERROR,
    lambda app: iter(()),
)
"""Listed by ``treaty rules``; ``audit(baseline=...)`` runs it against the baseline"""


def audit(
    app: App,
    target: str,
    *,
    limit: int,
    baseline: Mapping[str, object] | None = None,
    released: str | None = None,
    root: Path = Path("."),
) -> AuditReport:
    """Every rule against ``app``; the schema lock and the conformance profile are looked
    for under ``root``, the run's ``--cwd`` for ``treaty audit``"""
    clear_caches()
    results: list[RuleResult] = []
    rooted: dict[object, Callable[[App], Iterator[Finding]]] = {
        _schema_version: lambda a: _schema_version(a, root),
        _profile: lambda a: _profile(a, root),
    }
    rules = [dataclasses.replace(r, check=rooted.get(r.check, r.check)) for r in RULES]
    if baseline is not None:
        rules.append(dataclasses.replace(ADDITIVE, check=lambda a: removals(a, baseline, released)))
    for rule in rules:
        findings = tuple(rule.check(app))
        results.append(RuleResult(rule.id, rule.title, rule.severity.value, not findings, findings))
    pending = in_order(f for r in results for f in r.findings)
    return AuditReport(target=target, rules=tuple(results), next_steps=pending[:limit])


def in_order(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    """Errors first, then warnings, then advice, so what fails --strict is never behind
    advice; within a severity, the order the rules run in, since sorted is stable"""
    return tuple(sorted(findings, key=lambda f: _RANK[f.severity]))
