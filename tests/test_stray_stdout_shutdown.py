"""Two stdout-interception faults (#268): under ``--debug`` a ``QueueListener`` writing
records to ``sys.stdout`` traced its own trace records on its thread, each trace the next;
and as the process exits with a held thread alive, descriptor 1 is a pipe whose reader
stops with the interpreter, so a last flush larger than the pipe could block the exit."""

import io
import json
import logging
import logging.handlers
import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass

from treaty import App, Ctx, Flag

SECRET = "hunter2-s3cret"


@dataclass(frozen=True, slots=True)
class Login:
    api_token: str = Flag(default="", description="API token", secret=True)


class _Seen(logging.Handler):
    """Sets ``traced`` once the listener has written a ``stdout write`` trace record"""

    def __init__(self) -> None:
        super().__init__()
        self.traced = threading.Event()

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage() == "stdout write":
            self.traced.set()


def listener_app() -> App:
    app = App("loud", version="1.0.0")

    @app.command("show", description="Show", danger_level="safe", exit_codes=())
    def show(args: Login, ctx: Ctx) -> dict[str, bool]:
        records: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
        seen = _Seen()
        # sys.stdout is the run's stand-in: the listener's thread writes every record to it
        listener = logging.handlers.QueueListener(records, logging.StreamHandler(sys.stdout), seen)
        handler = logging.handlers.QueueHandler(records)
        logging.getLogger().addHandler(handler)
        listener.start()
        try:
            logging.getLogger("loud.lib").warning("token %s", args.api_token)
            # The listener wrote the trace of its own write back to the stand-in; a trace
            # of that write would be queued before the stop
            assert seen.traced.wait(30)
        finally:
            logging.getLogger().removeHandler(handler)
            listener.stop()
        return {"ok": True}

    return app


def test_a_queue_listener_writing_to_stdout_under_debug_does_not_trace_its_traces() -> None:
    out, err = io.StringIO(), io.StringIO()
    code = listener_app().run(
        ["show", "--api-token-from-env", "TOKEN", "--debug"],
        stdin=io.StringIO(""),
        stdout=out,
        stderr=err,
        env={"TOKEN": SECRET},
        isatty=False,
    )
    assert code == 0, err.getvalue()[-2000:]
    assert json.loads(out.getvalue())["data"] == {"ok": True}
    assert SECRET not in err.getvalue()
    lines = err.getvalue().splitlines()
    assert len(lines) < 50, lines[:20]
    traced = [json.loads(line) for line in lines if line.startswith("{")]
    writes = [line["fields"]["text"] for line in traced if line["message"] == "stdout write"]
    # The record the listener wrote, traced once, redacted
    assert writes == ["token [REDACTED]"]
    # The listener's copy of that trace record went out as written, not traced again
    assert lines.count("stdout write") == 1


LINE = "{tag} {secret} " + "x" * 100
WRITTEN = 256 * 1024 // (len(LINE) + 4)

HOST = f"""
import atexit, os, sys, threading
from dataclasses import dataclass
from treaty import App, Ctx, Flag

app = App("late", version="1.0.0")
GO = threading.Event()
SECRET = os.environ["LATE_TOKEN"]


@dataclass(frozen=True, slots=True)
class Login:
    api_token: str = Flag(description="API token", secret=True)


@app.command("slow", description="Outlive the timeout", danger_level="safe", exit_codes=(),
             timeout=1)
def slow(args: Login, ctx: Ctx) -> dict[str, bool]:
    GO.wait(120)  # abandoned at its timeout, alive as the process exits
    return {{"ok": True}}


def lines(tag, count):
    return "".join({LINE!r}.format(tag=tag, secret=SECRET) + "\\n" for _ in range(count))


def at_exit():
    # Registered before main(), so it runs after the interceptor's exit hook: descriptor 1
    # is still the pipe. What stays in sys.stdout's buffer is flushed as the interpreter
    # finalizes, once the reader has stopped: it must not block the exit
    sys.__stdout__.write(lines("buffered", {WRITTEN // 3}))


atexit.register(at_exit)
try:
    app.main()
except SystemExit:
    pass
# Past the pipe's buffer while the held thread lives: the reader drains it to stderr
data = memoryview(lines("fd1", {WRITTEN}).encode())
while data:
    data = data[os.write(1, data):]
"""


def test_a_large_write_to_descriptor_1_at_exit_drains_and_never_blocks_the_exit() -> None:
    env = {**os.environ, "LATE_TOKEN": SECRET, "PYTHONUTF8": "1"}
    for name in ("FORCE_COLOR", "TREATY_FORMAT"):
        env.pop(name, None)
    proc = subprocess.run(
        [sys.executable, "-", "slow", "--api-token-from-env", "LATE_TOKEN"],
        input=HOST.encode(),
        capture_output=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert SECRET.encode() not in proc.stdout + proc.stderr
    out = proc.stdout.decode().replace("\r\n", "\n")
    err = proc.stderr.decode().replace("\r\n", "\n")
    assert json.loads(out.splitlines()[0])["error"]["code"] == "TIMEOUT"
    assert "fd1" not in out
    expected = LINE.format(tag="fd1", secret="[REDACTED]")
    assert err.splitlines().count(expected) == WRITTEN, err[-2000:]
