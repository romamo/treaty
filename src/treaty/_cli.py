"""The ``treaty`` console script, built on treaty itself."""

from __future__ import annotations

import importlib
import json
import re
import stat
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from ._agents_md import AGENTS_FILE, Mismatch, check, check_tools, render_file
from ._app import App, NoArgs
from ._atomic import write_atomic
from ._audit import ADDITIVE, LOCK_FILE, RULES, AuditReport, Severity, audit, schema_lock
from ._changelog import ChangelogEntry, diff, dump_changelog, load_changelog, record
from ._context import Ctx
from ._errors import Exit, ParseError
from ._flags import Arg, Flag
from ._mode import Format
from ._out import Out
from ._profile import (
    SPEC_FALLBACK,
    build_profile,
    default_command,
    has_kit,
    probes_for,
    profile_drift,
    run_kit,
    write_profile,
)
from ._scaffold import ProjectName, render

_PEP440_RE = re.compile(r"(\d+\.\d+\.\d+)(?:(a|b|rc)(\d+))?")
_PRE_LABELS = {"a": "alpha", "b": "beta", "rc": "rc"}


def semver_of(pep440: str) -> str:
    """The semver spelling of a PEP 440 release, ``1.0.0rc1`` as ``1.0.0-rc.1``;
    ``App(version=)`` takes semver, and PEP 440 reads the result back as the same version"""
    match = _PEP440_RE.fullmatch(pep440)
    if match is None:
        raise ValueError(f"treaty version {pep440!r} has no semver spelling")
    release, label, number = match.groups()
    return release if label is None else f"{release}-{_PRE_LABELS[label]}.{number}"


# The treaty CLI keeps no audit log: `treaty audit` is the linter, and a second
# `audit-log` beside it would only confuse
cli = App(
    "treaty",
    version=semver_of(__version__),
    description="Build and audit agent-ready CLIs",
    audit_log=None,
)
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

cli.exit_code(
    "DOCS_OUT_OF_DATE",
    81,
    description="An agent doc disagrees with the app; data.mismatches lists each difference",
    retryable=False,
    side_effects="none",
)

BLOCKING = frozenset({Severity.ERROR, Severity.WARNING})


def check_target(target: str) -> None:
    """``module:attribute``, checked in phase 1 so a malformed target exits 2"""
    module_name, sep, attr = target.partition(":")
    if not sep or not module_name or not attr:
        raise ParseError(
            "target must be module:attribute", context={"argument": "target", "target": target}
        )


@dataclass(frozen=True, slots=True)
class AuditArgs:
    target: str = Arg(description="Import path of the App object, as module:attribute")
    all: bool = Flag(default=False, description="List every finding instead of the next few")
    limit: int = Flag(default=3, description="How many next steps to show")
    strict: bool = Flag(default=False, description="Exit with AUDIT_FAILED on any warning or error")
    baseline: Path | None = Flag(
        default=None,
        description="The last release's manifest (tool manifest --format json); anything it "
        "lists that is gone without deprecation is an error",
    )

    def __post_init__(self) -> None:
        errors = []
        try:
            check_target(self.target)
        except ParseError as exc:
            errors.append(exc)
        if self.limit < 1:
            errors.append(
                ParseError(
                    "limit must be at least 1", context={"flag": "limit", "limit": self.limit}
                )
            )
        if errors:
            raise ParseError.combine(errors)


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
    findings: tuple[FindingOut, ...] = Out(ordered=True)


@dataclass(frozen=True, slots=True)
class AuditOut:
    target: str
    rules_total: int
    passed: int
    failed: int
    next_steps: tuple[FindingOut, ...] = Out(ordered=True)
    """Most important first"""
    rules: tuple[RuleOut, ...] = Out(ordered=True)
    """In the order they are checked"""


def load_app(target: str) -> App:
    """The App at ``target``; the commands check its shape in phase 1, ``treaty-mcp`` here"""
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
        raise Exit.NOT_FOUND(
            f"Module {module_name} has no attribute {attr}", context={"target": target}
        )
    if not isinstance(obj, App):
        raise Exit.PRECONDITION(
            f"Target {target} is {type(obj).__name__}, not a treaty App", context={"target": target}
        )
    return obj


