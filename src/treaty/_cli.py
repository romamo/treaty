"""The ``treaty`` console script, built on treaty itself."""

from __future__ import annotations

import importlib
import os
import re
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from ._app import App, NoArgs
from ._audit import RULES, AuditReport, Severity, audit
from ._context import Ctx
from ._errors import Exit, ParseError
from ._flags import Arg, Flag
from ._profile import (
    SPEC_FALLBACK,
    build_profile,
    has_kit,
    probes_for,
    run_kit,
    write_profile,
)
from ._scaffold import ProjectName, render


def _version() -> str:
    try:
        return version("treaty")
    except PackageNotFoundError:
        return "0.0.0"


cli = App("treaty", version=_version(), description="Build and audit agent-ready CLIs")
cli.exit_code(
    "CONFORMANCE_FAILED",
    80,
    description="The spec kit reported failing checks; data carries the result",
    retryable=False,
    side_effects="complete",
)
cli.exit_code(
    "AUDIT_FAILED",
    79,
    description="Strict audit found warnings or errors; data holds the full report",
    retryable=False,
    side_effects="none",
)

BLOCKING = frozenset({Severity.ERROR, Severity.WARNING})

_PERCENT_RE = re.compile(r"%[0-9A-Fa-f]{2}")


@dataclass(frozen=True, slots=True)
class WritePath:
    """A path this CLI writes to: no ``..`` segments, percent-encodings, or null bytes"""

    value: Path

    @classmethod
    def parse(cls, raw: str, flag: str) -> WritePath:
        context = {"flag": flag, "value": raw}
        if "\x00" in raw:
            raise ParseError(f"{flag} contains a null byte", context=context)
        if _PERCENT_RE.search(raw):
            raise ParseError(
                f"{flag} contains a percent-encoded sequence",
                context=context,
                suggestion=f"pass the decoded path: {flag} {unquote(raw)}",
            )
        if ".." in Path(raw).parts:
            raise ParseError(
                f"{flag} escapes its base directory with '..'",
                context=context,
                suggestion=f"pass the absolute path if intended: {flag} {Path(raw).resolve()}",
            )
        return cls(Path(raw))


@dataclass(frozen=True, slots=True)
class AuditArgs:
    target: str = Arg(description="Import path of the App object, as module:attribute")
    all: bool = Flag(default=False, description="List every finding instead of the next few")
    limit: int = Flag(default=3, description="How many next steps to show")
    strict: bool = Flag(default=False, description="Exit with AUDIT_FAILED on any warning or error")


@dataclass(frozen=True, slots=True)
class FindingOut:
    rule: str
    severity: str
    command: str | None
    message: str
    fix: str


@dataclass(frozen=True, slots=True)
class RuleOut:
    id: str
    title: str
    severity: str
    passed: bool
    findings: tuple[FindingOut, ...]


@dataclass(frozen=True, slots=True)
class AuditOut:
    target: str
    rules_total: int
    passed: int
    failed: int
    next_steps: tuple[FindingOut, ...]
    rules: tuple[RuleOut, ...]


def load_app(target: str) -> App:
    module_name, sep, attr = target.partition(":")
    if not sep or not module_name or not attr:
        raise Exit.ARG_ERROR("target must be module:attribute", context={"target": target})
    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise Exit.NOT_FOUND(
            f"cannot import {module_name}",
            context={"module": module_name, "missing": exc.name},
            suggestion="run from the project root inside its environment: uv run treaty audit ...",
        ) from None
    obj = getattr(module, attr, None)
    if obj is None:
        raise Exit.NOT_FOUND(f"{module_name} has no attribute {attr}", context={"target": target})
    if not isinstance(obj, App):
        raise Exit.PRECONDITION(
            f"{target} is {type(obj).__name__}, not a treaty App", context={"target": target}
        )
    return obj


def _to_out(report: AuditReport, show_all: bool) -> AuditOut:
    def conv(f: Any) -> FindingOut:
        return FindingOut(f.rule, f.severity.value, f.command, f.message, f.fix)

    rules = tuple(
        RuleOut(r.id, r.title, r.severity, r.passed, tuple(conv(f) for f in r.findings))
        for r in report.rules
    )
    pending = tuple(conv(f) for r in report.rules for f in r.findings)
    return AuditOut(
        target=report.target,
        rules_total=len(report.rules),
        passed=report.passed,
        failed=report.failed,
        next_steps=pending if show_all else tuple(conv(f) for f in report.next_steps),
        rules=rules,
    )


