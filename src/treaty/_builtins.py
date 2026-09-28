"""Built-ins every app gets that yield to an app command of the same name (13-D1):
``doctor`` (REQ-O-026, REQ-O-031, REQ-C-018), ``cleanup`` (REQ-C-011, REQ-O-027),
``status`` (REQ-O-028), and ``audit-log`` (REQ-O-030)."""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import shlex
import shutil
import stat
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ._atomic import write_atomic
from ._auth import Expired
from ._cache import cache_dir
from ._changelog import ChangelogEntry, version_key
from ._config import local_config, user_config
from ._context import Ctx
from ._declare import SideEffect, SideEffectType
from ._deps import (
    Check,
    CheckFn,
    Endpoint,
    Found,
    Version,
    dependency_result,
    find,
    tool_check,
)
from ._effect import Affects
from ._errors import CliExit, ParseError
from ._flags import Flag
from ._idempotency import state_dir
from ._journal import AuditLog, entry_time, log_path, parse_since, read_entries
from ._out import Out
from ._redact import scrub
from ._session import outputs
from ._skills import render, skill_file
from ._tools import tool_entries, tool_fields
from ._values import CommandPath, ExitCodeName

if TYPE_CHECKING:
    from ._app import App

DOCTOR_PATH = CommandPath("doctor")
DOCTOR_CHECKS_FAILED = "DOCTOR_CHECKS_FAILED"
_ANY_VERSION = r"(\d+(?:\.\d+)+)"


@dataclass(frozen=True, slots=True)
class DoctorArgs:
    """``doctor`` takes nothing"""


def register_doctor(app: App) -> CommandPath:
    @app.command(
        DOCTOR_PATH.value,
        description="Check the tool's dependencies, the programs its commands run, its state "
        "and config directories, and the app's own checks; exit 4 with "
        "DOCTOR_CHECKS_FAILED lists a fix for each failure",
        danger_level="safe",
        exit_codes=(),
        ordered=True,  # dependencies and checks are sorted by name here
        has_network_io=any(isinstance(c, Endpoint) for c in app.checks),
        examples=[("Check the environment before first use", f"{app.name} doctor")],
    )
    def doctor(args: DoctorArgs, ctx: Ctx) -> dict[str, object]:
        declared = {d.name: d for d in app.dependencies}
        found = {name: find(d.check_command, d.version_regex, ctx) for name, d in declared.items()}
        dependencies = [dependency_result(declared[n], found[n], ctx) for n in sorted(declared)]
        # Every dependency is a check too (REQ-O-026), at the highest minimum anything needs
        needed: dict[str, tuple[Version, list[str]]] = {
            name: (d.minimum, []) for name, d in declared.items()
        }
        for path, command in app.commands.items():
            for tool, minimum in command.required_tools.items():
                current, users = needed.get(tool, (minimum, []))
                needed[tool] = (max(current, minimum, key=lambda v: v.key), [*users, path.value])
        checks = _framework_checks(app, ctx)
        for tool in sorted(needed):
            minimum, users = needed[tool]
            dep = declared.get(tool)
            seen: Found = found[tool] if dep else find((tool, "--version"), _ANY_VERSION, ctx)
            fix = None if dep is None else dep.fix_command
            checks.append(tool_check(tool, minimum, seen, fix, users))
        checks += [_app_check(check, ctx) for check in app.checks]
        checks.sort(key=lambda c: str(c["name"]))
        report: dict[str, object] = {"checks": checks, "dependencies": dependencies}
        failed = [e for e in checks if not e["ok"]]
        if failed:
            raise CliExit(
                ExitCodeName("PRECONDITION"),
                f"{len(failed)} of {len(checks)} checks failed",
                code=DOCTOR_CHECKS_FAILED,
                context={"failed": sorted(str(e["name"]) for e in failed)},
                fix_required="Apply the fix listed for each failed check in data.checks",
                data=report,
            )
        return report

    return DOCTOR_PATH


