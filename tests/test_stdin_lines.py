"""Line-mode stdin, ``stdin_input="lines"`` (#33): lazy, no total cap, a cap per line."""

import io
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from fixture_lines_app import app

from treaty import App, Ctx, Flag, NoArgs, RegistrationError

LINECTL = Path(__file__).resolve().parent / "fixture_lines_app.py"


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def run(
    argv: list[str], stdin: io.TextIOBase | None = None, *, target: App = app
) -> tuple[int, list[dict]]:
    out = io.StringIO()
    code = target.run(
        [*argv, "--format", "json"],
        stdin=stdin if stdin is not None else io.StringIO(),
        stdout=out,
        stderr=io.StringIO(),
        env={},
        isatty=False,
    )
    return code, [json.loads(line) for line in out.getvalue().splitlines()]


def piped(data: bytes) -> io.TextIOWrapper:
    """A text stdin over bytes, as ``sys.stdin`` is over its pipe"""
    return io.TextIOWrapper(io.BytesIO(data), encoding="utf-8")


def test_lines_have_no_total_cap() -> None:
    # The repro of #33: 300 NDJSON lines of ~320 bytes pass the 64 KiB payload cap
    record = json.dumps({"isin": "IE00B3RBWM25", "pad": "x" * 300})
    text = "\n".join(record for _ in range(300)) + "\n"
    assert len(text) > 65_536
    target = App("big", version="1.0.0")

    @dataclass(frozen=True, slots=True)
    class Seen:
        lines: int

    @target.command("read", description="Count", danger_level="safe", exit_codes=(),
                    stdin_input="lines")  # fmt: skip
    def read(args: NoArgs, ctx: Ctx) -> Seen:
        return Seen(sum(1 for _ in ctx.stdin_lines))

    @target.command("whole", description="Count", danger_level="safe", exit_codes=(),
                    stdin_input=True)  # fmt: skip
    def whole(args: NoArgs, ctx: Ctx) -> Seen:
        return Seen(len((ctx.stdin_text or "").splitlines()))

    code, [envelope] = run(["read"], io.StringIO(text), target=target)
    assert code == 0 and envelope["data"] == {"lines": 300}
    code, [envelope] = run(["whole"], io.StringIO(text), target=target)
    assert code == 2 and envelope["error"]["code"] == "STDIN_TOO_LARGE"  # unchanged


def test_crlf_a_final_line_without_newline_and_a_bom() -> None:
    for stdin in (io.StringIO("﻿a\r\nb\nc"), piped(b"\xef\xbb\xbfa\r\nb\nc")):
        code, events = run(["upper"], stdin)
        assert code == 0
        assert [e["data"] for e in events[:-1]] == [
            {"n": 1, "text": "A"},
            {"n": 2, "text": "B"},
            {"n": 3, "text": "C"},
        ]
        assert events[-1]["meta"]["end"] is True


def test_empty_lines_are_kept_and_empty_input_has_none() -> None:
    code, [envelope] = run(["count"], io.StringIO("\n\nx\n"))
    assert code == 0 and envelope["data"] == {"lines": 3, "first": ""}
    code, [envelope] = run(["count"], io.StringIO(""))
    assert code == 0 and envelope["data"] == {"lines": 0, "first": None}


@pytest.mark.parametrize("stdin", [lambda d: io.StringIO(d.decode()), piped])
def test_a_line_over_the_cap_exits_1_with_its_number(stdin) -> None:  # type: ignore[no-untyped-def]
    exact = b"y" * 64
    code, [envelope] = run(["count"], stdin(b"a\n" + exact + b"\r\n" + exact))
    assert code == 0 and envelope["data"]["lines"] == 3
    code, [envelope] = run(["count"], stdin(b"a\nb\n" + b"z" * 65 + b"\nc\n"))
    error = envelope["error"]
    assert code == 1 and error["code"] == "LINE_TOO_LARGE"
    assert error["context"] == {"line": 3, "limit_bytes": 64, "source": "stdin"}
    assert error["phase"] == "execution"  # the handler ran: never exit 2 (REQ-F-002)


