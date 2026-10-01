import io
import json
import sys
import threading
import time
from dataclasses import dataclass

import pytest
from conftest import spec_validator

from treaty import App, Ctx, Flag, NoArgs, RegistrationError, Timeout
from treaty._timeout import Heartbeat, TimeoutExpired, call_with_timeout


@dataclass(frozen=True, slots=True)
class SleepArgs:
    seconds: float = Flag(default=0.0, description="How long to block")


def make_app(default_timeout: float | None = 0.05) -> App:
    app = App("slowctl", version="1.0.0", default_timeout=default_timeout)

    @app.command(
        "fetch",
        description="Blocks then returns",
        has_network_io=True,
        danger_level="safe",
        exit_codes=(),
    )
    def fetch(args: SleepArgs, ctx: Ctx) -> dict[str, object]:
        time.sleep(args.seconds)
        return {"slept": args.seconds, "timeout_s": ctx.timeout.seconds}

    @app.command(
        "quick", description="Own short limit", timeout=0.02, danger_level="safe", exit_codes=()
    )
    def quick(args: SleepArgs, ctx: Ctx) -> dict[str, object]:
        time.sleep(args.seconds)
        return {"timeout_s": ctx.timeout.seconds}

    return app


def run_json(app: App, argv: list[str]) -> tuple[int, dict]:
    out = io.StringIO()
    code = app.run(argv, stdout=out, stderr=io.StringIO(), env={}, isatty=False)
    envelope = json.loads(out.getvalue())
    spec_validator("response-envelope").validate(envelope)
    return code, envelope


def test_timeout_emits_envelope_and_exit_10() -> None:
    code, env = run_json(make_app(), ["fetch", "--seconds", "0.5"])
    assert code == 10 and env["ok"] is False
    # fetch is read-only, so its timeout changed nothing and may be retried (REQ-C-014)
    assert env["error"]["code"] == "TIMEOUT" and env["error"]["retryable"] is True
    assert env["error"]["context"]["timeout_ms"] == 50
    assert env["meta"]["timeout_ms"] == 50 and env["meta"]["duration_ms"] >= 50


def test_timeout_flag_overrides_default_and_reaches_ctx() -> None:
    code, env = run_json(make_app(), ["fetch", "--timeout", "1", "--seconds", "0.1"])
    assert code == 0 and env["data"]["timeout_s"] == 1.0 and env["meta"]["timeout_ms"] == 1000


def test_timeout_zero_disables_limit() -> None:
    code, env = run_json(make_app(), ["fetch", "--timeout=0", "--seconds", "0.1"])
    assert code == 0 and env["data"]["timeout_s"] is None and env["meta"]["timeout_ms"] is None


def test_per_command_timeout_beats_app_default() -> None:
    app = make_app(default_timeout=5)
    code, env = run_json(app, ["quick", "--seconds", "0.2"])
    assert code == 10 and env["meta"]["timeout_ms"] == 20


def test_timeout_flag_rejected_on_non_network_command() -> None:
    code, env = run_json(make_app(), ["quick", "--timeout", "1"])
    assert code == 2 and env["error"]["context"]["flag"] == "timeout"


@pytest.mark.parametrize("raw", ["-1", "abc", "nan"])
def test_bad_timeout_values(raw: str) -> None:
    code, env = run_json(make_app(), ["fetch", "--timeout", raw])
    assert code == 2


def test_manifest_advertises_timeout_flag_only_for_network_commands() -> None:
    commands = make_app().manifest()["commands"]
    assert commands["fetch"]["flags"]["timeout"]["type"] == "number"
    assert "timeout" not in commands["quick"]["flags"]
    spec_validator("manifest-response").validate(make_app().manifest())


def test_exec_line_honors_timeout_override() -> None:
    app = make_app()
    out = io.StringIO()
    stdin = io.StringIO(
        '{"_cmd": "fetch", "seconds": 0.1, "_opts": {"timeout": 1}}\n'
        '{"_cmd": "fetch", "seconds": 0.5}\n'
    )
    code = app.run(["exec"], stdin=stdin, stdout=out, stderr=io.StringIO(), env={}, isatty=False)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert code == 1 and lines[0]["ok"] is True and lines[1]["error"]["code"] == "TIMEOUT"


