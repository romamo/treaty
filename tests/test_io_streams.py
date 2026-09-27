"""I/O limits and streams: REQ-F-011, F-014, F-053, F-054, O-001."""

import io
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import needs_posix_signals, spec_validator

from treaty import App, Ctx, Format, NoArgs, table

IOCTL = Path(__file__).resolve().parent / "fixture_io_app.py"
BASE_ENV = {"PATH": os.environ["PATH"]}


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class Row:
    name: str
    size: int
    tags: list[str]


def make_app() -> App:
    app = App("ioctl", version="1", default_timeout=0.3)
    app.format(Format.CSV, render=table(","))

    @app.command("once", description="One event, then silence", danger_level="safe",
                 exit_codes=(), streaming=True)  # fmt: skip
    def once(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, int]]:
        yield {"n": 1}
        time.sleep(2)  # abandoned on its worker once the idle limit passes
        yield {"n": 2}

    @app.command("ticks", description="An event every 0.1 s", danger_level="safe",
                 exit_codes=(), streaming=True)  # fmt: skip
    def ticks(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, int]]:
        for n in range(6):
            time.sleep(0.1)
            yield {"n": n}

    @app.command("slow", description="Sleeps 0.35 s", danger_level="safe", exit_codes=(),
                 heartbeat=True, timeout=5)  # fmt: skip
    def slow(args: NoArgs, ctx: Ctx) -> dict[str, bool]:
        time.sleep(0.35)
        return {"done": True}

    @app.command("count", description="Count the input", danger_level="safe", exit_codes=(),
                 stdin_input=True)  # fmt: skip
    def count(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        assert ctx.stdin_text is not None
        return {"bytes": len(ctx.stdin_text.encode())}

    @app.command("rows", description="Rows", danger_level="safe", exit_codes=(),
                 output_file=True)  # fmt: skip
    def rows(args: NoArgs, ctx: Ctx) -> list[Row]:
        return [Row("a", 1, ["x"]), Row("b\tc", 2, [])]

    return app


def run(
    argv: list[str], *, stdin: io.StringIO | None = None, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = make_app().run(
        argv, stdin=stdin or io.StringIO(), stdout=out, stderr=err, env=env or {}, isatty=False
    )
    return code, out.getvalue(), err.getvalue()


def lines(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines()]


# REQ-F-011


def test_f011_a_stream_silent_past_the_idle_limit_ends_with_timeout() -> None:
    started = time.monotonic()
    code, out, _ = run(["once"])
    events = lines(out)
    assert code == 10 and time.monotonic() - started < 1.5
    assert events[0]["data"] == {"n": 1}
    assert events[-1]["error"]["code"] == "TIMEOUT" and events[-1]["meta"]["seq"] == 1
    assert all(e["meta"]["timeout_ms"] == 300 for e in events)


def test_f011_a_stream_that_keeps_producing_runs_past_the_limit() -> None:
    code, out, _ = run(["ticks"])
    assert code == 0 and lines(out)[-1]["meta"]["total"] == 6


def test_f011_schema_names_the_idle_timeout() -> None:
    _, out, _ = run(["ticks", "--schema"])
    assert json.loads(out)["data"]["timeout_kind"] == "idle"


# REQ-F-014 (D1): the stream case is in test_round_six; before any output it stays 141


def test_f014_closed_before_any_output_exits_141_silently() -> None:
    proc = subprocess.Popen(
        [sys.executable, str(IOCTL), "sleepy"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=BASE_ENV,
    )
    assert proc.stdout is not None and proc.stderr is not None
    proc.stdout.close()
    code = proc.wait(timeout=10)
    assert code == 141 and proc.stderr.read() == b""


def test_f014_head_after_a_complete_event_exits_0_silently(tmp_path: Path) -> None:
    flag = tmp_path / "go"
    proc = subprocess.Popen(
        [sys.executable, str(IOCTL), "wait", "--until", str(flag)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=BASE_ENV,
    )
    assert proc.stdout is not None and proc.stderr is not None
    first = json.loads(proc.stdout.readline())
    proc.stdout.close()
    flag.touch()
    code = proc.wait(timeout=10)
    assert first["data"] == {"n": 1}
    assert code == 0 and proc.stderr.read() == b""


# REQ-F-053


def test_f053_main_sets_pythonunbuffered() -> None:
    proc = subprocess.run(
        [sys.executable, str(IOCTL), "env"], capture_output=True, env=BASE_ENV, timeout=10
    )
    assert json.loads(proc.stdout)["data"] == {"PYTHONUNBUFFERED": "1"}


def test_f053_each_event_reaches_the_reader_before_the_process_ends(tmp_path: Path) -> None:
    """The second event waits for a file the test creates only after reading the first"""
    flag = tmp_path / "go"
    proc = subprocess.Popen(
        [sys.executable, str(IOCTL), "wait", "--until", str(flag)],
        stdout=subprocess.PIPE,
        env=BASE_ENV,
    )
    assert proc.stdout is not None
    assert json.loads(proc.stdout.readline())["data"] == {"n": 1}
    flag.touch()
    assert json.loads(proc.stdout.readline())["data"] == {"n": 2}
    proc.stdout.close()
    assert proc.wait(timeout=10) == 0


def test_f053_heartbeats_precede_the_envelope() -> None:
    code, out, _ = run(["slow", "--heartbeat-ms", "100"])
    *beats, envelope = lines(out)
    assert code == 0 and len(beats) >= 2
    for beat in beats:
        assert set(beat) == {"status", "heartbeat", "elapsed_ms"}
        assert beat["status"] == "running" and beat["heartbeat"] is True
    assert beats[0]["elapsed_ms"] < beats[-1]["elapsed_ms"]
    assert envelope["data"] == {"done": True}


def test_f053_heartbeats_off_with_zero_and_listed_in_schema() -> None:
    code, out, _ = run(["slow", "--heartbeat-ms", "0"])
    assert code == 0 and len(lines(out)) == 1
    _, out, _ = run(["slow", "--schema"])
    data = json.loads(out)["data"]
    assert data["heartbeat_ms"] == 10_000 and data["flags"]["heartbeat-ms"]["default"] == 10_000


# REQ-F-054


def test_f054_stdin_over_the_cap_exits_2_with_a_hint() -> None:
    code, out, _ = run(["count"], stdin=io.StringIO("x" * 65_537))
    error = json.loads(out)["error"]
    assert code == 2 and error["code"] == "STDIN_TOO_LARGE"
    assert "--input-file" in error["hint"]


def test_f054_stdin_under_the_cap_is_read() -> None:
    code, out, _ = run(["count"], stdin=io.StringIO("x" * 65_535))
    assert code == 0 and json.loads(out)["data"] == {"bytes": 65_535}


def test_f054_input_file_has_no_cap(tmp_path: Path) -> None:
    payload = tmp_path / "big.txt"
    payload.write_text("y" * 100_000)
    code, out, _ = run(["count", "--input-file", str(payload)])
    assert code == 0 and json.loads(out)["data"] == {"bytes": 100_000}


def test_f054_input_file_is_registered_automatically() -> None:
    manifest = make_app().manifest()
    spec_validator("manifest-response").validate(manifest)
    assert "input-file" in manifest["commands"]["count"]["flags"]  # type: ignore[index]


def test_f054_tool_variable_sets_the_cap() -> None:
    code, out, _ = run(["count"], stdin=io.StringIO("x" * 20), env={"IOCTL_MAX_STDIN_BYTES": "10"})
    assert code == 2 and json.loads(out)["error"]["context"] == {"limit_bytes": 10}


def test_f054_terminal_stdin_is_refused() -> None:
    code, out, _ = run(["count"], stdin=Terminal())
    assert code == 2 and json.loads(out)["error"]["code"] == "STDIN_IS_TTY"


def test_f054_exec_lines_take_input_file(tmp_path: Path) -> None:
    payload = tmp_path / "p.txt"
    payload.write_text("abc")
    plan = "\n".join(
        [
            json.dumps({"_cmd": "count", "input_file": str(payload)}),
            json.dumps({"_cmd": "count"}),
        ]
    )
    code, out, _ = run(["exec", "--ignore-errors"], stdin=io.StringIO(plan))
    first, second = lines(out)
    assert first["data"] == {"bytes": 3}
    assert code == 1 and second["error"]["code"] == "STDIN_UNAVAILABLE"


# REQ-O-001


def test_o001_jsonl_is_one_object_per_line_everywhere() -> None:
    for argv, stdin in (
        (["rows", "--format", "jsonl"], ""),
        (["ticks", "--format", "jsonl"], ""),
        (["exec", "--format", "jsonl"], json.dumps({"_cmd": "rows"}) + "\n"),
    ):
        code, out, _ = run(argv, stdin=io.StringIO(stdin))
        parsed = lines(out)
        assert code == 0 and parsed, argv
        for envelope in parsed:
            spec_validator("response-envelope").validate(envelope)


def test_o001_tsv_is_built_in_with_a_header_row() -> None:
    code, out, _ = run(["rows", "--format", "tsv"])
    assert code == 0 and out.splitlines()[0] == "name\tsize\ttags"
    rows = [line.split("\t") for line in out.splitlines()]
    assert rows[1:] == [["a", "1", '["x"]'], ["b\\tc", "2", "[]"]]


def test_o001_tsv_escapes_instead_of_quoting() -> None:
    from treaty import table

    out = table("\t")([{"a": 'x"y\\z', "b": "l1\nl2\r", "c": None}])
    assert out == 'a\tb\tc\nx"y\\\\z\tl1\\nl2\\r\t\n'


def test_o001_csv_to_a_file_with_the_envelope_on_stdout(tmp_path: Path) -> None:
    target = tmp_path / "r.csv"
    code, out, _ = run(["rows", "--format", "csv", "--output", str(target)])
    envelope = json.loads(out)
    spec_validator("response-envelope").validate(envelope)
    assert code == 0 and target.read_text().splitlines()[0] == "name,size,tags"
    assert envelope["data"] == {"path": str(target), "bytes": target.stat().st_size}


@pytest.mark.parametrize(("command", "flag"), [("rows", "--output"), ("count", "--input-file")])
def test_framework_path_flags_refuse_line_breaks(command: str, flag: str, tmp_path: Path) -> None:
    code, out, _ = run([command, flag, str(tmp_path / "a\nb")])
    assert code == 2 and json.loads(out)["error"]["context"]["rejected_pattern"] == "newline"
    assert list(tmp_path.iterdir()) == []


def test_o001_output_with_a_format_name_exits_2_and_writes_nothing(tmp_path: Path) -> None:
    code, out, _ = run(["rows", "--output", "json"])
    error = json.loads(out)["error"]
    assert code == 2 and "--format json" in error["errors"][0]["suggestion"]
    assert not Path("json").exists()


def test_o001_unknown_format_exits_2_before_any_file(tmp_path: Path) -> None:
    target = tmp_path / "r.txt"
    code, out, _ = run(["rows", "--format", "jsn", "--output", str(target)])
    assert code == 2 and "tsv" in json.loads(out)["error"]["context"]["allowed"]
    assert not target.exists()


@pytest.mark.parametrize("mode", ["json", "jsonl"])
def test_o001_json_modes_written_to_a_file_parse(tmp_path: Path, mode: str) -> None:
    target = tmp_path / "r"
    code, _, _ = run(["rows", "--format", mode, "--output", str(target)])
    text = target.read_text()
    rows = json.loads(text) if mode == "json" else [json.loads(x) for x in text.splitlines()]
    assert code == 0 and [r["name"] for r in rows] == ["a", "b\tc"]


# Review fixes: closed descriptors, fd-level stdout, cleanup order, finished streams


@pytest.mark.skipif(sys.platform == "win32", reason="closes descriptors with /bin/sh")
def test_a_closed_stdout_or_stdin_is_not_a_crash() -> None:
    def closed(redirect: str, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", "-c", f'exec "$@" {redirect}', "_", sys.executable, str(IOCTL), *argv],
            capture_output=True,
            text=True,
            env=BASE_ENV,
            timeout=10,
            check=False,
        )

    no_stdout = closed(">&-", "env")
    assert no_stdout.returncode == 0 and "Traceback" not in no_stdout.stderr
    no_stdin = closed("<&-", "payload")
    envelope = json.loads(no_stdin.stdout)
    assert no_stdin.returncode == 2, no_stdin.stderr
    assert envelope["error"]["code"] == "STDIN_UNAVAILABLE"
    assert envelope["error"]["message"] == "Stdin is closed, so there is no payload to read."


def test_fd_level_writes_under_main_reach_stderr_not_stdout() -> None:
    proc = subprocess.run(
        [sys.executable, str(IOCTL), "leaky"], capture_output=True, text=True, env=BASE_ENV
    )
    envelope = json.loads(proc.stdout)  # one JSON document: nothing leaked ahead of it
    assert proc.returncode == 0 and envelope["data"] == {"done": True}
    for leak in ("from-child", "from-fd", "from-buffer"):
        assert leak in proc.stderr


@needs_posix_signals
def test_a_signal_lets_the_handler_finish_before_cleanup(tmp_path: Path) -> None:
    log = tmp_path / "log"
    proc = subprocess.Popen(
        [sys.executable, str(IOCTL), "ordered"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**BASE_ENV, "IOCTL_LOG": str(log)},
    )
    try:
        deadline = time.monotonic() + 10
        while not (log.exists() and "started" in log.read_text()):
            assert time.monotonic() < deadline, "the handler never started"
            time.sleep(0.02)
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 143
    assert log.read_text().split() == ["started", "handler", "cleanup"]


class Dead(io.StringIO):
    """A stream whose reader is gone after ``live`` writes"""

    def __init__(self, live: int) -> None:
        super().__init__()
        self.live = live

    def write(self, text: str) -> int:
        if self.live <= 0:
            raise BrokenPipeError(32, "Broken pipe")
        self.live -= 1
        return super().write(text)


def test_a_finished_stream_skips_cleanup_when_its_last_line_is_lost() -> None:
    cleaned: list[bool] = []
    app = App("streamy", version="1")

    @app.command(
        "two",
        description="Two events",
        danger_level="safe",
        exit_codes=(),
        streaming=True,
        cleanup=lambda: cleaned.append(True),
    )
    def two(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, int]]:
        yield {"n": 1}
        yield {"n": 2}

    # Each envelope is two writes: the JSON and its newline; the terminal one fails
    code = app.run(["two", "--format", "json"], stdout=Dead(4), stderr=io.StringIO(), env={})
    assert code == 0 and cleaned == []
