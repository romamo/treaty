"""Streaming handlers: a generator yields one envelope line per event (REQ-O-004)."""

import io
import json
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import spec_validator

from treaty import App, Arg, Ctx, Exit, Flag, ParseError, RegistrationError

SLOWCTL = Path(__file__).resolve().parents[1] / "examples" / "slowctl.py"
CLOSED: list[str] = []


@dataclass(frozen=True, slots=True)
class TailArgs:
    count: int = Arg(description="Events to emit")
    fail_at: int | None = Flag(default=None, description="Raise NO_SPACE after this many")
    bad_at: int | None = Flag(default=None, description="Raise ParseError after this many")
    sleep: float = Flag(default=0.0, description="Seconds between events")


@dataclass(frozen=True, slots=True)
class Event:
    n: int
    text: str


def stream_app(*, timeout: float | None | str = "inherit") -> App:
    app = App("logctl", version="1", default_timeout=0.3)
    app.exit_code("NO_SPACE", 80, description="Disk full", retryable=False, side_effects="none")
    extra = {} if timeout == "inherit" else {"timeout": timeout}

    @app.command(
        "tail",
        description="Emit events",
        streaming=True,
        exit_codes=["NO_SPACE"],
        human=lambda e: f"[{e['n']}] {e['text']}\n",
        **extra,  # type: ignore[arg-type]
    )
    def tail(args: TailArgs, ctx: Ctx) -> Iterator[Event]:
        try:
            for n in range(1, args.count + 1):
                if args.fail_at == n:
                    raise Exit.NO_SPACE("disk full", context={"after": n - 1})
                if args.bad_at == n:
                    raise ParseError("count too high", context={"flag": "count"})
                time.sleep(args.sleep)
                yield Event(n, f"line {n}")
        finally:
            CLOSED.append("tail")

    return app


def run(
    argv: list[str], *, app: App | None = None, stdin: str = "", human: bool = False
) -> tuple[int, list[dict], str]:
    CLOSED.clear()
    out, err = io.StringIO(), io.StringIO()
    code = (app or stream_app()).run(
        argv, stdin=io.StringIO(stdin), stdout=out, stderr=err, env={}, isatty=human
    )
    if human:
        return code, [], out.getvalue() + err.getvalue()
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    return code, lines, err.getvalue()


# Wire format


def test_each_event_is_an_envelope_line_and_the_stream_ends_with_a_terminal_one() -> None:
    code, lines, _ = run(["tail", "3"])
    assert code == 0
    assert len(lines) == 4
    for line in lines:
        spec_validator("response-envelope").validate(line)
    assert [line["data"] for line in lines[:3]] == [
        {"n": 1, "text": "line 1"},
        {"n": 2, "text": "line 2"},
        {"n": 3, "text": "line 3"},
    ]
    assert [line["meta"]["seq"] for line in lines[:3]] == [1, 2, 3]
    end = lines[3]
    assert end["ok"] is True and end["data"] is None
    assert end["meta"]["end"] is True and end["meta"]["total"] == 3 and end["meta"]["seq"] == 3
    assert len({line["meta"]["request_id"] for line in lines}) == 1
    assert CLOSED == ["tail"]


def test_empty_stream_is_only_the_terminal_envelope() -> None:
    code, lines, _ = run(["tail", "0"])
    assert code == 0
    assert len(lines) == 1
    assert lines[0]["meta"]["total"] == 0


def test_no_stream_buffers_events_into_one_envelope() -> None:
    code, lines, _ = run(["tail", "2", "--no-stream"])
    assert code == 0
    assert len(lines) == 1
    envelope = lines[0]
    spec_validator("response-envelope").validate(envelope)
    assert envelope["data"] == [{"n": 1, "text": "line 1"}, {"n": 2, "text": "line 2"}]
    assert envelope["meta"]["total"] == 2
    assert "seq" not in envelope["meta"] and "end" not in envelope["meta"]


def test_no_stream_takes_no_value() -> None:
    code, lines, _ = run(["tail", "2", "--no-stream=true"])
    assert code == 2
    assert lines[0]["error"]["errors"][0]["message"] == "'no-stream' takes no value"


def test_human_mode_renders_each_event_and_nothing_for_the_end() -> None:
    code, _, text = run(["tail", "2"], human=True)
    assert code == 0
    assert text == "[1] line 1\n[2] line 2\n"


# Failures


def test_cli_exit_mid_stream_keeps_delivered_events_and_marks_partial() -> None:
    code, lines, _ = run(["tail", "5", "--fail-at", "3"])
    assert code == 80
    assert [line["meta"]["seq"] for line in lines] == [1, 2, 2]
    last = lines[-1]
    assert last["error"]["code"] == "NO_SPACE"
    assert last["meta"]["partial"] is True
    assert CLOSED == ["tail"]


def test_parse_error_mid_stream_is_an_argument_error() -> None:
    code, lines, _ = run(["tail", "5", "--bad-at", "2"])
    assert code == 2
    assert lines[-1]["error"]["code"] == "ARG_ERROR"
    assert lines[-1]["meta"]["seq"] == 1


def test_no_stream_failure_keeps_the_events_seen_so_far() -> None:
    code, lines, _ = run(["tail", "5", "--fail-at", "3", "--no-stream"])
    assert code == 80
    assert lines[0]["data"] == [{"n": 1, "text": "line 1"}, {"n": 2, "text": "line 2"}]
    assert lines[0]["error"]["code"] == "NO_SPACE"
    assert lines[0]["meta"]["partial"] is True
    assert lines[0]["meta"]["total"] == 2


