"""Built-ins every app gets that yield to an app command of the same name (13-D1):
``doctor`` (REQ-O-031, REQ-C-018)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._context import Ctx
from ._deps import Found, Version, dependency_result, find, tool_check
from ._errors import CliExit
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