def _app_check(check: CheckFn, ctx: Ctx) -> dict[str, object]:
    """One ``App(checks=)`` result; a failure without a fix would leave an agent stuck"""
    result = check(ctx)
    name = getattr(check, "__qualname__", type(check).__name__)
    if not isinstance(result, Check):
        raise CliExit(
            ExitCodeName("GENERAL_ERROR"),
            f"the doctor check {name} returned {type(result).__name__}, not a treaty.Check",
            code="INVALID_OUTPUT",
            context={"check": name},
        )
    if not result.ok and result.fix is None:
        raise CliExit(
            ExitCodeName("GENERAL_ERROR"),
            f"the doctor check {result.name!r} failed without a fix",
            code="INVALID_OUTPUT",
            context={"check": result.name},
            fix_required="give the failing treaty.Check a fix=, the shell command that resolves it",
        )
    return result.to_json()


def _framework_checks(app: App, ctx: Ctx) -> list[dict[str, object]]:
    """The state directory and the user config directory can be written (REQ-O-026)"""
    config = user_config(app.name, ctx.env)
    wanted = (
        ("state-dir", state_dir(app.name, app.state_dir, ctx.env)),
        ("config-dir", None if config is None else config.parent),
    )
    return [_writable(name, where).to_json() for name, where in wanted if where is not None]


def _writable(name: str, where: Path) -> Check:
    """``where`` can be made and written: its nearest existing ancestor is a writable
    directory"""
    found = where.absolute()
    while not found.exists() and found != found.parent:
        found = found.parent
    quoted = shlex.quote(str(found))
    if not found.is_dir():
        return Check(name, False, f"mv {quoted} {quoted}.bak", error=f"{found} is not a directory")
    if not os.access(found, os.W_OK):
        return Check(name, False, f"chmod u+w {quoted}", error=f"{found} is not writable")
    return Check(name, True)


CLEANUP_PATH = CommandPath("cleanup")
SCOPES: dict[str, frozenset[SideEffectType]] = {
    "all": frozenset({SideEffectType.TEMP, SideEffectType.CACHE, SideEffectType.LOG}),
    "temp": frozenset({SideEffectType.TEMP}),
    "cache": frozenset({SideEffectType.CACHE}),
    "logs": frozenset({SideEffectType.LOG}),
}
"""What ``cleanup --scope`` removes; ``credential`` and ``config`` paths never (REQ-O-027)"""


@dataclass(frozen=True, slots=True)
class CleanupArgs:
    dry_run: bool = Flag(default=False, description="List what would be removed; remove nothing")
    scope: Literal["all", "temp", "cache", "logs"] = Flag(
        default="all", description="Which declared side effects to remove; all is the other three"
    )
    min_age: int = Flag(
        default=0,
        description="Keep every path changed within this many seconds; they are listed "
        "under skipped",
    )

    def __post_init__(self) -> None:
        if self.min_age < 0:
            raise ParseError(
                "--min-age is a whole number of seconds, 0 or more",
                context={"flag": "min-age", "value": self.min_age},
            )


@dataclass(frozen=True, slots=True)
class Freed:
    path: str
    type: str
    bytes_freed: int


@dataclass(frozen=True, slots=True)
class Cleaned:
    effect: str
    total_bytes_freed: int
    skipped: list[str]
    """Paths in scope changed within ``--min-age`` seconds, left in place"""
    failed: list[str]
    """Paths it could not remove, such as one holding a read-only directory"""
    cleaned: list[Freed] = Out(sort_key="path")
    """Every path removed, with the bytes it held; empty in a dry run"""
    would_affect: Affects | None = None


