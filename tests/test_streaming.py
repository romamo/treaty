"""Streaming handlers: a generator yields one bare item line per event, then one terminal
line (REQ-O-004, D-10)."""

import io
import json
import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import needs_posix_signals, spec_validator

from treaty import App, Arg, Ctx, Exit, Flag, Format, NoArgs, ParseError, RegistrationError

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
    app = App("logctl", version="1.0.0", default_timeout=0.3)
    app.exit_code("NO_SPACE", 80, description="Disk full", retryable=False, side_effects="none")
    extra = {} if timeout == "inherit" else {"timeout": timeout}

    @app.command(
        "tail",
        description="Emit events",
        streaming=True,
        danger_level="safe",
        exit_codes=["NO_SPACE"],
        renderers={Format.PLAIN: lambda e: f"[{e['n']}] {e['text']}\n"},
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
    argv: list[str], *, app: App | None = None, stdin: str = "", plain: bool = False
) -> tuple[int, list[dict], str]:
    CLOSED.clear()
    out, err = io.StringIO(), io.StringIO()
    code = (app or stream_app()).run(
        argv, stdin=io.StringIO(stdin), stdout=out, stderr=err, env={}, isatty=plain
    )
    if plain:
        return code, [], out.getvalue() + err.getvalue()
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    return code, lines, err.getvalue()


# Wire format


def test_each_event_is_a_bare_numbered_item_line_and_the_stream_ends_with_a_summary() -> None:
    code, lines, _ = run(["tail", "3"])
    assert code == 0
    assert lines[:3] == [
        {"n": 1, "text": "line 1", "_seq": 1},
        {"n": 2, "text": "line 2", "_seq": 2},
        {"n": 3, "text": "line 3", "_seq": 3},
    ]
    summary = lines[3]
    assert summary["_summary"] is True and summary["_count"] == 3 and summary["total"] == 3
    assert summary["pagination"] == {
        "total": 3,
        "returned": 3,
        "truncated": False,
        "has_more": False,
        "next_cursor": None,
    }
    assert summary["exit_code"] == 0 and summary["command"] == "tail"
    assert "seq" not in summary and "end" not in summary and "ok" not in summary
    assert len(lines) == 4
    assert CLOSED == ["tail"]


def test_jsonl_writes_the_same_lines_as_json() -> None:
    _, lines, _ = run(["tail", "2", "--format", "jsonl"])
    assert [line.get("_seq") for line in lines] == [1, 2, None]
    assert lines[-1]["_summary"] is True


def test_empty_stream_is_only_the_summary_line() -> None:
    code, lines, _ = run(["tail", "0"])
    assert code == 0
    (summary,) = lines
    assert summary["_summary"] is True and summary["total"] == 0 and summary["_count"] == 0


def test_a_cleanup_failure_after_the_stream_is_on_its_summary_line() -> None:
    # An end-of-stream warning stays on the terminal line, not stderr alone (D-9, #389)
    app = App("cleanctl", version="1.0.0")

    def cleanup() -> None:
        raise OSError("gone")

    @app.command("tail", description="d", streaming=True, danger_level="safe", exit_codes=(),
                 cleanup=cleanup)  # fmt: skip
    def tail(args: NoArgs, ctx: Ctx) -> Iterator[Event]:
        yield Event(1, "x")

    code, lines, _ = run(["tail"], app=app)
    assert code == 0 and lines[0] == {"n": 1, "text": "x", "_seq": 1}
    summary = lines[-1]
    assert summary["_summary"] is True and len(lines) == 2
    [warning] = summary["warnings"]
    assert warning["code"] == "CLEANUP_FAILED" and warning["context"]["hook"] == "cleanup"


def test_validate_only_answers_one_envelope_not_a_summary_line() -> None:
    # Nothing streams under --validate-only: REQ-O-009's answer is the envelope
    code, lines, _ = run(["tail", "2", "--validate-only"])
    assert code == 0
    (envelope,) = lines
    spec_validator("response-envelope").validate(envelope)
    assert envelope["ok"] is True and envelope["meta"]["validation_only"] is True
    assert "_summary" not in envelope


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
    assert lines[0]["error"]["errors"][0]["message"] == "'no-stream' takes no value."


