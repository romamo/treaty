"""A dry run treaty switches on rebuilds the args in phase 1, which reruns
``__post_init__``; whatever it raises is answered as at parse time, before the handler
starts: ``ParseError`` exit 2, anything else ``HANDLER_CRASHED`` exit 1 (#161)."""

import io
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from treaty import Affects, App, Arg, Ctx, Envelope, Flag, ParseError
from treaty._mcp import call_tool
from treaty._tools import tool_entries

RAN: list[str] = []
REFUSAL = "--force makes no sense with --dry-run"


@dataclass(frozen=True, slots=True)
class WipeArgs:
    force: bool = Flag(default=False, description="Skip the safety check")
    strict: bool = Flag(default=False, description="Refuse a dry run with ValueError")
    typed: bool = Flag(default=False, description="Refuse a dry run with TypeError")
    dry_run: bool = Flag(default=False, description="Preview only")

    def __post_init__(self) -> None:
        if self.force and self.dry_run:
            raise ParseError(REFUSAL)
        if self.strict and self.dry_run:
            raise ValueError("--strict makes no sense with --dry-run")
        if self.typed and self.dry_run:
            raise TypeError("--typed makes no sense with --dry-run")


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
            raise ParseError("target must stay relative to the project")


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
    assert [e["message"] for e in error["errors"]] == [message]
    assert RAN == []


def assert_crashed(code: int, envelope: dict[str, object], exception: str) -> None:
    assert code == 1
    assert envelope["ok"] is False
    error = envelope["error"]
    assert isinstance(error, dict)
    assert error["code"] == "HANDLER_CRASHED"
    assert error["context"]["exception"] == exception
    assert RAN == []


def assert_refused_envelope(envelope: Envelope) -> None:
    assert envelope.exit_code == 2
    assert envelope.error is not None and envelope.error.code == "ARG_ERROR"
    assert envelope.error.phase == "validation"
    assert [e["message"] for e in envelope.error.errors] == [REFUSAL]
    assert RAN == []


@pytest.mark.parametrize("safe_default", [False, True])
def test_issue_repro_value_error_answers_as_an_explicit_dry_run_does(safe_default: bool) -> None:
    """A ValueError is a bug in __post_init__ at parse time; a forced dry run agrees"""
    app = wipe_app(safe_default=safe_default)
    code, out = run(app, ["wipe", "--strict", "--format", "json"])
    assert_crashed(code, out[-1], "ValueError")
    explicit_code, explicit = run(app, ["wipe", "--strict", "--dry-run"])
    assert_crashed(explicit_code, explicit[-1], "ValueError")
    assert out[-1]["error"] == explicit[-1]["error"]


@pytest.mark.parametrize("safe_default", [False, True])
def test_a_type_error_from_the_rebuild_is_a_crash_envelope(safe_default: bool) -> None:
    code, out = run(wipe_app(safe_default=safe_default), ["wipe", "--typed"])
    assert_crashed(code, out[-1], "TypeError")


def test_destructive_preview_parse_error_is_exit_2_naming_confirm_destructive() -> None:
    code, out = run(wipe_app(), ["wipe", "--force"])
    assert_refused(code, out[-1])
    error = out[-1]["error"]
    assert isinstance(error, dict) and "--confirm-destructive" in str(error["suggestion"])


def test_safe_default_parse_error_is_exit_2_naming_live() -> None:
    code, out = run(wipe_app(safe_default=True), ["wipe", "--force"])
    assert_refused(code, out[-1])
    meta = out[-1]["meta"]
    assert isinstance(meta, dict) and meta["dry_run"] is True
    error = out[-1]["error"]
    assert isinstance(error, dict) and "--live" in str(error["suggestion"])


def test_the_runs_that_need_no_rebuild_are_unchanged() -> None:
    for flags in (["--force"], ["--strict"], ["--typed"]):
        code, out = run(wipe_app(), ["wipe", *flags, "--confirm-destructive"])
        assert code == 0 and out[-1]["data"]["effect"] == "deleted"  # type: ignore[index]
        code, out = run(wipe_app(safe_default=True), ["wipe", *flags, "--live"])
        assert code == 0 and out[-1]["data"]["effect"] == "deleted"  # type: ignore[index]
    code, out = run(wipe_app(), ["wipe"])
    assert code == 2 and out[-1]["error"]["code"] == "CONFIRMATION_REQUIRED"  # type: ignore[index]
    assert RAN == ["wipe"]
    code, out = run(wipe_app(safe_default=True), ["wipe"])
    assert code == 0 and out[-1]["meta"]["dry_run"] is True  # type: ignore[index]
    assert out[-1]["data"]["effect"] == "would_delete"  # type: ignore[index]


@pytest.mark.parametrize("safe_default", [False, True])
def test_validate_only_answers_the_dry_run_rebuild(safe_default: bool) -> None:
    app = wipe_app(safe_default=safe_default)
    code, out = run(app, ["wipe", "--force", "--validate-only"])
    assert_refused(code, out[-1])
    code, out = run(app, ["wipe", "--strict", "--validate-only"])
    assert_crashed(code, out[-1], "ValueError")


def test_exec_line_answers_the_dry_run_rebuild() -> None:
    code, out = run(wipe_app(), ["exec"], '{"_cmd": "wipe", "force": true}\n')
    assert code == 1  # the plan's own exit: a line failed
    assert_refused(2, out[0])
    code, out = run(wipe_app(), ["exec"], '{"_cmd": "wipe", "typed": true}\n')
    assert_crashed(code, out[0], "TypeError")


def test_app_call_answers_the_dry_run_rebuild() -> None:
    RAN.clear()
    assert_refused_envelope(wipe_app().call("wipe", {"force": True}))
    assert_refused_envelope(wipe_app(safe_default=True).call("wipe", {"force": True}))
    crashed = wipe_app().call("wipe", {"strict": True})
    assert crashed.exit_code == 1
    assert crashed.error is not None and crashed.error.code == "HANDLER_CRASHED"
    assert RAN == []


def test_mcp_tool_call_answers_the_dry_run_rebuild() -> None:
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