def register_cleanup(app: App) -> CommandPath:
    @app.command(
        CLEANUP_PATH.value,
        description="Remove the temp, cache, and log paths the tool's commands declare in "
        "filesystem_side_effects, its caches, and the output files commands handed out",
        danger_level="destructive",
        exit_codes=(),
        examples=[
            ("See what would be removed", f"{app.name} cleanup --dry-run"),
            (
                "Remove temp files older than an hour",
                f"{app.name} cleanup --scope temp --min-age 3600 --confirm-destructive",
            ),
        ],
    )
    def cleanup(args: CleanupArgs, ctx: Ctx) -> Cleaned:
        kinds = SCOPES[args.scope]
        found = {p: kind for p, kind in inventory(app, ctx) if kind in kinds}
        now = time.time()
        sizes = {p: _measure(Path(p)) for p in sorted(found)}
        skipped = [p for p, (_, newest) in sizes.items() if now - newest < args.min_age]
        paths = [p for p in sizes if p not in skipped]
        if args.dry_run:
            summary = f"Removes {len(paths)} {args.scope} paths"
            affects = Affects(summary, tuple(paths), len(paths))
            return Cleaned("would_delete", 0, skipped, [], [], affects)
        failed = [p for p in paths if not _removed(Path(p))]
        if failed:
            ctx.warn(
                "CLEANUP_INCOMPLETE",
                f"{len(failed)} paths could not be removed; see data.failed",
                paths=failed,
            )
        freed = [Freed(p, found[p].value, sizes[p][0]) for p in paths if p not in failed]
        total = sum(f.bytes_freed for f in freed)
        return Cleaned("deleted" if freed else "noop", total, skipped, failed, freed)

    return CLEANUP_PATH


def _removed(target: Path) -> bool:
    """Remove a path, a symlink as the link itself; False when some of it stays"""
    try:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
    except OSError:
        return False
    return True


def inventory(app: App, ctx: Ctx) -> list[tuple[str, SideEffectType]]:
    """Every existing path the tool's side effects cover: the declared ones, the output
    files commands handed out, and the caches (REQ-C-011, REQ-F-043, REQ-O-018)"""
    found: dict[str, SideEffectType] = {}
    for _, effect, _, matches in declared(app, ctx.env.get("HOME")):
        found.update(dict.fromkeys(matches, effect.kind))
    if ctx._session is not None:
        # REQ-F-043: ctx.output_file files; running sessions remove their own
        found.update(
            dict.fromkeys((str(p) for p in outputs(ctx._session.root.path)), SideEffectType.TEMP)
        )
    for command_path, command in app.commands.items():
        where = None if command.cache is None else cache_dir(app.name, command_path.value, ctx.env)
        if where is not None and where.exists():
            found[str(where)] = SideEffectType.CACHE  # wherever XDG_CACHE_HOME put it
    return sorted(found.items())


def declared(
    app: App, home: str | None
) -> Iterator[tuple[CommandPath, SideEffect, str, list[str]]]:
    """Each declared side effect with its absolute glob and the paths it matches now, in
    native form (declarations use ``/``, which Windows would leave mixed with ``\\``);
    a ``~/`` one is left out without a home"""
    for path, command in sorted(app.commands.items(), key=lambda kv: kv[0].value):
        for effect in command.filesystem_side_effects:
            pattern = effect.pattern(home)
            if pattern is not None:
                where = glob.escape(pattern).replace("[*]", "*")
                found = glob.glob(where, include_hidden=True)
                matches = (os.path.normpath(m) for m in found if not _linked(m, pattern))
                yield path, effect, pattern, sorted(matches)


def _linked(match: str, pattern: str) -> bool:
    """Whether ``match`` was reached through a symlink a wildcard matched, or one below
    it: cleaning it would remove what the link points at. The declared prefix may hold
    links, such as ``/tmp`` on macOS; the match itself is removed as a link."""
    parts = Path(match).parts
    first = next((i for i, p in enumerate(Path(pattern).parts) if "*" in p), len(parts))
    return any(Path(*parts[: i + 1]).is_symlink() for i in range(first, len(parts) - 1))


