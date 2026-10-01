"""A dry run treaty switches on rebuilds the args, which reruns ``__post_init__``: a
refusal there is a phase-1 validation error, exit 2, before the handler starts (#161)."""

import io
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from treaty import Affects, App, Arg, Ctx, Envelope, Flag
from treaty._mcp import call_tool
from treaty._tools import tool_entries

RAN: list[str] = []
REFUSAL = "--force makes no sense with --dry-run"


@dataclass(frozen=True, slots=True)
class WipeArgs:
    force: bool = Flag(default=False, description="Skip the safety check")
    dry_run: bool = Flag(default=False, description="Preview only")

    def __post_init__(self) -> None:
        if self.force and self.dry_run:
            raise ValueError(REFUSAL)


@dataclass(frozen=True, slots=True)
class Wiped:
    effect: str
    would_affect: Affects | None = None


@dataclass(frozen=True, slots=True)
class PruneArgs:
    target: Path = Arg(description="What to prune, relative to the project")
    dry_run: bool = Flag(default=False, description="Preview only")

    def __post_init__(self) -> None:
        if self.target.is_absolute():
            raise ValueError("target must stay relative to the project")


def wipe_app(*, safe_default: bool = False) -> App:
    app = App("probe", version="1.0.0")

    @app.command(
        "wipe",
        description="Wipe",
        danger_level="destructive",
        exit_codes=(),
        safe_default=safe_default,
    )
    def wipe(args: WipeArgs, _ctx: Ctx) -> Wiped:
        RAN.append("wipe")
        if args.dry_run:
            return Wiped("would_delete", Affects("Wipes everything", ("all",), 1))
        return Wiped("deleted")

    @app.command("prune", description="Prune", danger_level="mutating", exit_codes=())
    def prune(args: PruneArgs, _ctx: Ctx) -> Wiped:
        RAN.append("prune")
        return Wiped("updated")

    return app


def run(app: App, argv: list[str], stdin: str = "") -> tuple[int, list[dict[str, object]]]:
    RAN.clear()
    out = io.StringIO()
    code = app.run(
        argv, stdin=io.StringIO(stdin), stdout=out, stderr=io.StringIO(), env={}, isatty=False
    )
    return code, [json.loads(line) for line in out.getvalue().splitlines()]


def assert_refused(code: int, envelope: dict[str, object], message: str = REFUSAL) -> None:
    assert code == 2
    assert envelope["ok"] is False
    error = envelope["error"]
    assert isinstance(error, dict)
    assert error["code"] == "ARG_ERROR" and error["phase"] == "validation"
    assert [e["message"] for e in error["errors"]] == [message]  # type: ignore[index, union-attr]
    assert RAN == []


def assert_refused_envelope(envelope: Envelope) -> None:
    assert envelope.exit_code == 2
    assert envelope.error is not None and envelope.error.code == "ARG_ERROR"
    assert envelope.error.phase == "validation"
    assert [e["message"] for e in envelope.error.errors] == [REFUSAL]
    assert RAN == []


def test_issue_repro_destructive_preview_refusal_is_exit_2() -> None:
    code, out = run(wipe_app(), ["wipe", "--force", "--format", "json"])
    assert_refused(code, out[-1])
    error = out[-1]["error"]
    assert isinstance(error, dict) and "--confirm-destructive" in str(error["suggestion"])


def test_safe_default_dry_run_refusal_is_exit_2_with_meta_dry_run() -> None:
    code, out = run(wipe_app(safe_default=True), ["wipe", "--force"])
    assert_refused(code, out[-1])
    meta = out[-1]["meta"]
    assert isinstance(meta, dict) and meta["dry_run"] is True
    error = out[-1]["error"]
    assert isinstance(error, dict) and "--live" in str(error["suggestion"])


def test_the_runs_that_need_no_rebuild_are_unchanged() -> None:
    code, out = run(wipe_app(), ["wipe", "--force", "--confirm-destructive"])
    assert code == 0 and out[-1]["data"]["effect"] == "deleted"  # type: ignore[index]
    code, out = run(wipe_app(safe_default=True), ["wipe", "--force", "--live"])
    assert code == 0 and out[-1]["data"]["effect"] == "deleted"  # type: ignore[index]
    code, out = run(wipe_app(), ["wipe"])
    assert code == 2 and out[-1]["error"]["code"] == "CONFIRMATION_REQUIRED"  # type: ignore[index]
    assert RAN == ["wipe"]
    code, out = run(wipe_app(safe_default=True), ["wipe"])
    assert code == 0 and out[-1]["meta"]["dry_run"] is True  # type: ignore[index]
    assert out[-1]["data"]["effect"] == "would_delete"  # type: ignore[index]


def test_validate_only_reports_the_dry_run_refusal() -> None:
    code, out = run(wipe_app(), ["wipe", "--force", "--validate-only"])
    assert_refused(code, out[-1])
    code, out = run(wipe_app(safe_default=True), ["wipe", "--force", "--validate-only"])
    assert_refused(code, out[-1])


def test_exec_line_refusal_is_exit_2() -> None:
    code, out = run(wipe_app(), ["exec"], '{"_cmd": "wipe", "force": true}\n')
    assert code == 1  # the plan's own exit: a line failed
    assert_refused(2, out[0])


def test_app_call_refusal_is_exit_2() -> None:
    RAN.clear()
    assert_refused_envelope(wipe_app().call("wipe", {"force": True}))
    assert_refused_envelope(wipe_app(safe_default=True).call("wipe", {"force": True}))


def test_mcp_tool_call_refusal_is_exit_2() -> None:
    RAN.clear()
    app = wipe_app()
    entries = {e.name: e for e in tool_entries(app)}
    assert_refused_envelope(call_tool(app, entries, "wipe", {"force": True}))


def test_cwd_rebuild_refusal_is_exit_2(tmp_path: Path) -> None:
    message = "Target must stay relative to the project."
    code, out = run(wipe_app(), ["prune", "cache", "--cwd", str(tmp_path)])
    assert_refused(code, out[-1], message)
    plan = json.dumps({"_cmd": "prune", "target": "cache"}) + "\n"
    code, out = run(wipe_app(), ["exec", "--cwd", str(tmp_path)], plan)
    assert code == 1  # the plan's own exit: a line failed
    assert_refused(2, out[0], message)


@pytest.mark.parametrize("argv", [["wipe", "--force"], ["wipe", "--force", "--validate-only"]])
def test_post_init_runs_twice_at_most(argv: list[str]) -> None:
    """Parse once, rebuild once in phase 1: the handler path does not rebuild again"""
    seen: list[bool] = []

    @dataclass(frozen=True, slots=True)
    class Counted:
        dry_run: bool = Flag(default=False, description="Preview only")
        force: bool = Flag(default=False, description="Skip the safety check")

        def __post_init__(self) -> None:
            seen.append(self.dry_run)

    app = App("probe", version="1.0.0")

    @app.command("wipe", description="Wipe", danger_level="destructive", exit_codes=())
    def wipe(args: Counted, _ctx: Ctx) -> Wiped:
        return Wiped("would_delete", Affects("Wipes everything", ("all",), 1))

    run(app, argv)
    assert seen == [False, True]
