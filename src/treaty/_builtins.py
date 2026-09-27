"""Built-ins every app gets that yield to an app command of the same name (13-D1):
``doctor`` (REQ-O-031, REQ-C-018), ``cleanup`` (REQ-C-011), and ``audit-log``
(REQ-O-030)."""

from __future__ import annotations

import datetime as dt
import glob
import shutil
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ._cache import cache_dir
from ._context import Ctx
from ._declare import CLEARED
from ._deps import Found, Version, dependency_result, find, tool_check
from ._effect import Affects
from ._errors import CliExit, ParseError
from ._flags import Flag
from ._journal import AuditLog, entry_time, log_path, parse_since, read_entries
from ._redact import scrub
from ._session import outputs
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
        description="Check the tool's dependencies and the programs its commands run; "
        "exit 4 with DOCTOR_CHECKS_FAILED lists a fix for each failure",
        danger_level="safe",
        exit_codes=(),
        ordered=True,  # dependencies and checks are sorted by name here
        examples=[("Check the environment before first use", f"{app.name} doctor")],
    )
    def doctor(args: DoctorArgs, ctx: Ctx) -> dict[str, object]:
        declared = {d.name: d for d in app.dependencies}
        found = {name: find(d.check_command, d.version_regex, ctx) for name, d in declared.items()}
        dependencies = [dependency_result(declared[n], found[n], ctx) for n in sorted(declared)]
        needed: dict[str, tuple[Version, list[str]]] = {}
        for path, command in app.commands.items():
            for tool, minimum in command.required_tools.items():
                current, users = needed.get(tool, (minimum, []))
                needed[tool] = (max(current, minimum, key=lambda v: v.key), [*users, path.value])
        checks: list[dict[str, object]] = []
        for tool in sorted(needed):
            minimum, users = needed[tool]
            dep = declared.get(tool)
            seen: Found = found[tool] if dep else find((tool, "--version"), _ANY_VERSION, ctx)
            fix = None if dep is None else dep.fix_command
            checks.append(tool_check(tool, minimum, seen, fix, users))
        report: dict[str, object] = {"checks": checks, "dependencies": dependencies}
        failed = [e for e in (*checks, *dependencies) if not e["ok"]]
        if failed:
            total = len(checks) + len(dependencies)
            raise CliExit(
                ExitCodeName("PRECONDITION"),
                f"{len(failed)} of {total} checks failed",
                code=DOCTOR_CHECKS_FAILED,
                context={"failed": sorted(str(e["name"]) for e in failed)},
                fix_required="Apply the fix listed for each failed check in data.dependencies "
                "(fix_command) and data.checks (fix)",
                data=report,
            )
        return report

    return DOCTOR_PATH


CLEANUP_PATH = CommandPath("cleanup")


@dataclass(frozen=True, slots=True)
class CleanupArgs:
    dry_run: bool = Flag(default=False, description="List what would be removed; remove nothing")


@dataclass(frozen=True, slots=True)
class Cleaned:
    effect: str
    removed: list[str]
    """Every path removed; empty in a dry run"""
    would_affect: Affects | None = None


def register_cleanup(app: App) -> CommandPath:
    @app.command(
        CLEANUP_PATH.value,
        description="Remove the temp and cache paths the tool's commands declare in "
        "filesystem_side_effects, and the output files commands handed out",
        danger_level="destructive",
        exit_codes=(),
        examples=[("See what would be removed", f"{app.name} cleanup --dry-run")],
    )
    def cleanup(args: CleanupArgs, ctx: Ctx) -> Cleaned:
        found = _cleared(app, ctx.env.get("HOME"))
        if ctx.session is not None:
            # REQ-F-043: ctx.output_file files; running sessions remove their own
            found.update(str(p) for p in outputs(ctx.session.root.path))
        for command_path, command in app.commands.items():
            where = (
                None if command.cache is None else cache_dir(app.name, command_path.value, ctx.env)
            )
            if where is not None and where.exists():
                found.add(str(where))  # REQ-O-018: wherever XDG_CACHE_HOME put it
        paths = sorted(found)
        if args.dry_run:
            summary = f"Removes {len(paths)} temp and cache paths"
            return Cleaned("would_delete", [], Affects(summary, tuple(paths), len(paths)))
        for path in paths:
            target = Path(path)
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
        return Cleaned("deleted" if paths else "noop", paths)

    return CLEANUP_PATH


def _cleared(app: App, home: str | None) -> set[str]:
    """Every existing path a temp or cache side effect of a command covers (REQ-C-011)"""
    found: set[str] = set()
    for command in app.commands.values():
        for effect in command.filesystem_side_effects:
            pattern = effect.pattern(home) if effect.kind in CLEARED else None
            if pattern is not None:
                found.update(
                    glob.glob(glob.escape(pattern).replace("[*]", "*"), include_hidden=True)
                )
    return found


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