def _measure(path: Path) -> tuple[int, float]:
    """The bytes of the files under ``path`` and its newest modification time, symlinks
    not followed"""
    top = path.lstat()
    if not stat.S_ISDIR(top.st_mode):
        return top.st_size, top.st_mtime
    size, newest = 0, top.st_mtime
    for root, dirs, files in os.walk(path):
        for name in (*dirs, *files):
            try:
                st = Path(root, name).lstat()
            except FileNotFoundError:  # removed by someone else since the walk listed it
                continue
            newest = max(newest, st.st_mtime)
            size += 0 if stat.S_ISDIR(st.st_mode) else st.st_size
    return size, newest


STATUS_PATH = CommandPath("status")


@dataclass(frozen=True, slots=True)
class StatusArgs:
    show_side_effects: bool = Flag(
        default=False,
        description="List every declared filesystem side effect with the paths it covers "
        "and their sizes",
    )
    show_state_files: bool = Flag(
        default=False,
        description="List the tool's state files: config, idempotency records, the audit "
        "log, and declared credential and config paths; values are never shown",
    )


def register_status(app: App) -> CommandPath:
    @app.command(
        STATUS_PATH.value,
        description="Show the tool's local state: side-effect paths with sizes, state files, "
        "and whether a credential is active; --show-config shows the settings",
        danger_level="safe",
        exit_codes=(),
        ordered=True,  # sorted by command, then declaration order
        examples=[
            ("Everything", f"{app.name} status"),
            ("What cleanup would remove", f"{app.name} status --show-side-effects"),
            ("The effective settings and their sources", f"{app.name} status --show-config"),
        ],
    )
    def status(args: StatusArgs, ctx: Ctx) -> dict[str, object]:
        both = not args.show_side_effects and not args.show_state_files
        report: dict[str, object] = {}
        if both or args.show_side_effects:
            report["side_effects"] = _side_effects(app, ctx)
        if both or args.show_state_files:
            report["state_files"] = _state_files(app, ctx)
            if app.credentials is not None:
                scopes = app.credentials.active_scopes(ctx)
                report["logged_in"] = scopes is not None and not isinstance(scopes, Expired)
                if isinstance(scopes, Expired):
                    report["token_expires"] = scopes.iso
        return report

    return STATUS_PATH


def _side_effects(app: App, ctx: Ctx) -> list[dict[str, object]]:
    """``status --show-side-effects`` (REQ-C-011, REQ-O-028)"""
    entries: list[dict[str, object]] = []
    for command_path, effect, pattern, matches in declared(app, ctx.env.get("HOME")):
        paths = [{"path": str(Path(m).resolve()), "bytes": _measure(Path(m))[0]} for m in matches]
        entry: dict[str, object] = {
            "command": command_path.value,
            "type": effect.type,
            "pattern": str(Path(pattern).resolve()),
            "paths": paths,
            "bytes": sum(int(p["bytes"]) for p in paths),  # type: ignore[call-overload]
        }
        if effect.clearable_with is not None:
            entry["clearable_with"] = effect.clearable_with
        entries.append(entry)
    return entries


def _state_files(app: App, ctx: Ctx) -> list[dict[str, object]]:
    """``status --show-state-files``: where the tool keeps state, never what it holds"""
    wanted: list[tuple[str, Path | None]] = [
        ("project config", local_config(app.name, ctx.cwd)),
        ("user config", user_config(app.name, ctx.env)),
        ("idempotency records", state_dir(app.name, app.state_dir, ctx.env)),
    ]
    if app.audit_log is not None:
        wanted.append(("audit log", log_path(app.audit_log, app.name, ctx.env)))
    for command_path, effect, _, matches in declared(app, ctx.env.get("HOME")):
        if effect.kind in (SideEffectType.CREDENTIAL, SideEffectType.CONFIG):
            purpose = f"{effect.type} of {command_path.value}"
            wanted += [(purpose, Path(m)) for m in matches]
    files: list[dict[str, object]] = []
    for purpose, where in wanted:
        if where is None:
            continue
        exists = where.exists()
        files.append(
            {
                "path": str(where.resolve()),
                "purpose": purpose,
                "exists": exists,
                "bytes": _measure(where)[0] if exists else 0,
            }
        )
    return files


