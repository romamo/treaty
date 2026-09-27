"""An app for stdout buffering and SIGPIPE tests; runnable as a tool."""

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from treaty import App, Ctx, Flag, NoArgs

app = App("ioctl", version="1.0.0")


@app.command("env", description="Report the buffering variable", danger_level="safe", exit_codes=())
def env(args: NoArgs, ctx: Ctx) -> dict[str, str | None]:
    return {"PYTHONUNBUFFERED": os.environ.get("PYTHONUNBUFFERED")}


@dataclass(frozen=True, slots=True)
class WaitArgs:
    until: Path = Flag(description="Yield the second event once this file exists")


@app.command(
    "wait",
    description="One event, then a second once a file appears",
    danger_level="safe",
    exit_codes=(),
    streaming=True,
)
def wait(args: WaitArgs, ctx: Ctx) -> Iterator[dict[str, int]]:
    yield {"n": 1}
    while not args.until.exists():
        time.sleep(0.01)
    yield {"n": 2}


@app.command(
    "sleepy",
    description="Sleep, then answer",
    danger_level="safe",
    exit_codes=(),
)
def sleepy(args: NoArgs, ctx: Ctx) -> dict[str, bool]:
    time.sleep(0.3)
    return {"done": True}


@app.command(
    "payload",
    description="Count a stdin payload",
    danger_level="safe",
    exit_codes=(),
    stdin_input=True,
)
def payload(args: NoArgs, ctx: Ctx) -> dict[str, int]:
    return {"chars": len(ctx.stdin_text or "")}


def note(line: str) -> None:
    with open(os.environ["IOCTL_LOG"], "a", encoding="utf-8") as log:
        log.write(line + "\n")


@app.command(
    "ordered",
    description="Log when the handler and its cleanup run",
    danger_level="safe",
    exit_codes=(),
    cleanup=lambda: note("cleanup"),
)
def ordered(args: NoArgs, ctx: Ctx) -> dict[str, bool]:
    note("started")
    time.sleep(0.5)
    note("handler")
    return {"done": True}


@app.command(
    "leaky", description="Write to stdout below sys.stdout", danger_level="safe", exit_codes=()
)
def leaky(args: NoArgs, ctx: Ctx) -> dict[str, bool]:
    subprocess.run([sys.executable, "-c", "print('from-child')"], check=True)
    os.write(1, b"from-fd\n")
    sys.stdout.buffer.write(b"from-buffer\n")
    sys.stdout.flush()
    return {"done": True}


if __name__ == "__main__":
    app.main()
