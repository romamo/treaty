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


class Pool:
    """An async resource released on the run's loop, behind the handler (#383)"""

    @classmethod
    async def acquire(cls, args: object, ctx: Ctx) -> Pool:
        await asyncio.sleep(0)
        return cls()

    async def release(self) -> None:
        await asyncio.sleep(0)
        note("pool released")


async def linger_held(args: LingerArgs, ctx: Ctx, pool: Pool) -> None:
    await linger(args, ctx)


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

app.command(
    "linger-held",
    description="Wait until stopped, holding a pool",
    danger_level="safe",
    exit_codes=(),
    timeout=None,
)(linger_held)


if __name__ == "__main__":
    app.main()