CHANGELOG_PATH = CommandPath("changelog")


@dataclass(frozen=True, slots=True)
class ChangelogArgs:
    since: str | None = Flag(
        default=None,
        pattern_type="semver",
        description="Only versions after this one, such as the version a caller was built against",
    )


@dataclass(frozen=True, slots=True)
class Changelog:
    entries: list[ChangelogEntry] = Out(ordered=True)
    """Newest first"""


def register_changelog(app: App, entries: tuple[ChangelogEntry, ...]) -> CommandPath:
    @app.command(
        CHANGELOG_PATH.value,
        description="The history of schema changes, newest first: each version's added, "
        "removed, and changed field paths and whether it breaks callers",
        danger_level="safe",
        exit_codes=(),
        examples=[
            ("Every version", f"{app.name} changelog"),
            ("What changed since 1.0.0", f"{app.name} changelog --since 1.0.0"),
        ],
    )
    def changelog(args: ChangelogArgs, ctx: Ctx) -> Changelog:
        if args.since is None:
            return Changelog(list(entries))
        since = version_key(args.since)
        return Changelog([e for e in entries if version_key(e.version) > since])

    return CHANGELOG_PATH


GENERATE_SKILLS_PATH = CommandPath("generate-skills")


@dataclass(frozen=True, slots=True)
class GenerateSkillsArgs:
    output_dir: Path = Flag(
        default=Path("skills"), description="Directory to write the skill files to"
    )


@dataclass(frozen=True, slots=True)
class SkillFile:
    path: str
    type: str
    """``context`` for CONTEXT.md, ``skill`` for a command's file"""
    command: str | None
    format: str = "markdown"


@dataclass(frozen=True, slots=True)
class Skills:
    effect: str
    skills: list[SkillFile] = Out(sort_key="path")


def register_generate_skills(app: App) -> CommandPath:
    @app.command(
        GENERATE_SKILLS_PATH.value,
        description="Write agent skill files from the command schemas: CONTEXT.md and one "
        "SKILL-<command>.md per command, each with YAML frontmatter",
        danger_level="mutating",
        exit_codes=(),
        examples=[
            ("Write ./skills", f"{app.name} generate-skills"),
            (
                "Write them where Claude Code finds them",
                f"{app.name} generate-skills --output-dir .claude/skills/{app.name}",
            ),
        ],
    )
    def generate_skills(args: GenerateSkillsArgs, ctx: Ctx) -> Skills:
        where = (ctx.cwd / args.output_dir).resolve()
        files: list[SkillFile] = []
        changed = existed = 0
        paths = {skill_file(p): p for p in app.commands}
        rendered = render(app)
        try:
            where.mkdir(parents=True, exist_ok=True)
            for name, text in rendered.items():
                target = where / name
                old = target.read_text(encoding="utf-8") if target.is_file() else None
                existed += old is not None
                if old != text:
                    write_atomic(target, text, new_mode=0o644)
                    changed += 1
        except OSError as exc:
            raise CliExit(
                ExitCodeName("PRECONDITION"),
                f"cannot write the skill files to {where}: {exc.strerror or exc}",
                code="OUTPUT_DIR_UNWRITABLE",
                context={"output_dir": str(where)},
                fix_required="pass --output-dir a directory you can write",
            ) from None
        for name in rendered:
            target = where / name
            path = paths.get(name)
            kind = "context" if path is None else "skill"
            files.append(SkillFile(str(target), kind, None if path is None else path.value))
        effect = "noop" if not changed else "updated" if existed else "created"
        return Skills(effect, files)

    return GENERATE_SKILLS_PATH


