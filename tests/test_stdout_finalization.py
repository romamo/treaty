"""What reaches descriptor 1 as the process exits, a handler thread abandoned at its timeout
still alive, is not lost (#271). Descriptor 1 is then a pipe to stderr whose reader, a
daemon thread, stops for good as the interpreter finalizes: what a host's exit hook or the
interpreter's last flush wrote there reached neither stdout nor stderr. Treaty's exit hook
now turns descriptor 1 to a spool file it drains itself, redacted, and gives it back to
stdout once the last held thread ends."""

import json
import os
import subprocess
import sys

import pytest

SECRET = "hunter2-s3cret"

HOST = """
import atexit, os, sys, threading

EXITING, WROTE, GO = threading.Event(), threading.Event(), threading.Event()
ENDS = os.environ["LATE_MODE"] == "ends"


def last():
    # Registered before treaty is imported, so it runs after treaty's own exit hook
    EXITING.set()
    WROTE.wait(30)
    if ENDS:
        GO.set()
        for thread in threading.enumerate():
            if thread.name == "treaty-handler":
                thread.join(30)
    os.write(1, b"last fd1\\n")
    sys.__stdout__.write("last dunder\\n")  # left in its buffer for the last flush
    print("last print")


atexit.register(last)

from dataclasses import dataclass
from treaty import App, Ctx, Flag

app = App("late", version="1.0.0")


@dataclass(frozen=True, slots=True)
class Login:
    api_token: str = Flag(description="API token", secret=True)


@app.command("slow", description="Outlive the timeout", danger_level="safe", exit_codes=(),
             timeout=1)
def slow(args: Login, ctx: Ctx) -> dict[str, bool]:
    # Abandoned at its timeout, it writes once treaty's exit hook has run
    EXITING.wait(30)
    os.write(1, ("held fd1 " + args.api_token + "\\n").encode())
    WROTE.set()
    GO.wait(120)
    return {"ok": True}


def host():
    # Registered after treaty is imported, before main(): it runs before treaty's hook
    print("host print")
    os.write(1, b"host fd1\\n")
    sys.__stdout__.write("host dunder\\n")


atexit.register(host)
app.main()
"""


def run_host(mode: str) -> tuple[str, str]:
    env = {**os.environ, "LATE_TOKEN": SECRET, "LATE_MODE": mode, "PYTHONUTF8": "1"}
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
    assert json.loads(out.splitlines()[0])["error"]["code"] == "TIMEOUT", (out, err)
    return out, err


@pytest.mark.parametrize("mode", ["stays", "ends"])
def test_writes_to_descriptor_1_at_exit_reach_stdout_or_stderr(mode: str) -> None:
    out, err = run_host(mode)
    lines = out.splitlines()[1:] + err.splitlines()
    for line in ("host print", "host fd1", "host dunder", "last fd1", "last dunder"):
        assert line in lines, (line, out, err)
    assert "last print" in out.splitlines(), out
    # What the held thread wrote to descriptor 1 reached stderr redacted, never stdout
    assert "held fd1 [REDACTED]" in err.splitlines(), err
    assert "held fd1" not in out


def test_descriptor_1_is_stdout_again_once_the_last_held_thread_ends_at_exit() -> None:
    out, err = run_host("ends")
    assert "last fd1" in out.splitlines(), (out, err)
    assert "last dunder" in out.splitlines(), (out, err)


def test_while_a_held_thread_lives_descriptor_1_never_leads_to_stdout_at_exit() -> None:
    out, err = run_host("stays")
    assert out.splitlines()[1:] == ["host print", "last print"], (out, err)
    for line in ("host fd1", "host dunder", "last fd1", "last dunder"):
        assert line in err.splitlines(), (line, err)
