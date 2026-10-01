"""A declared secret passed to ``ctx.warn`` is redacted from ``warnings`` on every path (#162)."""

import io
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from treaty import App, Ctx, Envelope, Flag
from treaty._mcp import call_tool, tool_entries

SECRET = "Zq7-supersecret-value-91"
ENV = {"PROBE_TOKEN": SECRET}


@dataclass(frozen=True, slots=True)
class GoArgs:
    token: str = Flag(description="API token", secret=True)


@dataclass(frozen=True, slots=True)
class Out:
    n: int


def probe_app() -> App:
    app = App("probe", version="1.0.0")

    @app.command("go", description="Go", danger_level="safe", exit_codes=(), output_file=True)
    def go(args: GoArgs, ctx: Ctx) -> Out:
        ctx.warn("TOK", f"bad token {args.token}", token=args.token, seen=[{"t": args.token}])
        return Out(1)

    @app.command("tail", description="Tail", danger_level="safe", exit_codes=(), streaming=True)
    def tail(args: GoArgs, ctx: Ctx) -> Iterator[Out]:
        for n in range(3):
            ctx.warn("TOK", f"bad token {args.token} at {n}", token=args.token)
            yield Out(n)

    return app


def run(argv: list[str], *, stdin: str = "", env: dict[str, str] | None = None) -> str:
    out = io.StringIO()
    probe_app().run(
        argv, stdin=io.StringIO(stdin), stdout=out, stderr=io.StringIO(), env=env or ENV
    )
    return out.getvalue()


def assert_redacted(warnings: object) -> None:
    """The warning is there, keys and all, with the secret value replaced"""
    assert isinstance(warnings, list) and warnings
    text = json.dumps(warnings)
    assert SECRET not in text
    assert warnings[0]["message"].startswith("bad token [REDACTED]")
    assert warnings[0]["context"]["token"] == "[REDACTED]"


def test_the_issue_repro_in_the_json_envelope() -> None:
    envelope = json.loads(run(["go", "--token-from-env", "PROBE_TOKEN", "--format", "json"]))
    assert envelope["ok"] is True and envelope["data"] == {"n": 1}
    assert envelope["warnings"] == [
        {
            "code": "TOK",
            "context": {"seen": [{"t": "[REDACTED]"}], "token": "[REDACTED]"},
            "message": "bad token [REDACTED]",
        }
    ]


def test_jsonl() -> None:
    [line] = run(["go", "--token-from-env", "PROBE_TOKEN", "--format", "jsonl"]).splitlines()
    assert_redacted(json.loads(line)["warnings"])


@pytest.mark.parametrize("extra", [["--stream"], ["--no-stream"]])
def test_stream_events_and_the_buffered_stream(extra: list[str]) -> None:
    out = run(["tail", "--token-from-env", "PROBE_TOKEN", *extra])
    assert SECRET not in out
    for line in out.splitlines():
        warnings = json.loads(line)["warnings"]
        assert warnings, line
        assert_redacted(warnings)


def test_exec_lines() -> None:
    plan = json.dumps({"_cmd": "go", "token_from_env": "PROBE_TOKEN"}) + "\n"
    lines = run(["exec"], stdin=plan * 2).splitlines()
    assert len(lines) == 2
    for line in lines:
        assert_redacted(json.loads(line)["warnings"])


def test_app_call_and_mcp_results() -> None:
    app = probe_app()
    called = app.call("go", {"token_from_env": "PROBE_TOKEN"}, env=ENV)
    tool = call_tool(app, {e.name: e for e in tool_entries(app)}, "go", {}, env=ENV)
    for envelope in (called, tool):
        assert isinstance(envelope, Envelope)
        assert_redacted(envelope.to_json()["warnings"])
    assert tool.ok  # the MCP tool read the secret from its default variable


def test_output_file_and_audit_log(tmp_path: Path) -> None:
    target = tmp_path / "r.json"
    env = {**ENV, "XDG_STATE_HOME": str(tmp_path / "state"), "PROBE_AUDIT_LOG": "1"}
    out = run(["go", "--token-from-env", "PROBE_TOKEN", "--output", str(target)], env=env)
    # The file gets the data; the envelope describing the write carries the warnings
    assert json.loads(target.read_text()) == {"n": 1}
    assert_redacted(json.loads(out)["warnings"])
    [log] = (tmp_path / "state").rglob("*.jsonl")
    assert SECRET not in log.read_text() and json.loads(log.read_text())["warnings"] == ["TOK"]


def test_short_secret_follows_the_minimum_and_keys_stay() -> None:
    """A secret under MIN_REDACTED (4) is not replaced, as in error messages: it would
    garble every word sharing its letters; a key named like a credential keeps its
    unrelated value, as stdout is never redacted by name (REQ-F-034)"""
    app = App("probe", version="1.0.0")

    @app.command("go", description="Go", danger_level="safe", exit_codes=())
    def go(args: GoArgs, ctx: Ctx) -> Out:
        ctx.warn("TOK", f"pin {args.token}", pin=args.token, api_key="public-id")
        return Out(1)

    envelope = app.call("go", {"token_from_env": "T"}, env={"T": "abc"})
    assert envelope.warnings[0].message == "pin abc"
    assert envelope.warnings[0].context == {"api_key": "public-id", "pin": "abc"}


@pytest.mark.parametrize("argv", [["go"], ["tail", "--stream"], ["tail", "--no-stream"]])
def test_ndjson_warning_lines_on_stderr(argv: list[str]) -> None:
    out, err = io.StringIO(), io.StringIO()
    probe_app().run(
        [*argv, "--token-from-env", "PROBE_TOKEN", "--format", "ndjson"],
        stdin=io.StringIO(),
        stdout=out,
        stderr=err,
        env=ENV,
    )
    assert SECRET not in out.getvalue() + err.getvalue()
    lines = [json.loads(line) for line in err.getvalue().splitlines()]
    warnings = [line for line in lines if line.get("code") == "TOK"]
    assert_redacted(warnings)