def test_human_mode_failure_goes_to_stderr_after_rendered_events() -> None:
    code, _, text = run(["tail", "5", "--fail-at", "2"], human=True)
    assert code == 80
    assert text.startswith("[1] line 1\n")
    assert "logctl: NO_SPACE: disk full" in text


# Timeouts


def test_streaming_commands_default_to_no_timeout() -> None:
    app = stream_app()
    code, lines, _ = run(["tail", "2", "--sleep", "0.2"], app=app)
    assert code == 0
    assert lines[0]["meta"]["timeout_ms"] is None
    assert app.manifest()["commands"]["tail"]["streaming_default"] is True


def test_explicit_timeout_is_a_deadline_for_the_whole_stream() -> None:
    app = stream_app(timeout=0.25)
    code, lines, _ = run(["tail", "10", "--sleep", "0.1"], app=app)
    assert code == 10
    assert lines[-1]["error"]["code"] == "TIMEOUT"
    assert lines[-1]["meta"]["partial"] is True
    assert 1 <= lines[-1]["meta"]["seq"] <= 3
    assert lines[-1]["meta"]["timeout_ms"] == 250


# Exec


def test_exec_writes_every_event_with_line_and_cmd_meta() -> None:
    plan = "\n".join(
        [
            json.dumps({"_cmd": "tail", "count": 2}),
            json.dumps({"_cmd": "tail", "count": 1, "_opts": {"no-stream": True}}),
        ]
    )
    code, lines, _ = run(["exec"], stdin=plan + "\n")
    assert code == 0
    assert [(line["meta"]["_line"], line["meta"].get("seq")) for line in lines] == [
        (1, 1),
        (1, 2),
        (1, 2),
        (2, None),
    ]
    assert all(line["meta"]["_cmd"] == "tail" for line in lines)
    assert lines[3]["data"] == [{"n": 1, "text": "line 1"}]


def test_exec_stops_after_a_failed_stream_without_ignore_errors() -> None:
    plan = "\n".join(
        [
            json.dumps({"_cmd": "tail", "count": 3, "_opts": {"fail-at": 2}}),
            json.dumps({"_cmd": "tail", "count": 1}),
        ]
    )
    code, lines, _ = run(["exec"], stdin=plan + "\n")
    assert code == 1
    assert [line["meta"]["_line"] for line in lines] == [1, 1]
    assert lines[-1]["error"]["code"] == "NO_SPACE"


# Manifest


def test_manifest_declares_streaming_default_and_no_stream_and_validates() -> None:
    manifest = stream_app().manifest()
    spec_validator("manifest-response").validate(manifest)
    entry = manifest["commands"]["tail"]
    assert entry["streaming_default"] is True
    assert entry["flags"]["no-stream"]["type"] == "boolean"
    assert entry["output_schema"]["title"] == "Event"
    assert "streaming_default" not in manifest["commands"]["version"]


def test_help_advertises_streaming() -> None:
    code, _, text = run(["tail", "--help"], human=True)
    assert code == 0
    assert "Streams one JSONL envelope per event; --no-stream returns a single envelope" in text


# Signals


def test_sigint_during_a_stream_ends_it_with_cancelled_after_the_events() -> None:
    proc = subprocess.Popen(
        [sys.executable, str(SLOWCTL), "serve", "--interval", "0.05"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.6)
    proc.send_signal(signal.SIGINT)
    out, err = proc.communicate(timeout=5)
    assert proc.returncode == 130, err
    lines = [json.loads(line) for line in out.splitlines()]
    assert lines[0]["data"]["event"] == "listening"
    assert len(lines) >= 3
    last = lines[-1]
    spec_validator("response-envelope").validate(last)
    assert last["error"]["code"] == "CANCELLED"
    assert last["meta"]["partial"] is True
    assert last["meta"]["seq"] == len(lines) - 1
    assert "serve: server closed" in err


# Registration


def register(match: str, **meta: object) -> None:
    app = App("logctl", version="1")
    with pytest.raises(RegistrationError, match=match):

        @app.command("x", description="x", streaming=True, **meta)  # type: ignore[arg-type]
        def handler(args: TailArgs, ctx: Ctx) -> Iterator[Event]:
            yield Event(1, "x")


def test_streaming_requires_an_iterator_annotation() -> None:
    app = App("logctl", version="1")
    with pytest.raises(RegistrationError, match=r"annotated Iterator\[T\]"):

        @app.command("x", description="x", streaming=True)
        def handler(args: TailArgs, ctx: Ctx) -> Event:
            return Event(1, "x")


def test_streaming_events_must_be_payloads() -> None:
    app = App("logctl", version="1")
    with pytest.raises(RegistrationError, match="each yielded event must serialize"):

        @app.command("x", description="x", streaming=True)
        def handler(args: TailArgs, ctx: Ctx) -> Iterator[int]:
            yield 1


def test_streaming_commands_must_be_safe() -> None:
    register("streaming commands must be safe", danger_level="mutating")


def test_iterator_annotation_without_streaming_is_refused() -> None:
    app = App("logctl", version="1")
    with pytest.raises(RegistrationError, match="return type must serialize"):

        @app.command("x", description="x")
        def handler(args: TailArgs, ctx: Ctx) -> Iterator[Event]:
            yield Event(1, "x")