def test_a_stream_ends_with_the_line_error_after_its_events() -> None:
    code, events = run(["upper"], io.StringIO("a\n" + "z" * 65 + "\n"))
    assert code == 1 and events[0]["data"] == {"n": 1, "text": "A"}
    assert events[-1]["error"]["code"] == "LINE_TOO_LARGE"
    assert events[-1]["meta"]["partial"] is True


def test_a_line_that_is_not_utf8_names_its_line() -> None:
    # The bad byte is on line 3; a decoder reading ahead would blame line 1
    code, [envelope] = run(["count"], piped(b"a\nb\n\xff\xfe\nd\n"))
    error = envelope["error"]
    assert code == 1 and error["code"] == "LINE_NOT_UTF8"
    assert error["context"] == {"line": 3, "source": "stdin"}
    # A surrogateescape stdin without bytes, as a C-locale sys.stdin decodes them
    code, [envelope] = run(["count"], io.StringIO("a\n\udcff\n"))
    assert code == 1 and envelope["error"]["context"]["line"] == 2


def test_input_file_is_read_line_by_line(tmp_path: Path) -> None:
    source = tmp_path / "in.ndjson"
    source.write_bytes(b"one\r\ntwo\n" + b"q" * 65 + b"\n")
    code, events = run(["upper", "--input-file", str(source)])
    assert [e["data"]["text"] for e in events[:2]] == ["ONE", "TWO"]
    assert code == 1 and events[-1]["error"]["context"] == {
        "line": 3,
        "limit_bytes": 64,
        "source": str(source),
    }
    code, [envelope] = run(["count", "--input-file", str(tmp_path / "missing")])
    assert code == 2 and envelope["error"]["code"] == "INPUT_FILE_UNREADABLE"
    code, [envelope] = run(["count", "--input-file", "-"], io.StringIO("a\nb\n"))
    assert code == 0 and envelope["data"]["lines"] == 2


def test_a_terminal_on_stdin_is_refused() -> None:
    for command in ("count", "upper"):
        code, [envelope] = run([command], Terminal())
        assert code == 2 and envelope["error"]["code"] == "STDIN_IS_TTY", command


def test_call_takes_input_lines() -> None:
    assert app.call("count", {"input_lines": ["a", "b"]}).data == {"lines": 2, "first": "a"}
    assert app.call("upper", {"input_lines": ["a"]}).data == [{"n": 1, "text": "A"}]
    missing = app.call("count", {})
    assert missing.exit_code == 2 and missing.error is not None
    assert missing.error.code == "STDIN_UNAVAILABLE"
    assert "input_lines" in (missing.error.fix_required or "")
    broken = app.call("count", {"input_lines": ["a\nb"]})
    assert broken.exit_code == 2 and broken.error is not None
    assert broken.error.context == {"field": "input_lines", "line": 1}
    assert app.call("count", {"input_lines": "a"}).exit_code == 2
    both = app.call("count", {"input_lines": ["a"], "input_file": "x"})
    assert both.exit_code == 2 and both.error is not None
    too_long = app.call("count", {"input_lines": ["a", "z" * 65]})
    assert too_long.exit_code == 1 and too_long.error is not None
    assert too_long.error.context == {"line": 2, "limit_bytes": 64, "source": "input_lines"}
    # Only a line-mode command takes them
    other = app.call("payload", {"input_lines": ["a"]})
    assert other.exit_code == 2 and other.error is not None
    assert other.error.context["field"] == "input_lines"


def test_exec_lines_take_input_lines() -> None:
    plan = json.dumps({"_cmd": "count", "input_lines": ["x", "y", "z"]}) + "\n"
    code, [envelope] = run(["exec"], io.StringIO(plan))
    assert code == 0 and envelope["data"] == {"lines": 3, "first": "x"}


