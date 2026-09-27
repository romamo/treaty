"""Built-ins every app gets that yield to an app command of the same name (13-D1):
``doctor`` (REQ-O-031, REQ-C-018) and ``cleanup`` (REQ-C-011)."""

from __future__ import annotations

import glob
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ._cache import cache_dir
from ._context import Ctx
from ._declare import CLEARED
from ._deps import Found, Version, dependency_result, find, tool_check
from ._effect import Affects
from ._errors import CliExit
from ._flags import Flag
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
