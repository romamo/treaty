import io
import json
import time
from dataclasses import dataclass

import pytest
from conftest import spec_validator

from treaty import App, Ctx, Flag, NoArgs, Timeout
from treaty._timeout import TimeoutExpired, call_with_timeout


@dataclass(frozen=True, slots=True)
class SleepArgs:
    seconds: float = Flag(default=0.0, description="How long to block")


def make_app(default_timeout: float | None = 0.05) -> App:
    app = App("slowctl", version="1", default_timeout=default_timeout)

    @app.command("fetch", description="Blocks then returns", has_network_io=True)
    def fetch(args: SleepArgs, ctx: Ctx) -> dict[str, object]:
        time.sleep(args.seconds)
        return {"slept": args.seconds, "timeout_s": ctx.timeout.seconds}

    @app.command("quick", description="Own short limit", timeout=0.02)
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
    assert env["error"]["code"] == "TIMEOUT" and env["error"]["retryable"] is False
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


def test_call_with_timeout_reraises_handler_exception() -> None:
    def boom() -> None:
        raise ValueError("inner")

    with pytest.raises(ValueError, match="inner"):
        call_with_timeout(boom, Timeout(1))
    with pytest.raises(TimeoutExpired):
        call_with_timeout(lambda: time.sleep(1), Timeout(0.01))


def test_handler_exception_becomes_crash_envelope_with_traceback() -> None:
    app = App("x", version="1")

    @app.command("crash", description="Raises")
    def crash(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        raise ValueError("handler bug")

    out, err = io.StringIO(), io.StringIO()
    code = app.run(["crash"], stdout=out, stderr=err, env={}, isatty=False)
    env = json.loads(out.getvalue())
    assert code == 1 and env["error"]["code"] == "HANDLER_CRASHED"
    assert env["error"]["message"] == "crash raised ValueError: handler bug"
    assert "Traceback" in err.getvalue() and "handler bug" in err.getvalue()
