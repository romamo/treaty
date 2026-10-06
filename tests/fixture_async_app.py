"""An app whose async handler awaits in its finally block (#355); runnable as a tool."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass

from treaty import App, Ctx, Flag

app = App("lingerctl", version="1.0.0")


def note(what: str) -> None:
    print(f"lingerctl: {what}", file=sys.stderr, flush=True)


@dataclass(frozen=True, slots=True)
class LingerArgs:
    cleanup: float = Flag(default=0.2, description="Seconds the finally block awaits")


async def linger(args: LingerArgs, ctx: Ctx) -> None:
    try:
        note("waiting")  # the test signals once the handler is inside its await
        await asyncio.sleep(3600)
    finally:
        await asyncio.sleep(args.cleanup)
        note("cleaned up")


# No timeout, so the handler runs on the main thread; with one, on a worker
app.command(
    "linger", description="Wait until stopped", danger_level="safe", exit_codes=(), timeout=None
)(linger)
app.command(
    "linger-bounded",
    description="Wait until stopped or timed out",
    danger_level="safe",
    exit_codes=(),
    timeout=60,
)(linger)


if __name__ == "__main__":
    app.main()