def read_baseline(path: Path) -> tuple[dict[str, object], str | None]:
    """A saved ``tool manifest`` response, or its ``data``; with the app version that
    served it, when the response's ``meta.tool_version`` is there"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise Exit.NOT_FOUND(f"no baseline at {path}", context={"baseline": str(path)}) from None
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise Exit.PRECONDITION(
            f"baseline {path} is not JSON: {exc.msg}", context={"baseline": str(path)}
        ) from None
    manifest = loaded.get("data", loaded) if isinstance(loaded, dict) else None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("commands"), dict):
        raise Exit.PRECONDITION(
            f"baseline {path} is not a manifest",
            context={"baseline": str(path)},
            suggestion="save one with: tool manifest --format json > manifest.json",
        )
    meta = loaded.get("meta") if manifest is not loaded else None
    released = meta.get("tool_version") if isinstance(meta, dict) else None
    return manifest, released if isinstance(released, str) else None


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
        (
            "Check nothing released was removed",
            "treaty audit myapp.cli:app --baseline manifest.json --strict",
        ),
    ],
    renderers={Format.PLAIN: render_audit},
    danger_level="safe",
)
def audit_command(args: AuditArgs, ctx: Ctx) -> AuditOut:
    app = load_app(args.target)
    baseline, released = (None, None) if args.baseline is None else read_baseline(args.baseline)
    report = audit(app, args.target, limit=args.limit, baseline=baseline, released=released)
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


@cli.command(
    "rules",
    description="List the audit rules in the order they are checked",
    danger_level="safe",
    exit_codes=(),
    default_limit=0,  # a short, fixed list
    ordered=True,
)
def rules_command(args: NoArgs, ctx: Ctx) -> list[dict[str, str]]:
    return [
        {"id": r.id, "title": r.title, "severity": r.severity.value} for r in (*RULES, ADDITIVE)
    ]


# schema-lock


@dataclass(frozen=True, slots=True)
class SchemaLockArgs:
    target: str = Arg(description="Import path of the App object, as module:attribute")

    def __post_init__(self) -> None:
        check_target(self.target)


@dataclass(frozen=True, slots=True)
class SchemaLockOut:
    effect: str
    lock: str
    commands: int


@cli.command(
    "schema-lock",
    description="Record each command's schema version and output schema for the audit to diff",
    danger_level="mutating",
    exit_codes=["NOT_FOUND", "PRECONDITION"],
    examples=[("Record the contracts", "treaty schema-lock myapp.cli:app")],
)
def schema_lock_command(args: SchemaLockArgs, ctx: Ctx) -> SchemaLockOut:
    """The schema-version audit rule compares the registry against this file (REQ-F-022)"""
    app = load_app(args.target)
    lock = schema_lock(app)
    text = json.dumps(lock, indent=2, sort_keys=True) + "\n"
    old = LOCK_FILE.read_text(encoding="utf-8") if LOCK_FILE.is_file() else None
    effect = "noop" if old == text else "created" if old is None else "updated"
    if effect != "noop":
        write_atomic(LOCK_FILE, text, new_mode=0o644)
    commands = lock["commands"]
    assert isinstance(commands, dict)
    return SchemaLockOut(effect, str(LOCK_FILE), len(commands))


# changelog-add


@dataclass(frozen=True, slots=True)
class ChangelogAddArgs:
    target: str = Arg(description="Import path of the App object, as module:attribute")

    def __post_init__(self) -> None:
        check_target(self.target)


@dataclass(frozen=True, slots=True)
class ChangelogAddOut:
    effect: str
    changelog: str
    snapshot: str
    entry: ChangelogEntry | None
    """The entry written, None when the manifest is unchanged"""


@cli.command(
    "changelog-add",
    description="Record the manifest changes since the last snapshot in the app's schema "
    "changelog, then update the snapshot",
    danger_level="mutating",
    exit_codes=["NOT_FOUND", "PRECONDITION"],
    examples=[("Record this release's schema changes", "treaty changelog-add myapp.cli:app")],
)
def changelog_add_command(args: ChangelogAddArgs, ctx: Ctx) -> ChangelogAddOut:
    """The manifest snapshot ``<app>.manifest.json`` sits beside the changelog (REQ-O-029)"""
    app = load_app(args.target)
    path = app.schema_changelog
    if path is None:
        raise Exit.PRECONDITION(
            f"App {app.name} declares no schema_changelog",
            context={"target": args.target},
            fix_required="pass App(..., schema_changelog=Path(__file__).parent / "
            '"schema-changelog.json") and run this again',
        )
    snapshot = path.with_name(f"{app.name}.manifest.json")
    live = app.manifest()
    entries = load_changelog(path, app.name)
    if entries and entries[0].etag == live["etag"]:
        return ChangelogAddOut("noop", str(path), str(snapshot), None)
    old = read_baseline(snapshot)[0] if snapshot.is_file() else None
    today = datetime.now(UTC).date()
    entries, entry = record(entries, diff(old, live), app.version, str(live["etag"]), today)
    effect = "updated" if path.is_file() else "created"
    write_atomic(path, dump_changelog(entries), new_mode=0o644)
    write_atomic(snapshot, json.dumps(live, indent=2, sort_keys=True) + "\n", new_mode=0o644)
    return ChangelogAddOut(effect, str(path), str(snapshot), entry)


# agents-md


@dataclass(frozen=True, slots=True)
class AgentsMdArgs:
    target: str = Arg(description="Import path of the App object, as module:attribute")
    path: Path = Flag(default=Path(AGENTS_FILE), description="The file to write")
    invocation: str | None = Flag(
        default=None, description="Command prefix agents use, default the app name"
    )

    def __post_init__(self) -> None:
        check_target(self.target)


@dataclass(frozen=True, slots=True)
class AgentsMdOut:
    effect: str
    path: Path
    cli_version: str


@cli.command(
    "agents-md",
    description="Write AGENTS.md from the registry: the cli-version comment and the generated "
    "sections between the treaty markers; text outside the markers is kept",
    danger_level="mutating",
    exit_codes=["NOT_FOUND", "PRECONDITION"],
    examples=[("Write or refresh ./AGENTS.md", "treaty agents-md myapp.cli:app")],
)
def agents_md_command(args: AgentsMdArgs, ctx: Ctx) -> AgentsMdOut:
    """REQ-O-043, REQ-O-044: ``treaty check-docs`` keeps the result current"""
    app = load_app(args.target)
    path = ctx.cwd / args.path
    old = path.read_text(encoding="utf-8") if path.is_file() else None
    text = render_file(app, old, args.target, args.invocation or app.name)
    effect = "noop" if old == text else "created" if old is None else "updated"
    if effect != "noop":
        write_atomic(path, text, new_mode=0o644)
    return AgentsMdOut(effect, path, app.version)


# check-docs


@dataclass(frozen=True, slots=True)
class CheckDocsArgs:
    target: str = Arg(description="Import path of the App object, as module:attribute")
    paths: tuple[Path, ...] = Arg(
        description="AGENTS.md, a generate-skills directory, or a treaty-mcp --list-tools file"
    )

    def __post_init__(self) -> None:
        check_target(self.target)


@dataclass(frozen=True, slots=True)
class CheckDocsOut:
    cli_version: str
    files: tuple[Path, ...] = Out(ordered=True)
    mismatches: tuple[Mismatch, ...] = Out(ordered=True)


def render_check_docs(data: Any) -> str:
    lines = [
        f"- {m['file']}:{m['line']} {m['kind']} {m['name']}: {m['problem']}"
        for m in data["mismatches"]
    ]
    if not lines:
        count = len(data["files"])
        lines.append(
            f"{count} {'file matches' if count == 1 else 'files match'} {data['cli_version']}"
        )
    return "\n".join(lines).rstrip("\n") + "\n"


def _doc_files(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(path.glob("*.md"))
    if not path.is_file():
        raise Exit.NOT_FOUND(f"no file or directory {path}", context={"path": str(path)})
    return [path]


@cli.command(
    "check-docs",
    description="Check agent docs against the app: the declared version, AGENTS.md's "
    "sections, and every command, flag, and variable they name against --help",
    danger_level="safe",
    exit_codes=["NOT_FOUND", "PRECONDITION", "DOCS_OUT_OF_DATE"],
    examples=[
        ("Check AGENTS.md in CI", "treaty check-docs myapp.cli:app AGENTS.md"),
        ("Check the skill files too", "treaty check-docs myapp.cli:app AGENTS.md skills"),
    ],
    renderers={Format.PLAIN: render_check_docs},
)
def check_docs_command(args: CheckDocsArgs, ctx: Ctx) -> CheckDocsOut:
    """REQ-O-045, REQ-O-046: drift exits 81 with one mismatch per item"""
    app = load_app(args.target)
    files: list[Path] = []
    mismatches: list[Mismatch] = []
    for given in args.paths:
        for path in _doc_files(ctx.cwd / given):
            files.append(path)
            text = path.read_text(encoding="utf-8")
            if path.suffix == ".json":
                try:
                    mismatches += check_tools(app, path, text)
                except json.JSONDecodeError as exc:
                    raise Exit.PRECONDITION(
                        f"{path} is not JSON: {exc.msg}", context={"path": str(path)}
                    ) from None
            else:
                mismatches += check(app, path, text, agents_md=path.name == AGENTS_FILE)
    out = CheckDocsOut(app.version, tuple(files), tuple(mismatches))
    if mismatches:
        raise Exit.DOCS_OUT_OF_DATE(
            f"{len(mismatches)} items in the docs disagree with {app.name} {app.version}",
            context={"files": [str(f) for f in files]},
            fix_required=f"run treaty agents-md {args.target}, then fix what it does not write",
            data=out,
        )
    return out


# init


@dataclass(frozen=True, slots=True)
class InitArgs:
    name: str = Arg(description="Project and command name, lowercase with hyphens")
    directory: Path | None = Flag(default=None, description="Target directory, default ./<name>")
    dry_run: bool = Flag(default=False, description="List the files without writing them")
    treaty_source: str | None = Flag(
        default=None, description="Local treaty checkout to depend on instead of PyPI"
    )

    def __post_init__(self) -> None:
        errors = []
        try:
            ProjectName(self.name)
        except ParseError as exc:
            errors.append(exc)
        source = self.treaty_source
        if source is not None and not (Path(source) / "pyproject.toml").is_file():
            # Caught now, not as a "Distribution not found" from uv sync in the new project
            errors.append(
                ParseError(
                    f"--treaty-source {source} is not a treaty checkout",
                    context={"flag": "treaty-source", "treaty_source": source},
                    suggestion="pass the directory that holds treaty's pyproject.toml",
                )
            )
        if errors:
            raise ParseError.combine(errors)


@dataclass(frozen=True, slots=True)
class InitOut:
    effect: str
    directory: Path
    files: tuple[str, ...]
    written: bool
    next_steps: tuple[str, ...] = Out(ordered=True)


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
    renderers={Format.PLAIN: render_init},
)
def init_command(args: InitArgs, ctx: Ctx) -> InitOut:
    name = ProjectName(args.name)
    target = args.directory if args.directory is not None else Path(name.value)
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise Exit.CONFLICT(
            f"Directory {target} exists and is not empty",
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
        effect="would_create" if args.dry_run else "created",
        directory=target,
        files=tuple(files),
        written=not args.dry_run,
        next_steps=(
            f"cd {target} && uv sync",
            f"uv run {name.value} show widget",
            "uv run pytest",
            f"uv run treaty audit {name.package}.cli:app",
            f"uv run treaty conformance {name.package}.cli:app --run",
        ),
    )


# conformance


@dataclass(frozen=True, slots=True)
class ConformanceArgs:
    target: str = Arg(description="Import path of the App object, as module:attribute")
    out: Path | None = Flag(
        default=None, description="Profile path, default conformance/<name>.json"
    )
    command: tuple[str, ...] = Flag(
        default=(),
        description="Executable argv for probes; default the launcher next to the profile "
        "(conformance/<name>), on Windows the app's console script in this environment, "
        "else the app name on PATH",
    )
    run: bool = Flag(default=False, description="Run the spec kit after writing the profile")
    force: bool = Flag(
        default=False,
        description="Replace an existing profile that differs from the generated one; "
        "without it the command exits 6 and leaves the file alone",
    )
    spec_dir: Path | None = Flag(
        default=None, description="Spec checkout with conformance/run.py; also TREATY_SPEC_DIR"
    )

    def __post_init__(self) -> None:
        check_target(self.target)


@dataclass(frozen=True, slots=True)
class CheckOut:
    id: str
    status: str
    level: int
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConformanceOut:
    effect: str
    profile: str
    probes: int
    ran: bool
    levels: dict[str, str]
    """Empty until the kit runs"""
    checks: tuple[CheckOut, ...] = Out(sort_key="id")


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


def resolve_spec_dir(explicit: Path | None, env: Mapping[str, str]) -> Path:
    """A named location must hold the kit; only unnamed discovery falls back to the sibling"""
    if explicit is not None:
        named, source = explicit, "--spec-dir"
    elif env.get("TREATY_SPEC_DIR"):
        named, source = Path(env["TREATY_SPEC_DIR"]), "TREATY_SPEC_DIR"
    else:
        if has_kit(SPEC_FALLBACK):
            return SPEC_FALLBACK.resolve()
        raise Exit.PRECONDITION(
            "cannot find the spec's conformance/run.py",
            context={"tried": [str(SPEC_FALLBACK.resolve())]},
            fix_required="pass --spec-dir or set TREATY_SPEC_DIR to the spec checkout",
        )
    if not has_kit(named):
        raise Exit.PRECONDITION(
            f"Spec checkout {source} has no conformance/run.py",
            context={"source": source, "spec_dir": str(named)},
            fix_required=f"point {source} at a spec checkout containing conformance/run.py",
        )
    return named.resolve()


@cli.command(
    "conformance",
    description="Write a conformance profile from the registry and optionally run the spec kit",
    danger_level="mutating",
    exit_codes=["CONFLICT", "CONFORMANCE_FAILED", "NOT_FOUND", "PRECONDITION"],
    supports_raw_payload=True,
    examples=[("Write and run", "treaty conformance myapp.cli:app --run")],
    timeout=900,
    renderers={Format.PLAIN: render_conformance},
)
def conformance_command(args: ConformanceArgs, ctx: Ctx) -> ConformanceOut:
    app = load_app(args.target)
    out = args.out
    spec_dir = resolve_spec_dir(args.spec_dir, ctx.env) if args.run else None
    probes = probes_for(app)
    profile_path = out or Path("conformance") / f"{app.name}.json"
    if args.command:
        command, beside_profile = list(args.command), False
    else:
        command, beside_profile = default_command(
            app.name,
            profile_path.parent,
            Path(sys.executable).parent,
            windows=sys.platform == "win32",
        )
    profile = build_profile(app, command, probes, beside_profile=beside_profile)
    # A profile may carry hand-written probes; only --force trades them for generated ones
    drift = profile_drift(profile_path, profile) if profile_path.exists() else {}
    if drift is None:
        effect = "noop"
    elif not profile_path.exists():
        effect = "created"
    elif args.force:
        effect = "updated"
    else:
        raise Exit.CONFLICT(
            f"Profile {profile_path} differs from the one treaty generates",
            context={"profile": str(profile_path), **drift},
            fix_required="pass --force to replace it, --out to write the generated profile "
            "elsewhere, or run the spec kit on the existing profile directly",
        )
    if effect != "noop":
        write_profile(profile, profile_path)
    result = ConformanceOut(effect, str(profile_path), len(probes), False, {}, ())
    if spec_dir is None:
        return result
    # The kit's deadline ends first, so it is killed rather than orphaned by the TIMEOUT path
    seconds = ctx.timeout.seconds
    try:
        kit = run_kit(spec_dir, profile_path, None if seconds is None else max(seconds - 5, 1))
    except subprocess.TimeoutExpired as exc:
        raise Exit.PRECONDITION(
            f"the kit did not finish within {exc.timeout:g}s",
            context={"timeout_seconds": exc.timeout},
            fix_required="run the kit directly without a deadline: uv run --project "
            f"{spec_dir} {spec_dir / 'conformance' / 'run.py'} {profile_path}",
            data=result,
        ) from None
    report = kit.envelope.get("data") if kit.envelope else None
    if kit.exit_code == 2 or not isinstance(report, dict) or "checks" not in report:
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
    checks = tuple(
        CheckOut(
            c["id"],
            c["status"],
            c["level"],
            tuple(f"{f['probe']}: {f['detail']}" for f in c["failures"]),
        )
        for c in report["checks"]
    )
    result = ConformanceOut(
        effect, str(profile_path), len(probes), True, dict(report["levels"]), checks
    )
    if kit.exit_code != 0:
        raise Exit.CONFORMANCE_FAILED(
            f"{report['summary']['failed']} conformance checks failed",
            context={"summary": report["summary"]},
            data=result,
        )
    return result


def main() -> None:
    cli.main()
