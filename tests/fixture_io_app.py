"""An app for stdout buffering and SIGPIPE tests; runnable as a tool."""

import os
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


if __name__ == "__main__":
    app.main()