def test_plain_mode_renders_each_event_and_nothing_for_the_end() -> None:
    code, _, text = run(["tail", "2"], plain=True)
    assert code == 0
    assert text == "[1] line 1\n[2] line 2\n"


# Failures


def test_cli_exit_mid_stream_keeps_delivered_events_and_marks_partial() -> None:
    code, lines, _ = run(["tail", "5", "--fail-at", "3"])
    assert code == 80
    assert [line["_seq"] for line in lines[:2]] == [1, 2]
    last = lines[-1]
    assert len(lines) == 3
    spec_validator("response-envelope").validate(last)
    assert last["ok"] is False and last["error"]["code"] == "NO_SPACE"
    assert last["meta"]["exit_code"] == 80
    assert last["meta"]["items_emitted"] == 2
    assert last["meta"]["partial"] is True
    assert "seq" not in last["meta"]
    assert CLOSED == ["tail"]


def test_parse_error_mid_stream_is_validation_after_start() -> None:
    code, lines, _ = run(["tail", "5", "--bad-at", "2"])
    assert code == 1
    assert lines[-1]["error"]["code"] == "VALIDATION_AFTER_START"
    assert lines[-1]["meta"]["items_emitted"] == 1


def test_no_stream_failure_keeps_the_events_seen_so_far() -> None:
    code, lines, _ = run(["tail", "5", "--fail-at", "3", "--no-stream"])
    assert code == 80
    assert lines[0]["data"] == [{"n": 1, "text": "line 1"}, {"n": 2, "text": "line 2"}]
    assert lines[0]["error"]["code"] == "NO_SPACE"
    assert lines[0]["meta"]["partial"] is True
    assert lines[0]["meta"]["total"] == 2


def test_plain_mode_failure_goes_to_stderr_after_rendered_events() -> None:
    code, _, text = run(["tail", "5", "--fail-at", "2"], plain=True)
    assert code == 80
    assert text.startswith("[1] line 1\n")
    assert "logctl: NO_SPACE: Disk full." in text


# Timeouts


def test_f011_streams_inherit_the_default_as_an_idle_limit() -> None:
    """0.1 s between events, four events: longer than the 0.3 s limit in all, never idle
    past it, with room for a slow runner"""
    app = stream_app()
    code, lines, _ = run(["tail", "4", "--sleep", "0.1"], app=app)
    assert code == 0 and len(lines) == 5
    assert lines[-1]["timeout_ms"] == 300
    assert app.manifest()["commands"]["tail"]["streaming_default"] is True


def test_f011_a_stream_idle_past_its_timeout_ends_with_timeout() -> None:
    app = stream_app(timeout=0.25)
    code, lines, _ = run(["tail", "3", "--sleep", "0.5"], app=app)
    assert code == 10
    assert lines[-1]["error"]["code"] == "TIMEOUT"
    assert "next event" in lines[-1]["error"]["message"]
    assert lines[-1]["meta"]["items_emitted"] == 0
    assert lines[-1]["meta"]["timeout_ms"] == 250


def test_no_stream_timeout_is_a_deadline_for_the_whole_stream() -> None:
    app = stream_app(timeout=0.25)
    code, lines, _ = run(["tail", "10", "--sleep", "0.1", "--no-stream"], app=app)
    (line,) = lines
    assert code == 10 and line["error"]["code"] == "TIMEOUT"
    assert 1 <= len(line["data"]) <= 3


# Stream lines (REQ-O-004, REQ-F-069, D-10)


@dataclass(frozen=True, slots=True)
class Count:
    upto: int = Arg(description="Events to emit")
    fail: bool = Flag(default=False, description="Fail after the events")
    interrupt: bool = Flag(default=False, description="Cancel after the events")


@dataclass(frozen=True, slots=True)
class OwnSeq:
    _seq: int