MCP_VALIDATE_PATH = CommandPath("mcp-validate")
SCHEMA_DRIFT_DETECTED = "SCHEMA_DRIFT_DETECTED"


@dataclass(frozen=True, slots=True)
class McpValidateArgs:
    mcp_schema_file: Path = Flag(
        description="The MCP tool list to compare, as treaty-mcp module:app --list-tools writes it"
    )


@dataclass(frozen=True, slots=True)
class FieldDrift:
    command: str
    field: str | None
    """``input.<flag>`` or ``data.<field>``; None for a whole tool the CLI no longer has"""


@dataclass(frozen=True, slots=True)
class TypeDrift:
    command: str
    field: str
    cli_type: str
    mcp_type: str


@dataclass(frozen=True, slots=True)
class Drift:
    added: list[FieldDrift]
    """In the CLI schema, not the MCP one"""
    removed: list[FieldDrift]
    """In the MCP schema, not the CLI one"""
    changed: list[TypeDrift]
    missing_from_mcp: list[str]
    """CLI commands the MCP schema has no tool for"""


@dataclass(frozen=True, slots=True)
class McpValidation:
    drift: Drift


def register_mcp_validate(app: App) -> CommandPath:
    @app.command(
        MCP_VALIDATE_PATH.value,
        description="Compare a saved MCP tool list with the current command schemas; drift "
        "exits 1 with SCHEMA_DRIFT_DETECTED and the diff in data",
        danger_level="safe",
        exit_codes=("NOT_FOUND",),
        examples=[
            (
                "Check the committed tool list in CI",
                f"{app.name} mcp-validate --mcp-schema-file mcp.json",
            )
        ],
    )
    def mcp_validate(args: McpValidateArgs, ctx: Ctx) -> McpValidation:
        listed = _read_tools(ctx.cwd / args.mcp_schema_file)
        live = {e.name: e for e in tool_entries(app)}
        drift = Drift([], [], [], [])
        for name, entry in live.items():
            tool = listed.get(name)
            if tool is None:
                drift.missing_from_mcp.append(entry.path.value)
                continue
            cli = tool_fields(
                {"inputSchema": entry.input_schema, "outputSchema": entry.output_schema}
            )
            mcp = tool_fields(tool)
            where = entry.path.value
            drift.added.extend(FieldDrift(where, f) for f in sorted(set(cli) - set(mcp)))
            drift.removed.extend(FieldDrift(where, f) for f in sorted(set(mcp) - set(cli)))
            drift.changed.extend(
                TypeDrift(where, f, cli[f], mcp[f])
                for f in sorted(set(cli) & set(mcp))
                if cli[f] != mcp[f]
            )
        drift.removed.extend(FieldDrift(name, None) for name in sorted(set(listed) - set(live)))
        result = McpValidation(drift)
        if drift.added or drift.removed or drift.changed or drift.missing_from_mcp:
            raise CliExit(
                ExitCodeName("GENERAL_ERROR"),
                "the MCP tool list differs from the CLI's command schemas",
                code=SCHEMA_DRIFT_DETECTED,
                context={"mcp_schema_file": str(args.mcp_schema_file)},
                fix_required=f"regenerate it: treaty-mcp <module>:app --list-tools > "
                f"{args.mcp_schema_file}",
                data=result,
            )
        return result

    return MCP_VALIDATE_PATH


