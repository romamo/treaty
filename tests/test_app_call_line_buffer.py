"""Under ``App.call``, what a handler prints reaches the host's own stream a line at a time,
each thread's apart, so a secret split across two writes is redacted whole (#261): during
the call, from a handler thread held past its timeout after the call returned, and from
two calls' threads printing at once. A subprocess, since pytest captures the standard
streams; the script goes on stdin, as Windows limits a command line's length."""

import os
import subprocess
import sys

from treaty._stdout import LINE_CAP, LINE_TAIL, LineBuffer

SECRET = "hunter2-s3cret"

PRELUDE = f"""
import sys, threading
from dataclasses import dataclass
from treaty import App, Ctx, Flag

SECRET = {SECRET!r}
HEAD, TAIL = SECRET[:5], SECRET[5:]
ENV = {{"LIBCTL_API_TOKEN": SECRET, "LIBCTL_AUDIT_LOG": "0"}}


@dataclass(frozen=True, slots=True)
class Login:
    api_token: str = Flag(description="API token", secret=True)


app = App("libctl", version="1.0.0")
"""

DURING = """
@app.command("show", description="Print", danger_level="safe", exit_codes=(),
             timeout=TIMEOUT)
def show(args: Login, ctx: Ctx) -> dict[str, bool]:
    sys.stdout.write("out " + HEAD)
    sys.stdout.write(TAIL + "\\n")
    sys.stderr.write("err " + HEAD)
    sys.stderr.write(TAIL + "\\r")
    sys.stdout.write("progress " + HEAD)
    sys.stdout.write(TAIL + "\\rdone\\n")
    sys.stdout.write("unended " + HEAD)
    sys.stdout.write(TAIL)
    return {"ok": True}


envelope = app.call("show", {}, env=ENV)
sys.stdout.write("|after\\n")
print(envelope.ok)
"""


def run_script(script: str) -> subprocess.CompletedProcess[bytes]:
    env = {**os.environ, "PYTHONUTF8": "1"}
    for name in ("FORCE_COLOR", "TREATY_FORMAT"):
        env.pop(name, None)
    return subprocess.run(
        [sys.executable, "-"],
        input=script.encode(),
        capture_output=True,
        env=env,
        timeout=60,
        check=False,
    )


def text(stream: bytes) -> str:
    return stream.decode().replace("\r\n", "\n")


def test_a_secret_split_across_two_prints_during_a_call_is_redacted() -> None:
    for timeout in ("None", "30.0"):  # on the calling thread, and on a worker
        proc = run_script(PRELUDE + DURING.replace("TIMEOUT", timeout))
        assert proc.returncode == 0, proc.stderr
        assert SECRET.encode() not in proc.stdout + proc.stderr
        # A line without its end goes out redacted as the call returns, before the host's
        assert text(proc.stdout) == (
            "out [REDACTED]\nprogress [REDACTED]\rdone\nunended [REDACTED]|after\nTrue\n"
        ), (timeout, proc.stdout)
        assert text(proc.stderr) == "err [REDACTED]\r", (timeout, proc.stderr)


HELD = """
returned, done = threading.Event(), threading.Event()


@app.command("slow", description="Print late", danger_level="safe", exit_codes=(),
             timeout=0.05)
def slow(args: Login, ctx: Ctx) -> dict[str, bool]:
    returned.wait(30)  # abandoned at its timeout; prints once the call returned
    sys.stdout.write("late " + HEAD)
    sys.stdout.write(TAIL + "\\n")
    sys.stderr.write("unended " + HEAD)
    sys.stderr.write(TAIL)
    done.set()
    return {"ok": True}


envelope = app.call("slow", {}, env=ENV)
returned.set()
done.wait(30)
for thread in threading.enumerate():
    if thread.name == "treaty-handler":
        thread.join(30)  # as it ends, its line without an end goes out
sys.stderr.write("|ended\\n")
print(envelope.exit_code)
"""


def test_a_secret_split_by_a_held_thread_after_the_call_returned_is_redacted() -> None:
    proc = run_script(PRELUDE + HELD)
    assert proc.returncode == 0, proc.stderr
    assert SECRET.encode() not in proc.stdout + proc.stderr
    # A late thread's stdout text goes to stderr, never into the host's output
    assert text(proc.stdout) == "10\n", proc.stdout
    assert text(proc.stderr) == "late [REDACTED]\nunended [REDACTED]|ended\n", proc.stderr


