"""A numeric secret is redacted by value wherever a string secret is, not only inside
strings (#258): an ``int`` secret echoed as a number in ``Exit`` context, a warning, a
log field, the audit log, or an exec_fallback line's data reads ``[REDACTED]``."""

import io
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from treaty import App, Ctx, Envelope, Exit, Flag
from treaty._mcp import call_tool, tool_entries

PIN = 987654
ENV = {"PROBE_PIN": str(PIN)}


@dataclass(frozen=True, slots=True)
class PinArgs:
    pin: int = Flag(default=0, secret=True, description="Account PIN")
    account: int = Flag(default=0, description="Account number, not a secret")
    word: str = Flag(default="", secret=True, description="A string secret")


def probe_app() -> App:
    app = App("probe", version="1.0.0")

    @app.command("fail", description="Fail", danger_level="safe", exit_codes=("NOT_FOUND",))
    def fail(args: PinArgs, ctx: Ctx) -> dict[str, bool]:
        raise Exit.NOT_FOUND(
            "no such account",
            context={
                "pin": args.pin,
                "seen": [{"p": args.pin}, float(args.pin)],
                "note": f"pin {args.pin}",
                "small": 7,
                "zero": 0,
                "flag": True,
            },
        )

    @app.command("show", description="Show", danger_level="safe", exit_codes=())
    def show(args: PinArgs, ctx: Ctx) -> dict[str, bool]:
        ctx.warn("PIN_SEEN", "a pin was seen", pin=args.pin, count=3)
        ctx.log("checked", value=args.pin, count=3)
        logging.getLogger("probe.lib").debug("pin %d", args.pin)
        return {"ok": True}

    return app


def run(argv: list[str], *, stdin: str = "", env: Mapping[str, str] = ENV) -> tuple[str, str]:
    out, err = io.StringIO(), io.StringIO()
    probe_app().run(
        argv, stdin=io.StringIO(stdin), stdout=out, stderr=err, env=dict(env), isatty=False
    )
    return out.getvalue(), err.getvalue()


def assert_context_redacted(context: object) -> None:
    assert context == {
        "pin": "[REDACTED]",
        "seen": [{"p": "[REDACTED]"}, "[REDACTED]"],
        "note": "pin [REDACTED]",
        "small": 7,
        "zero": 0,
        "flag": True,
    }


def test_exit_context_under_main() -> None:
    out, err = run(["fail", "--pin-from-env", "PROBE_PIN"])
    assert str(PIN) not in out + err
    assert_context_redacted(json.loads(out)["error"]["context"])


def test_exit_context_in_exec_lines() -> None:
    plan = json.dumps({"_cmd": "fail", "pin_from_env": "PROBE_PIN"}) + "\n"
    out, _ = run(["exec", "--ignore-errors"], stdin=plan * 2)
    lines = out.splitlines()
    assert len(lines) == 2 and str(PIN) not in out
    for line in lines:
        assert_context_redacted(json.loads(line)["error"]["context"])


def test_app_call_and_mcp_command_tools() -> None:
    app = probe_app()
    called = app.call("fail", {"pin_from_env": "PROBE_PIN"}, env=ENV)
    tool = call_tool(app, {e.name: e for e in tool_entries(app)}, "fail", {}, env=ENV)
    for envelope in (called, tool):
        assert isinstance(envelope, Envelope) and envelope.error is not None
        assert str(PIN) not in json.dumps(envelope.to_json())
        assert_context_redacted(envelope.error.context)


def test_warning_and_log_fields_at_debug() -> None:
    out, err = run(["show", "--pin-from-env", "PROBE_PIN", "-vv"])
    assert str(PIN) not in out + err
    warning = json.loads(out)["warnings"][0]
    assert warning["context"] == {"pin": "[REDACTED]", "count": 3}
    lines = [json.loads(line) for line in err.splitlines()]
    [logged] = [line for line in lines if line["message"] == "checked"]
    assert logged["fields"] == {"value": "[REDACTED]", "count": 3}
    [library] = [line for line in lines if line["fields"].get("logger") == "probe.lib"]
    assert library["message"] == "pin [REDACTED]"


