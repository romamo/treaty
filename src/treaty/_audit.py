"""Static audit of an ``App``: ordered rules over the registry with generated fixes.

Rules see declarations, not behaviour. Runtime guarantees (timeouts, signals,
stream purity) are the conformance kit's job; the last rule points there.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import re
import textwrap
import typing
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._command import Command, DangerLevel
from ._env import UNPREFIXED, app_var
from ._errors import Exit
from ._exit import ExitCodeRegistry, FrameworkCode
from ._out import out_spec
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


def _describe(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        if not c.examples:
            example = " ".join(
                [app.name, *c.path.parts, *(f"<{f.flag}>" for f in c.fields if f.required)]
            )
            yield Finding(
                "describe",
                Severity.ADVICE,
                c.path.value,
                "no example invocation; agents copy examples verbatim as starting points",
                f'examples=[("What it does", "{example}")]',
            )


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
    for c in user_commands(app):
        if c.danger_level is DangerLevel.SAFE or c.exit_codes:
            continue
        name = c.path.parts[-1].upper().replace("-", "_") + "_FAILED"
        yield Finding(
            "exit-codes",
            Severity.WARNING,
            c.path.value,
            "non-safe command declares no command-specific exit codes; "
            "agents cannot tell failures apart",
            f'app.exit_code("{name}", 79, description="...", retryable=False, side_effects="none")'
            f' then exit_codes=["{name}"]',
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


def _network_io(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        if c.has_network_io:
            continue
        try:
            source = inspect.getsource(c.handler)
        except OSError, TypeError:
            continue  # no source to scan (REPL, exec, C extension); the heuristic cannot apply
        if _NETWORK_HINTS.search(source):
            yield Finding(
                "network-io",
                Severity.WARNING,
                c.path.value,
                "handler source mentions a network library "
                "but has_network_io is not declared (heuristic)",
                "has_network_io=True, then pass ctx.timeout.seconds to every network call",
            )


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
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
    except OSError, TypeError:
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
        for name in untimed_network_calls(c.handler):
            yield Finding(
                "network-timeout",
                Severity.WARNING,
                c.path.value,
                f"{name}(...) has no timeout=, so it can outlive --timeout",
                f"{name}(..., timeout=ctx.timeout.seconds)",
            )


_SHELL_CALLS = frozenset({"os.system", "os.popen", "system", "popen"})


def shell_calls(handler: Callable[..., object]) -> list[str]:
    """Calls in the handler's source that hand a string to a shell (REQ-F-044)"""
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
    except OSError, TypeError:
        return []  # no source to scan (REPL, exec, C extension)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
        if name is None:
            continue
        shell = any(
            k.arg == "shell" and not (isinstance(k.value, ast.Constant) and not k.value.value)
            for k in node.keywords
        )
        if name in _SHELL_CALLS or shell:
            found.append(name)
    return found