def lines_app() -> App:
    app = App("lines", version="1.0.0")
    app.exit_code("BROKEN", 80, description="Broken", retryable=False, side_effects="none")

    def after(args: Count) -> None:
        if args.fail:
            raise Exit.BROKEN("broken")
        if args.interrupt:
            raise KeyboardInterrupt  # what SIGINT raises in the handler's thread

    @app.command(
        "objects", description="d", streaming=True, danger_level="safe", exit_codes=["BROKEN"]
    )
    def objects(args: Count, ctx: Ctx) -> Iterator[Event]:
        for n in range(1, args.upto + 1):
            yield Event(n, f"line {n}")
        after(args)

    @app.command(
        "maps", description="d", streaming=True, danger_level="safe", exit_codes=["BROKEN"]
    )
    def maps(args: Count, ctx: Ctx) -> Iterator[dict[str, int]]:
        for n in range(1, args.upto + 1):
            yield {"n": n}
        after(args)

    @app.command(
        "aobjects", description="d", streaming=True, danger_level="safe", exit_codes=["BROKEN"]
    )
    async def aobjects(args: Count, ctx: Ctx) -> AsyncIterator[Event]:
        for n in range(1, args.upto + 1):
            yield Event(n, f"line {n}")
        after(args)

    @app.command(
        "amaps", description="d", streaming=True, danger_level="safe", exit_codes=["BROKEN"]
    )
    async def amaps(args: Count, ctx: Ctx) -> AsyncIterator[dict[str, int]]:
        for n in range(1, args.upto + 1):
            yield {"n": n}
        after(args)

    @app.command("own", description="d", streaming=True, danger_level="safe", exit_codes=())
    def own(args: Count, ctx: Ctx) -> Iterator[OwnSeq]:
        for n in range(1, args.upto + 1):
            yield OwnSeq(n * 10)

    return app


NUMBERED = pytest.mark.parametrize("command", ["objects", "aobjects"])
UNNUMBERED = pytest.mark.parametrize("command", ["maps", "amaps"])


def items(lines: list[dict]) -> list[dict]:
    return [{k: v for k, v in line.items() if k != "_seq"} for line in lines]


@NUMBERED
def test_a_numbered_stream_numbers_its_items_and_counts_them_on_the_summary(command: str) -> None:
    code, lines, _ = run([command, "2"], app=lines_app())
    assert code == 0
    assert lines[:2] == [
        {"n": 1, "text": "line 1", "_seq": 1},
        {"n": 2, "text": "line 2", "_seq": 2},
    ]
    assert lines[2]["_summary"] is True and lines[2]["_count"] == 2 and lines[2]["total"] == 2
    assert len(lines) == 3


@UNNUMBERED
def test_an_unnumbered_stream_writes_bare_items_and_a_summary_without_count(command: str) -> None:
    code, lines, _ = run([command, "2"], app=lines_app())
    assert code == 0
    assert lines[:2] == [{"n": 1}, {"n": 2}]
    assert lines[2]["_summary"] is True and lines[2]["total"] == 2 and "_count" not in lines[2]


@NUMBERED
def test_a_numbered_stream_that_fails_ends_on_the_error_envelope_with_items_emitted(
    command: str,
) -> None:
    code, lines, _ = run([command, "2", "--fail"], app=lines_app())
    assert code == 80
    assert [line["_seq"] for line in lines[:2]] == [1, 2]
    last = lines[2]
    spec_validator("response-envelope").validate(last)
    assert last["ok"] is False and last["error"]["code"] == "BROKEN"
    assert last["meta"]["exit_code"] == 80 and last["meta"]["items_emitted"] == 2
    assert not any("_summary" in line for line in lines) and len(lines) == 3


@UNNUMBERED
def test_an_unnumbered_stream_that_fails_has_no_items_emitted(command: str) -> None:
    code, lines, _ = run([command, "1", "--fail"], app=lines_app())
    assert code == 80
    assert lines[0] == {"n": 1}
    assert lines[1]["ok"] is False and "items_emitted" not in lines[1]["meta"]