def _read_tools(path: Path) -> dict[str, dict[str, object]]:
    """The tools of an MCP ``tools/list`` result or ``treaty-mcp --list-tools`` file"""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CliExit(
            ExitCodeName("NOT_FOUND"),
            f"no MCP schema file at {path}",
            context={"mcp_schema_file": str(path)},
        ) from None
    except (OSError, ValueError) as exc:
        raise CliExit(
            ExitCodeName("PRECONDITION"),
            f"{path} cannot be read as JSON: {exc}",
            code="MCP_SCHEMA_INVALID",
            context={"mcp_schema_file": str(path)},
        ) from None
    tools = loaded.get("tools") if isinstance(loaded, dict) else None
    if not isinstance(tools, list) or not all(
        isinstance(t, dict) and isinstance(t.get("name"), str) for t in tools
    ):
        raise CliExit(
            ExitCodeName("PRECONDITION"),
            f"{path} has no tools list of named tools",
            code="MCP_SCHEMA_INVALID",
            context={"mcp_schema_file": str(path)},
            fix_required="write it with treaty-mcp <module>:app --list-tools",
        )
    return {t["name"]: t for t in tools}


AUDIT_LOG_PATH = CommandPath("audit-log")
AUDIT_LOG_OFF = "AUDIT_LOG_OFF"
AUDIT_LINES_UNREADABLE = "AUDIT_LINES_UNREADABLE"


@dataclass(frozen=True, slots=True)
class AuditLogArgs:
    since: str | None = Flag(
        default=None,
        description="Only entries from this long ago, such as 30m, 1h, or 2d, or since this "
        "ISO 8601 time",
    )
    command: str | None = Flag(
        default=None, description="Only entries of this command, such as deploy.rollback"
    )
    trace_id: str | None = Flag(default=None, description="Only entries with this trace ID")
    limit: int | None = Flag(
        default=None, description="Most entries to return, the newest; default every entry"
    )

    def __post_init__(self) -> None:
        if self.since is not None:
            parse_since(self.since, dt.datetime.now(dt.UTC))  # a bad value exits 2
        if self.limit is not None and self.limit < 1:
            raise ParseError(
                "--limit is a whole number of at least 1",
                context={"flag": "limit", "value": self.limit},
            )


def register_audit_log(app: App, settings: AuditLog) -> CommandPath:
    @app.command(
        AUDIT_LOG_PATH.value,
        description="Query the audit log: one entry per invocation, oldest first, with its "
        "parameters (secrets redacted), exit code, duration, and trace, request, and "
        "session ids",
        danger_level="safe",
        exit_codes=(),
        streaming=True,
        examples=[
            ("Invocations of the past hour", f"{app.name} audit-log --since 1h --format jsonl"),
            ("One trace", f"{app.name} audit-log --trace-id abc123"),
        ],
    )
    def audit_log(args: AuditLogArgs, ctx: Ctx) -> Iterator[dict[str, object]]:
        path = log_path(settings, app.name, ctx.env)
        if path is None:
            raise CliExit(
                ExitCodeName("PRECONDITION"),
                "the audit log is off for this run",
                code=AUDIT_LOG_OFF,
                fix_required="unset the tool's AUDIT_LOG variable, or set it to a path, "
                "or set HOME or XDG_DATA_HOME",
            )
        since = None if args.since is None else parse_since(args.since, dt.datetime.now(dt.UTC))
        wanted = None if args.command is None else ".".join(args.command.split())
        kept: deque[dict[str, object]] = deque(maxlen=args.limit)
        unreadable = 0
        for entry in read_entries(path):
            if entry is None:
                unreadable += 1
                continue
            if wanted is not None and entry.get("command") != wanted:
                continue
            if args.trace_id is not None and entry.get("trace_id") != args.trace_id:
                continue
            if since is not None and ((when := entry_time(entry)) is None or when < since):
                continue
            # Redacted again: a line may predate the declaration that made a field secret
            cleaned = scrub("", entry)
            assert isinstance(cleaned, dict)
            kept.append(cleaned)
        if unreadable:
            ctx.warn(
                AUDIT_LINES_UNREADABLE,
                f"{unreadable} lines of the audit log are not JSON objects and were skipped",
                count=unreadable,
                path=str(path),
            )
        yield from kept

    return AUDIT_LOG_PATH