def _no_shell(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        for name in shell_calls(c.handler):
            yield Finding(
                "no-shell",
                Severity.WARNING,
                c.path.value,
                f"{name}(...) runs a shell, which splits words, expands globs, and "
                "executes metacharacters in arguments",
                "ctx.run(['program', 'arg', ...]), which takes an argument list "
                "and never starts a shell",
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
                    f"{f.name}: str = Flag(..., multiline=True) if it may span lines",
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
                "network command has no cleanup hook for SIGINT and SIGTERM",
                "cleanup=release_resources where the function "
                "closes connections and removes temp files",
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
                'config_write_scope="local" and write through ctx.write_config',
            )


def _handler_tree(handler: Callable[..., object]) -> ast.AST | None:
    try:
        return ast.parse(textwrap.dedent(inspect.getsource(handler)))
    except OSError, TypeError:
        return None  # no source to scan (REPL, exec, C extension)


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


_BASE64_CALLS = frozenset(
    {"base64.b64encode", "b64encode", "base64.standard_b64encode", "base64.encodebytes"}
)


def _binary_output(app: App) -> Iterator[Finding]:
    for c in user_commands(app):
        tree = _handler_tree(c.handler)
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
        tree = _handler_tree(c.handler)
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


def schema_change(old: object, new: object) -> Change:
    """How an output schema changed for a reader: a removed or retyped field breaks it,
    an added field does not; a field that became optional breaks it too"""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return Change.NONE if old == new else Change.BREAKING
    shape_keys = ("properties", "required", "items", *_NOTES)
    if {k: v for k, v in old.items() if k not in shape_keys} != {
        k: v for k, v in new.items() if k not in shape_keys
    }:
        return Change.BREAKING
    changes: list[Change] = []
    if "items" in old or "items" in new:
        changes.append(schema_change(old.get("items"), new.get("items")))
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


def _read_lock() -> dict[str, dict[str, object]] | None:
    if not LOCK_FILE.is_file():
        return None
    try:
        commands = json.loads(LOCK_FILE.read_text(encoding="utf-8"))["commands"]
        if not isinstance(commands, dict):
            raise TypeError("commands is not an object")
        return commands
    except (ValueError, KeyError, TypeError) as exc:
        raise Exit.PRECONDITION(
            f"{LOCK_FILE} is not a schema lock: {exc}",
            context={"lock": str(LOCK_FILE)},
            fix_required="delete it and run treaty schema-lock module:app again",
        ) from None


def _schema_version(app: App) -> Iterator[Finding]:
    locked = _read_lock()
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
    tree = _handler_tree(handler)
    if tree is None:
        return None
    nodes = list(ast.walk(tree))
    cwd = any(isinstance(n, ast.Call) and _dotted(n.func) in _CWD_CALLS for n in nodes)
    up = any(isinstance(n, ast.Attribute) and n.attr in ("parent", "parents") for n in nodes)
    if not (cwd and up):
        return None
    named = {n.value for n in nodes if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    return next((m for m in _ROOT_MARKERS if m in named), ".git")


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
    tree = _handler_tree(fn)
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
    tree = _handler_tree(fn)
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
    tree = _handler_tree(fn)
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


_SLEEPS = frozenset({"time.sleep", "sleep"})


def retries_by_hand(handler: Callable[..., object]) -> bool:
    """A loop holding a ``try`` and a ``time.sleep``: a retry the framework cannot count"""
    tree = _handler_tree(handler)
    if tree is None:
        return False
    for loop in ast.walk(tree):
        if not isinstance(loop, (ast.For, ast.While)):
            continue
        inner = list(ast.walk(loop))
        tried = any(isinstance(n, ast.Try) for n in inner)
        slept = any(isinstance(n, ast.Call) and _dotted(n.func) in _SLEEPS for n in inner)
        if tried and slept:
            return True
    return False


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


def _profile(app: App) -> Iterator[Finding]:
    if not any(Path("conformance").glob("*.json")):
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
    Rule("no-shell", "Handlers never run a shell", Severity.WARNING, _no_shell),
    Rule("path-typed", "Path-like fields are typed Path", Severity.WARNING, _path_typed),
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
    Rule("broad-scope", "Required scopes are narrow", Severity.WARNING, _broad_scope),
    Rule("auth-declared", "Login commands declare auth", Severity.WARNING, _auth_declared),
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
        "stable-order",
        "Arrays of objects in output declare their order",
        Severity.ADVICE,
        _stable_order,
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
        "project-root",
        "Commands that find a project root declare project_root",
        Severity.WARNING,
        _project_root,
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
    Rule("profile", "A conformance profile exists for the spec kit", Severity.ADVICE, _profile),
)


def audit(app: App, target: str, *, limit: int) -> AuditReport:
    results: list[RuleResult] = []
    for rule in RULES:
        findings = tuple(rule.check(app))
        results.append(RuleResult(rule.id, rule.title, rule.severity.value, not findings, findings))
    pending = [f for r in results for f in r.findings]
    return AuditReport(target=target, rules=tuple(results), next_steps=tuple(pending[:limit]))