@NUMBERED
def test_a_stream_failing_before_its_first_item_has_items_emitted_0(command: str) -> None:
    code, lines, _ = run([command, "0", "--fail"], app=lines_app())
    assert code == 80
    (last,) = lines
    assert last["meta"]["items_emitted"] == 0


@pytest.mark.parametrize("command", ["objects", "aobjects", "maps", "amaps"])
def test_a_cancelled_stream_ends_on_req_f_069s_cancellation_object(command: str) -> None:
    code, lines, _ = run([command, "2", "--interrupt"], app=lines_app())
    assert code == 130
    assert items(lines[:2]) == items(run([command, "2"], app=lines_app())[1][:2])
    last = lines[2]
    spec_validator("response-envelope").validate(last)
    assert last["ok"] is False and last["data"] == {"partial": True}
    assert last["error"]["code"] == "CANCELLED"
    assert last["error"]["context"]["signal"] == "SIGINT"
    assert last["meta"]["exit_code"] == 130
    assert len(lines) == 3


def test_an_item_type_with_its_own_seq_is_not_numbered() -> None:
    code, lines, _ = run(["own", "2"], app=lines_app())
    assert code == 0
    assert lines[:2] == [{"_seq": 10}, {"_seq": 20}]
    assert "_count" not in lines[2]


def test_app_call_and_no_stream_keep_the_buffered_envelope() -> None:
    envelope = lines_app().call("objects", {"upto": 2})
    assert envelope.data == [{"n": 1, "text": "line 1"}, {"n": 2, "text": "line 2"}]
    _, (line,), _ = run(["objects", "2", "--no-stream"], app=lines_app())
    assert line["ok"] is True and line["data"] == envelope.data


def test_an_events_warnings_go_to_stderr_as_json_lines() -> None:
    app = App("warns", version="1.0.0")

    @app.command("w", description="d", streaming=True, danger_level="safe", exit_codes=())
    def w(args: NoArgs, ctx: Ctx) -> Iterator[Event]:
        ctx.warn("SLOW", "the source is slow")
        yield Event(1, "x")

    code, lines, err = run(["w"], app=app)
    assert code == 0
    assert lines[0] == {"n": 1, "text": "x", "_seq": 1} and lines[1]["_summary"] is True
    warnings = [json.loads(line) for line in err.splitlines() if line.startswith("{")]
    assert [w["code"] for w in warnings] == ["SLOW"]


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
    code, _, text = run(["tail", "--help"], plain=True)
    assert code == 0
    assert (
        "Streams one JSON object per event, then a _summary line; --no-stream returns a single "
        "envelope" in text
    )


# Signals


def read_lines(proc: subprocess.Popen[str], count: int, *, deadline: float) -> str:
    """Read `count` or more stdout lines from the pipe, failing with stderr past the deadline"""
    assert proc.stdout is not None and proc.stderr is not None
    buffer = b""
    while buffer.count(b"\n") < count:
        remaining = deadline - time.monotonic()
        ready, _, _ = select.select([proc.stdout], [], [], max(remaining, 0))
        chunk = os.read(proc.stdout.fileno(), 4096) if ready else b""
        if not chunk:
            proc.kill()
            _, err = proc.communicate()
            pytest.fail(f"{count} lines never arrived; stdout {buffer!r}, stderr:\n{err}")
        buffer += chunk
    return buffer.decode()