def render_audit(data: Any) -> str:
    lines = [f"{data['target']}: {data['passed']} of {data['rules_total']} rules pass", ""]
    for rule in data["rules"]:
        mark = "ok " if rule["passed"] else "-- "
        lines.append(f"  {mark} {rule['id']:<13} {rule['title']}")
    steps = data["next_steps"]
    if steps:
        lines.append("")
        lines.append("Next steps" if len(steps) > 1 else "Next step")
        for n, step in enumerate(steps, 1):
            where = f" [{step['command']}]" if step["command"] else ""
            lines.append(f"  {n}. ({step['severity']}) {step['rule']}{where}: {step['message']}")
            lines.append(f"     fix: {step['fix']}")
    else:
        lines.append("")
        lines.append("Nothing left to do here; run the conformance kit for runtime checks")
    return "\n".join(lines) + "\n"


@cli.command(
    "audit",
    description="Check an App's registrations against the spec and suggest the next step",
    exit_codes=["NOT_FOUND", "PRECONDITION", "AUDIT_FAILED"],
    examples=[
        ("Show the next steps", "treaty audit myapp.cli:app"),
        ("List every finding as JSON", "treaty audit myapp.cli:app --all --format json"),
        ("Fail a CI step on warnings", "treaty audit myapp.cli:app --strict"),
    ],
    human=render_audit,
)
def audit_command(args: AuditArgs, ctx: Ctx) -> AuditOut:
    if args.limit < 1:
        raise Exit.ARG_ERROR("limit must be at least 1", context={"limit": args.limit})
    app = load_app(args.target)
    report = audit(app, args.target, limit=args.limit)
    out = _to_out(report, args.all)
    if args.strict:
        blocking = [f for r in report.rules for f in r.findings if f.severity in BLOCKING]
        if blocking:
            raise Exit.AUDIT_FAILED(
                f"{len(blocking)} warning or error findings",
                context={"rules": sorted({f.rule for f in blocking})},
                suggestion="apply each fix, or drop --strict to report without failing",
                data=out,
            )
    return out


@cli.command("rules", description="List the audit rules in the order they are checked")
def rules_command(args: NoArgs, ctx: Ctx) -> list[dict[str, str]]:
    return [{"id": r.id, "title": r.title, "severity": r.severity.value} for r in RULES]


# init


@dataclass(frozen=True, slots=True)
class InitArgs:
    name: str = Arg(description="Project and command name, lowercase with hyphens")
    directory: str | None = Flag(default=None, description="Target directory, default ./<name>")
    dry_run: bool = Flag(default=False, description="List the files without writing them")
    treaty_source: str | None = Flag(
        default=None, description="Local treaty checkout to depend on instead of PyPI"
    )


@dataclass(frozen=True, slots=True)
class InitOut:
    directory: str
    files: tuple[str, ...]
    written: bool
    next_steps: tuple[str, ...]


def render_init(data: Any) -> str:
    verb = "Created" if data["written"] else "Would create"
    lines = [f"{verb} {data['directory']}", ""]
    lines.extend(f"  {f}" for f in data["files"])
    lines.append("")
    lines.append("Next")
    lines.extend(f"  {s}" for s in data["next_steps"])
    return "\n".join(lines) + "\n"


@cli.command(
    "init",
    description="Scaffold a new CLI project that passes treaty audit",
    danger_level="mutating",
    exit_codes=["CONFLICT"],
    examples=[("New project", "treaty init deployctl")],
    human=render_init,
)
def init_command(args: InitArgs, ctx: Ctx) -> InitOut:
    name = ProjectName(args.name)
    target = (
        WritePath.parse(args.directory, "--directory").value if args.directory else Path(name.value)
    )
    if target.exists() and any(target.iterdir()):
        raise Exit.CONFLICT(
            f"{target} exists and is not empty",
            context={"directory": str(target)},
            fix_required="choose an empty directory with --directory",
        )
    source = str(Path(args.treaty_source).resolve()) if args.treaty_source else None
    files = render(name, source)
    if not args.dry_run:
        for rel, content in files.items():
            path = target / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            if rel == f"conformance/{name.value}":
                path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return InitOut(
        directory=str(target),
        files=tuple(files),
        written=not args.dry_run,
        next_steps=(
            f"cd {target} && uv sync",
            f"uv run {name.value} status widget",
            "uv run pytest",
            f"uv run treaty audit {name.package}.cli:app",
            f"uv run treaty conformance {name.package}.cli:app --run",
        ),
    )


# conformance