def unbounded_app() -> App:
    """``play`` and ``child`` run unbounded and ``long`` longer than the default, all
    mutating and none on the network; ``short`` declares a limit under the default"""
    app = App("checks", version="1.0.0", default_timeout=30)

    @app.command(
        "play", description="Run a playbook", timeout=None, danger_level="mutating", exit_codes=()
    )
    def play(args: SleepArgs, ctx: Ctx) -> dict[str, object]:
        time.sleep(args.seconds)
        return {"effect": "noop", "timeout_s": ctx.timeout.seconds, "remaining": ctx.remaining}

    @app.command(
        "long", description="Run long", timeout=600, danger_level="mutating", exit_codes=()
    )
    def long(args: SleepArgs, ctx: Ctx) -> dict[str, object]:
        return {"effect": "noop", "timeout_s": ctx.timeout.seconds}

    @app.command(
        "short", description="Run briefly", timeout=10, danger_level="mutating", exit_codes=()
    )
    def short(args: SleepArgs, ctx: Ctx) -> dict[str, object]:
        return {"effect": "noop", "timeout_s": ctx.timeout.seconds}

    @app.command(
        "child", description="Run a child", timeout=None, danger_level="mutating", exit_codes=()
    )
    def child(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        ctx.run([sys.executable, "-c", "import time; time.sleep(5)"])
        return {"effect": "noop"}

    return app


def test_an_unbounded_command_takes_timeout_and_ends_in_timeout() -> None:
    """Issue 69: timeout=None let no caller bound one run; --timeout is its deadline"""
    code, env = run_json(unbounded_app(), ["play", "--seconds", "5", "--timeout", "0.2"])
    assert code == 10 and env["error"]["code"] == "TIMEOUT"
    assert env["meta"]["timeout_ms"] == 200 and env["meta"]["duration_ms"] < 5000
    code, env = run_json(unbounded_app(), ["play", "--timeout", "3"])
    assert code == 0 and env["data"]["timeout_s"] == 3.0 and 0 < env["data"]["remaining"] <= 3
    code, env = run_json(unbounded_app(), ["play"])
    assert code == 0 and env["data"] == {"effect": "noop", "timeout_s": None, "remaining": None}


def test_the_timeout_of_an_unbounded_command_stops_its_child() -> None:
    started = time.monotonic()
    code, env = run_json(unbounded_app(), ["child", "--timeout", "0.3"])
    assert code == 10 and env["error"]["code"] == "TIMEOUT"
    assert time.monotonic() - started < 4


def test_timeout_is_offered_past_the_app_default_only() -> None:
    code, env = run_json(unbounded_app(), ["long", "--timeout", "1"])
    assert code == 0 and env["data"]["timeout_s"] == 1.0
    code, env = run_json(unbounded_app(), ["short", "--timeout", "1"])
    assert code == 2 and env["error"]["context"]["flag"] == "timeout"
    code, env = run_json(unbounded_app(), ["play", "--timeout", "-1"])
    assert code == 2


def test_the_manifest_lists_timeout_on_unbounded_and_long_commands() -> None:
    app = unbounded_app()
    flags = {name: c["flags"] for name, c in app.manifest()["commands"].items()}
    assert {c for c in ("play", "long", "short") if "timeout" in flags[c]} == {"play", "long"}
    assert "proxy" not in flags["play"] and "no-proxy" not in flags["play"]
    spec_validator("manifest-response").validate(app.manifest())


def test_an_exec_line_bounds_an_unbounded_command() -> None:
    out = io.StringIO()
    stdin = io.StringIO('{"_cmd": "play", "seconds": 5, "_opts": {"timeout": 0.2}}\n')
    app = unbounded_app()
    code = app.run(["exec"], stdin=stdin, stdout=out, stderr=io.StringIO(), env={}, isatty=False)
    assert code == 1 and json.loads(out.getvalue())["error"]["code"] == "TIMEOUT"


@dataclass(frozen=True, slots=True)
class OwnTimeout:
    timeout: float = Flag(default=5.0, description="Own limit")


def test_an_unbounded_commands_own_timeout_field_keeps_the_flag() -> None:
    """An app that had --timeout on an unbounded command keeps it: the framework's yields
    there, and the value reaches the field, not the deadline"""
    app = App("checks", version="1.0.0")

    @app.command("wait", description="Wait", timeout=None, danger_level="safe", exit_codes=())
    def wait(args: OwnTimeout, ctx: Ctx) -> dict[str, object]:
        return {"own": args.timeout, "timeout_s": ctx.timeout.seconds}

    code, env = run_json(app, ["wait", "--timeout", "7"])
    assert code == 0 and env["data"] == {"own": 7.0, "timeout_s": None}
    assert env["meta"]["timeout_ms"] is None
    assert app.manifest()["commands"]["wait"]["flags"]["timeout"]["description"] == "Own limit"


def test_a_timeout_field_on_a_network_command_is_still_refused() -> None:
    app = App("checks", version="1.0.0")
    with pytest.raises(RegistrationError, match="drop --timeout"):

        @app.command(
            "wait", description="Wait", has_network_io=True, danger_level="safe", exit_codes=()
        )
        def wait(args: OwnTimeout, ctx: Ctx) -> dict[str, object]:
            return {}


def test_call_with_timeout_reraises_handler_exception() -> None:
    def boom() -> None:
        raise ValueError("inner")

    with pytest.raises(ValueError, match="inner"):
        call_with_timeout(boom, Timeout(1))
    with pytest.raises(TimeoutExpired):
        call_with_timeout(lambda: time.sleep(1), Timeout(0.01))


def test_the_handler_runs_only_once_its_worker_is_registered() -> None:
    """``running`` has the worker before the handler runs, so ``ended`` never comes
    first for a handler that returns at once: what ``running`` keeps for it, such as the
    redacting log handler, would then be kept with no thread left to release it (#128)"""
    order: list[str] = []
    ran = threading.Event()

    def fn() -> None:
        order.append("fn")
        ran.set()

    def running(pending: object) -> None:
        # Long enough for an unregistered handler to run and end; the fixed order times out
        ran.wait(timeout=0.2)
        order.append("running")

    call_with_timeout(fn, Timeout(5), running, ended=lambda worker: order.append("ended"))
    assert order == ["running", "fn", "ended"]


def test_a_late_wake_skips_the_missed_heartbeats_rather_than_bursting() -> None:
    # Issue #56: a wait that wakes several intervals late ticked once per missed
    # interval in the same instant; the clock jumps 5.5 intervals to stall the wait
    interval = 0.05
    offset = [0.0]
    read = threading.Event()

    beats: list[float] = []
    beat = threading.Event()

    def clock() -> float:
        now = time.monotonic() + offset[0]
        read.set()
        return now

    def tick() -> None:
        beats.append(clock())
        beat.set()

    def handler() -> None:
        read.wait()  # the waiting thread has read its start time
        offset[0] += 5.5 * interval
        # Issue #121: wait for the late wake itself, not a fixed sleep a loaded runner
        # can outlast; then long enough for a burst, if one came back, to show
        assert beat.wait(5), "the waiting thread never woke to send a heartbeat"
        time.sleep(3 * interval)

    call_with_timeout(
        handler,
        Timeout(None),
        heartbeats=[Heartbeat(interval, tick)],
        clock=clock,
    )
    assert beats
    gaps = [b - a for a, b in zip(beats, beats[1:], strict=False)]
    assert all(gap >= interval / 2 for gap in gaps), gaps


def test_handler_exception_becomes_crash_envelope_with_traceback() -> None:
    app = App("x", version="1.0.0")

    @app.command("crash", description="Raises", danger_level="safe", exit_codes=())
    def crash(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        raise ValueError("handler bug")

    out, err = io.StringIO(), io.StringIO()
    code = app.run(["crash"], stdout=out, stderr=err, env={}, isatty=False)
    env = json.loads(out.getvalue())
    assert code == 1 and env["error"]["code"] == "HANDLER_CRASHED"
    assert env["error"]["message"] == "Command crash raised ValueError: handler bug."
    assert "Traceback" in err.getvalue() and "handler bug" in err.getvalue()


def remaining_app(default_timeout: float | None) -> tuple[App, list[bool]]:
    app = App("slowctl", version="1.0.0", default_timeout=default_timeout)
    seen: list[bool] = []

    @app.command("left", description="Reports its time left", danger_level="safe", exit_codes=())
    def left(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"remaining": ctx.remaining, "expired": ctx.expired}

    @app.command(
        "drain", description="Works while time is left", danger_level="safe", exit_codes=()
    )
    def drain(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        done = 0
        while (ctx.remaining or 0) > 0.2:
            time.sleep(0.005)
            done += 1
        return {"done": done}

    @app.command("overrun", description="Sleeps past its limit", danger_level="safe", exit_codes=())
    def overrun(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        time.sleep(0.5)  # ten times the limit, so a slow runner still times out first
        seen.append(ctx.expired)
        return {}

    return app, seen


def test_ctx_remaining_counts_down_from_the_timeout() -> None:
    code, env = run_json(remaining_app(5)[0], ["left"])
    assert code == 0 and env["data"]["expired"] is False
    assert 4 < env["data"]["remaining"] <= 5


def test_ctx_remaining_is_none_without_a_limit() -> None:
    code, env = run_json(remaining_app(None)[0], ["left"])
    assert code == 0 and env["data"] == {"remaining": None, "expired": False}


def test_ctx_remaining_lets_a_handler_return_its_work_before_the_limit() -> None:
    code, env = run_json(remaining_app(0.5)[0], ["drain"])
    assert code == 0 and env["data"]["done"] > 0


def test_ctx_expired_is_true_once_the_timeout_passes() -> None:
    app, seen = remaining_app(0.05)
    code, env = run_json(app, ["overrun"])
    assert code == 10 and env["error"]["code"] == "TIMEOUT"
    deadline = time.monotonic() + 5.0
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == [True]  # the abandoned worker sees its deadline has passed