@needs_posix_signals
def test_sigint_during_a_stream_ends_it_with_cancelled_after_the_events() -> None:
    proc = subprocess.Popen(
        [sys.executable, str(SLOWCTL), "serve", "--interval", "0.05"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # the listening event and a heartbeat: the handler runs, so treaty's SIGINT handler is in
    head = read_lines(proc, 2, deadline=time.monotonic() + 10)
    proc.send_signal(signal.SIGINT)
    out, err = proc.communicate(timeout=5)
    assert proc.returncode == 130, err
    lines = [json.loads(line) for line in (head + out).splitlines()]
    assert lines[0]["event"] == "listening"
    assert len(lines) >= 3
    last = lines[-1]
    spec_validator("response-envelope").validate(last)
    # REQ-F-069's cancellation object
    assert last["ok"] is False and last["data"] == {"partial": True}
    assert last["error"]["code"] == "CANCELLED"
    assert last["error"]["context"]["signal"] == "SIGINT"
    assert last["meta"]["exit_code"] == 130
    # dict events may hold any key, _seq too, so the stream is not numbered
    assert not any("_seq" in line for line in lines) and "items_emitted" not in last["meta"]
    assert "serve: server closed" in err


# Registration


def register(match: str, **meta: object) -> None:
    app = App("logctl", version="1.0.0")
    with pytest.raises(RegistrationError, match=match):

        @app.command("x", description="x", streaming=True, exit_codes=(), **meta)  # type: ignore[arg-type]
        def handler(args: TailArgs, ctx: Ctx) -> Iterator[Event]:
            yield Event(1, "x")


def test_streaming_requires_an_iterator_annotation() -> None:
    app = App("logctl", version="1.0.0")
    with pytest.raises(RegistrationError, match=r"annotated Iterator\[T\]"):

        @app.command("x", description="x", streaming=True, danger_level="safe", exit_codes=())
        def handler(args: TailArgs, ctx: Ctx) -> Event:
            return Event(1, "x")


def test_streaming_events_must_be_payloads() -> None:
    app = App("logctl", version="1.0.0")
    with pytest.raises(RegistrationError, match="each yielded event must serialize"):

        @app.command("x", description="x", streaming=True, danger_level="safe", exit_codes=())
        def handler(args: TailArgs, ctx: Ctx) -> Iterator[int]:
            yield 1


def test_streaming_commands_cannot_be_destructive() -> None:
    register("cannot ask confirmation for each action", danger_level="destructive")


def test_iterator_annotation_without_streaming_is_refused() -> None:
    app = App("logctl", version="1.0.0")
    with pytest.raises(RegistrationError, match="return type must serialize"):

        @app.command("x", description="x", danger_level="safe", exit_codes=())
        def handler(args: TailArgs, ctx: Ctx) -> Iterator[Event]:
            yield Event(1, "x")


# endless=True (#389)


def endless_app() -> App:
    app = App("logctl", version="1.0.0")

    @app.command(
        "follow",
        description="Follow the log",
        streaming=True,
        endless=True,
        danger_level="safe",
        exit_codes=(),
    )
    def follow(args: NoArgs, ctx: Ctx) -> Iterator[Event]:
        yield Event(1, "x")

    return app


def test_endless_needs_streaming() -> None:
    app = App("logctl", version="1.0.0")
    with pytest.raises(RegistrationError, match="declare streaming=True or drop endless=True"):

        @app.command("x", description="x", endless=True, danger_level="safe", exit_codes=())
        def handler(args: NoArgs, ctx: Ctx) -> Event:
            return Event(1, "x")


def test_endless_shows_in_schema_help_and_skills_only() -> None:
    from treaty._skills import render

    app = endless_app()
    manifest = app.manifest()
    spec_validator("manifest-response").validate(manifest)
    # Not a ManifestResponse key: the manifest entry stays the spec's
    assert "endless" not in manifest["commands"]["follow"]
    out = io.StringIO()
    assert app.run(["follow", "--schema"], stdout=out, stderr=io.StringIO(), env={}) == 0
    assert json.loads(out.getvalue())["data"]["endless"] is True
    code, _, text = run(["follow", "--help"], app=app, plain=True)
    assert code == 0 and "Runs until interrupted" in text
    assert "Endless stream" in render(app)["SKILL-follow.md"]
    # A stream without it publishes nothing new
    out = io.StringIO()
    assert stream_app().run(["tail", "--schema"], stdout=out, stderr=io.StringIO(), env={}) == 0
    assert "endless" not in json.loads(out.getvalue())["data"]
    assert "Endless stream" not in render(stream_app())["SKILL-tail.md"]