def test_audit_log_redacts_a_non_secret_argument_equal_to_the_secret(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    env = {**ENV, "PROBE_AUDIT_LOG": str(log)}
    run(["show", "--pin-from-env", "PROBE_PIN", "--account", str(PIN)], env=env)
    run(["show", "--pin-from-env", "PROBE_PIN", "--account", "12"], env=env)
    first, second = (json.loads(line) for line in log.read_text().splitlines())
    assert str(PIN) not in log.read_text()
    assert first["args"]["pin"] == "[REDACTED]" and first["args"]["account"] == "[REDACTED]"
    assert second["args"]["account"] == 12


def test_a_secret_under_the_minimum_length_stays() -> None:
    """A secret spelled in fewer than MIN_REDACTED (4) characters is not replaced, in a
    string or as a number: 12 would garble every count and id that equals it"""
    out, _ = run(["fail", "--pin-from-env", "P"], env={"P": "12"})
    context = json.loads(out)["error"]["context"]
    assert context["pin"] == 12 and context["note"] == "pin 12"


def test_a_bool_is_not_a_number_secret() -> None:
    """``True`` is not the string secret ``True`` nor the number 1"""
    out, _ = run(["fail", "--word-from-env", "W"], env={"W": "True"})
    assert json.loads(out)["error"]["context"]["flag"] is True


class Legacy:
    """An exec_fallback echoing its payload's numeric password"""

    def __call__(self, cmd: str, payload: Mapping[str, object]) -> object:
        return {"echo": payload["password"], "items": [payload["password"], 3]}


@pytest.mark.parametrize("password", [PIN, float(PIN)])
def test_exec_fallback_data_and_audit_log(tmp_path: Path, password: float) -> None:
    app = App("bean", version="1.0.0", exec_fallback=Legacy())
    log = tmp_path / "audit.jsonl"
    out = io.StringIO()
    line = json.dumps({"_cmd": "old", "password": password, "copy": password})
    app.run(
        ["exec"],
        stdin=io.StringIO(line + "\n"),
        stdout=out,
        stderr=io.StringIO(),
        env={"BEAN_AUDIT_LOG": str(log)},
        isatty=False,
    )
    data = json.loads(out.getvalue())["data"]
    # An array of numbers comes back in a stable order, so it is compared as a set
    assert data["echo"] == "[REDACTED]" and sorted(map(str, data["items"])) == ["3", "[REDACTED]"]
    entry = json.loads(log.read_text())
    assert entry["args"] == {"password": "[REDACTED]", "copy": "[REDACTED]"}


@pytest.mark.parametrize("echoed", [-PIN, PIN * 10 + 1, PIN + 0.5])
def test_a_number_spelling_the_secret_inside_it_is_redacted(echoed: float) -> None:
    """A number whose spelling holds the secret, such as its negation, reads
    ``[REDACTED]``, as the same text inside a string does: ``-987654`` hands out the PIN"""
    app = App("probe", version="1.0.0")

    @app.command("echo", description="Echo", danger_level="safe", exit_codes=())
    def echo(args: PinArgs, ctx: Ctx) -> dict[str, bool]:
        ctx.warn("SEEN", "a number was seen", value=echoed)
        return {"ok": True}

    out = io.StringIO()
    app.run(
        ["echo", "--pin-from-env", "PROBE_PIN"],
        stdin=io.StringIO(""),
        stdout=out,
        stderr=io.StringIO(),
        env=ENV,
        isatty=False,
    )
    assert str(PIN) not in out.getvalue()
    assert json.loads(out.getvalue())["warnings"][0]["context"]["value"] == "[REDACTED]"