INTERLEAVED = """
turns = [threading.Event() for _ in range(3)]  # each worker waits its turn to write


@app.command("first", description="Print", danger_level="safe", exit_codes=(), timeout=30.0)
def first(args: Login, ctx: Ctx) -> dict[str, bool]:
    sys.stdout.write("a " + HEAD)
    turns[0].set()
    turns[1].wait(30)
    sys.stdout.write(TAIL + "\\n")
    turns[2].set()
    return {"ok": True}


@app.command("second", description="Print", danger_level="safe", exit_codes=(),
             timeout=30.0)
def second(args: Login, ctx: Ctx) -> dict[str, bool]:
    turns[0].wait(30)
    sys.stdout.write("b " + HEAD)
    turns[1].set()
    turns[2].wait(30)
    sys.stdout.write(TAIL + "\\n")
    return {"ok": True}


results = {}
calls = [
    threading.Thread(target=lambda name=name: results.update({name: app.call(name, {}, env=ENV)}))
    for name in ("first", "second")
]
for call in calls:
    call.start()
for call in calls:
    call.join(60)
print(sorted(name for name, envelope in results.items() if envelope.ok))
"""


def test_two_threads_interleaving_halves_of_a_secret_are_each_redacted() -> None:
    proc = run_script(PRELUDE + INTERLEAVED)
    assert proc.returncode == 0, proc.stderr
    assert SECRET.encode() not in proc.stdout + proc.stderr
    assert text(proc.stdout).splitlines() == [
        "a [REDACTED]",
        "b [REDACTED]",
        "['first', 'second']",
    ], proc.stdout


def redact(line: str) -> str:
    return line.replace(SECRET, "[REDACTED]")


def test_a_raw_line_buffer_passes_text_on_as_written() -> None:
    """Without ``clean``, escapes stay; a secret a color splits, the color itself split
    across two writes, is found in the line's cleaned copy and goes out without escapes"""
    lines = LineBuffer(clean=False)
    assert lines.add("\x1b[1mbold\x1b[0m ", redact, color=False) == ""
    assert lines.add("ok\n", redact, color=False) == "\x1b[1mbold\x1b[0m ok\n"
    assert lines.add("tok " + SECRET[:3] + "\x1b[3", redact, color=False) == ""
    assert lines.add("1m" + SECRET[3:] + "\nnext", redact, color=False) == "tok [REDACTED]\n"
    assert lines.drain(redact) == "next"


def test_a_raw_line_past_the_cap_keeps_its_tail() -> None:
    lines = LineBuffer(clean=False)
    long = "x" * LINE_CAP
    passed = lines.add(long + SECRET[:5], redact, color=False)
    assert passed == "x" * (LINE_CAP + 5 - LINE_TAIL)
    assert lines.add(SECRET[5:] + "\n", redact, color=False).endswith("[REDACTED]\n")


KEPT = """
kept = []


@app.command("keep", description="Keep", danger_level="safe", exit_codes=(), timeout=None)
def keep(args: Login, ctx: Ctx) -> dict[str, bool]:
    kept.append(sys.stdout)  # the call's stand-in, kept as a StreamHandler keeps its stream
    return {"ok": True}


@app.command("half", description="Print", danger_level="safe", exit_codes=(), timeout=None)
def half(args: Login, ctx: Ctx) -> dict[str, bool]:
    kept[0].write("first " + HEAD)
    kept[0].write(TAIL)
    return {"ok": True}


@app.command("rest", description="Print", danger_level="safe", exit_codes=(), timeout=None)
def rest(args: Login, ctx: Ctx) -> dict[str, bool]:
    kept[0].write("|rest\\n")
    return {"ok": True}


app.call("keep", {}, env={"LIBCTL_API_TOKEN": "other-token-1", "LIBCTL_AUDIT_LOG": "0"})
app.call("half", {}, env=ENV)
app.call("rest", {}, env={"LIBCTL_API_TOKEN": "other-token-2", "LIBCTL_AUDIT_LOG": "0"})
"""


def test_a_line_held_in_a_stream_the_host_kept_goes_out_with_its_own_call() -> None:
    """A late stream a host kept past its call, then written through in a later one, is
    drained as that call returns, not in a third call that knows none of its secrets"""
    proc = run_script(PRELUDE + KEPT)
    assert proc.returncode == 0, proc.stderr
    assert SECRET.encode() not in proc.stdout + proc.stderr
    assert text(proc.stdout) == "first [REDACTED]|rest\n", proc.stdout