def test_schema_and_payload_name_the_line_mode() -> None:
    code, [envelope] = run(["count", "--schema"])
    data = envelope["data"]
    assert data["stdin_input"] is True and data["stdin_mode"] == "lines"
    assert "Read the input lines" in data["flags"]["input-file"]["description"]
    code, [envelope] = run(["payload", "--schema"])
    assert "stdin_mode" not in envelope["data"]
    from treaty._tools import input_schema  # the MCP tool schema
    from treaty._values import CommandPath

    lines_key = input_schema(app._commands[CommandPath("count")])["properties"]["input_lines"]
    assert lines_key["type"] == "array" and lines_key["items"] == {"type": "string"}


def test_registration_checks_the_mode() -> None:
    target = App("reg", version="1.0.0")
    with pytest.raises(RegistrationError, match="stdin_input='line'"):

        @target.command("a", description="A", danger_level="safe", exit_codes=(),
                        stdin_input="line")  # type: ignore[arg-type]  # fmt: skip
        def a(args: NoArgs, ctx: Ctx) -> Count:
            raise AssertionError

    @dataclass(frozen=True, slots=True)
    class Clash:
        input_lines: str = Flag(description="Collides")

    with pytest.raises(RegistrationError, match="input_lines"):

        @target.command("b", description="B", danger_level="safe", exit_codes=(),
                        stdin_input="lines")  # fmt: skip
        def b(args: Clash, ctx: Ctx) -> Count:
            raise AssertionError


@dataclass(frozen=True, slots=True)
class Count:
    lines: int


def test_streaming_payload_command_reads_its_payload() -> None:
    # A streaming stdin_input=True command used to see ctx.stdin_text as None
    code, events = run(["events"], io.StringIO("a\nb\n"))
    assert code == 0 and [e["data"]["text"] for e in events[:-1]] == ["a", "b"]


def _feed(write: int, lines: list[bytes], pause: float) -> threading.Thread:
    def target() -> None:
        with os.fdopen(write, "wb", buffering=0) as pipe:
            for line in lines:
                time.sleep(pause)
                pipe.write(line)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def test_each_line_read_restarts_a_streams_idle_timeout() -> None:
    # tally yields once, at the end; 10 lines 0.2 s apart outlast a 1 s idle limit, but
    # each read shows the input is alive. The limit leaves a loaded runner's late wake
    # 0.8 s per line (#172)
    read, write = os.pipe()
    feeder = _feed(write, [b"%d\n" % n for n in range(10)], 0.2)
    with os.fdopen(read, "r", encoding="utf-8") as stdin:
        code, events = run(["tally", "--timeout", "1"], stdin)  # type: ignore[arg-type]
    feeder.join()
    assert code == 0 and events[0]["data"] == {"lines": 10, "first": "0"}, events


def test_a_stalled_producer_times_the_stream_out() -> None:
    read, write = os.pipe()
    feeder = _feed(write, [b"a\n", b"b\n"], 0.9)
    with os.fdopen(read, "r", encoding="utf-8") as stdin:
        code, events = run(["upper", "--timeout", "0.5"], stdin)  # type: ignore[arg-type]
        feeder.join()  # the abandoned worker still reads; close the pipe after the writer
    assert code == 10 and events[-1]["error"]["code"] == "TIMEOUT"
    assert "next event" in events[-1]["error"]["message"]


def test_a_line_mode_stream_is_a_filter_in_a_real_pipe() -> None:
    # Each event is out before the next line is in: a | b | c runs concurrently
    proc = subprocess.Popen(
        [sys.executable, str(LINECTL), "upper"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env={"PATH": os.environ["PATH"], "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")},
    )
    assert proc.stdin is not None and proc.stdout is not None
    try:
        for n, word in enumerate((b"alpha", b"beta"), 1):
            proc.stdin.write(word + b"\n")
            proc.stdin.flush()
            event = json.loads(proc.stdout.readline())
            assert event["data"] == {"n": n, "text": word.decode().upper()}, event
        proc.stdin.write(b"\xff\n")  # not UTF-8, read through sys.stdin's bytes
        proc.stdin.close()
        last = json.loads(proc.stdout.readline())
        assert last["error"]["code"] == "LINE_NOT_UTF8", last
        assert last["error"]["context"]["line"] == 3, last
        assert proc.wait(timeout=10) == 1
    finally:
        proc.kill()
        proc.wait()
        proc.stdout.close()
