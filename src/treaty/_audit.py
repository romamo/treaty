"""Static audit of an ``App``: ordered rules over the registry with generated fixes.

Rules see declarations, not behaviour. Runtime guarantees (timeouts, signals,
stream purity) are the conformance kit's job; the last rule points there.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from ._command import Command, DangerLevel
from ._exit import ExitCodeRegistry, FrameworkCode
from ._types import FlagType

if TYPE_CHECKING:
    from ._app import App

BUILTINS = frozenset({"manifest", "version", "exec", "check-permissions"})
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
        if p.value not in BUILTINS
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
                Severity.WARNING,
                c.path.value,
                "returns a list without paginated=True, so it has no default limit, "
                "--limit, --cursor, or meta.pagination (REQ-F-018, REQ-F-019)",
                "paginated=True; return the list, or a treaty.Page read with ctx.page",
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


# Calls that open a connection and take timeout=; requests and httpx by module prefix
_NETWORK_CALLS = frozenset({"urlopen", "create_connection", "HTTPConnection", "HTTPSConnection"})
_NETWORK_MODULES = frozenset({"requests", "httpx"})


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
        network = parts[-1] in _NETWORK_CALLS or (len(parts) > 1 and parts[0] in _NETWORK_MODULES)
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
        "Commands returning lists are paginated",
        Severity.WARNING,
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
    Rule("profile", "A conformance profile exists for the spec kit", Severity.ADVICE, _profile),
)


def audit(app: App, target: str, *, limit: int) -> AuditReport:
    results: list[RuleResult] = []
    for rule in RULES:
        findings = tuple(rule.check(app))
        results.append(RuleResult(rule.id, rule.title, rule.severity.value, not findings, findings))
    pending = [f for r in results for f in r.findings]
    return AuditReport(target=target, rules=tuple(results), next_steps=tuple(pending[:limit]))