@dataclass(frozen=True, slots=True)
class ConformanceArgs:
    target: str = Arg(description="Import path of the App object, as module:attribute")
    out: str | None = Flag(
        default=None, description="Profile path, default conformance/<name>.json"
    )
    command: tuple[str, ...] = Flag(
        default=(), description="Executable argv for probes, default the app name on PATH"
    )
    run: bool = Flag(default=False, description="Run the spec kit after writing the profile")
    spec_dir: str | None = Flag(
        default=None, description="Spec checkout with conformance/run.py; also TREATY_SPEC_DIR"
    )


@dataclass(frozen=True, slots=True)
class CheckOut:
    id: str
    status: str
    level: int
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConformanceOut:
    profile: str
    probes: int
    ran: bool
    levels: dict[str, str] | None
    checks: tuple[CheckOut, ...]


def render_conformance(data: Any) -> str:
    lines = [f"Profile: {data['profile']} ({data['probes']} probes)"]
    if not data["ran"]:
        lines.append("Kit not run; add --run to execute it")
        return "\n".join(lines) + "\n"
    lines.append("Levels: " + ", ".join(f"{k} {v}" for k, v in data["levels"].items()))
    lines.append("")
    for c in data["checks"]:
        lines.append(f"  {c['status']:<5} L{c['level']} {c['id']}")
        lines.extend(f"        {f}" for f in c["failures"])
    return "\n".join(lines) + "\n"


def resolve_spec_dir(explicit: str | None, env: Mapping[str, str]) -> Path:
    """A named location must hold the kit; only unnamed discovery falls back to the sibling"""
    if explicit:
        named, source = Path(explicit), "--spec-dir"
    elif env.get("TREATY_SPEC_DIR"):
        named, source = Path(env["TREATY_SPEC_DIR"]), "TREATY_SPEC_DIR"
    else:
        if has_kit(SPEC_FALLBACK):
            return SPEC_FALLBACK.resolve()
        raise Exit.PRECONDITION(
            "cannot find the spec's conformance/run.py",
            context={"tried": [str(SPEC_FALLBACK)]},
            fix_required="pass --spec-dir or set TREATY_SPEC_DIR to the spec checkout",
        )
    if not has_kit(named):
        raise Exit.PRECONDITION(
            f"{source} has no conformance/run.py",
            context={"source": source, "spec_dir": str(named)},
            fix_required=f"point {source} at a spec checkout containing conformance/run.py",
        )
    return named.resolve()


@cli.command(
    "conformance",
    description="Write a conformance profile from the registry and optionally run the spec kit",
    danger_level="mutating",
    exit_codes=["CONFORMANCE_FAILED", "NOT_FOUND", "PRECONDITION"],
    supports_raw_payload=True,
    examples=[("Write and run", "treaty conformance myapp.cli:app --run")],
    timeout=900,
    human=render_conformance,
)
def conformance_command(args: ConformanceArgs, ctx: Ctx) -> ConformanceOut:
    app = load_app(args.target)
    out = WritePath.parse(args.out, "--out").value if args.out else None
    spec_dir = resolve_spec_dir(args.spec_dir, os.environ) if args.run else None
    probes = probes_for(app)
    command = list(args.command) or [app.name]
    profile_path = out or Path("conformance") / f"{app.name}.json"
    write_profile(build_profile(app, command, probes), profile_path)
    result = ConformanceOut(str(profile_path), len(probes), False, None, ())
    if spec_dir is None:
        return result
    kit = run_kit(spec_dir, profile_path, ctx.timeout.seconds)
    if kit.envelope is None or kit.exit_code == 2:
        kit_error = kit.envelope.get("error") if kit.envelope else None
        raise Exit.PRECONDITION(
            "the kit rejected the profile or produced no envelope",
            context={
                "kit_exit": kit.exit_code,
                "kit_error": kit_error,
                "stderr": kit.stderr[-2000:],
            },
            data=result,
        )
    report = kit.envelope["data"]
    assert isinstance(report, dict)
    checks = tuple(
        CheckOut(
            c["id"],
            c["status"],
            c["level"],
            tuple(f"{f['probe']}: {f['detail']}" for f in c["failures"]),
        )
        for c in report["checks"]
    )
    result = ConformanceOut(str(profile_path), len(probes), True, dict(report["levels"]), checks)
    if kit.exit_code != 0:
        raise Exit.CONFORMANCE_FAILED(
            f"{report['summary']['failed']} conformance checks failed",
            context={"summary": report["summary"]},
            data=result,
        )
    return result


def main() -> None:
    cli.main()
