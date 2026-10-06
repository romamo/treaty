"""An app whose streaming handlers are async generators (#347); runnable as a tool."""

from __future__ import annotations

import asyncio
import contextvars
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass

from treaty import App, Ctx, Flag, NoArgs

app = App("tickctl", version="1.0.0", default_timeout=30)

EVENTS: list[str] = []
"""What the sources and resources did, in order, for the in-process tests"""
TENANT: contextvars.ContextVar[str] = contextvars.ContextVar("tenant", default="none")


def note(what: str) -> None:
    EVENTS.append(what)
    print(f"tickctl: {what}", file=sys.stderr, flush=True)


@dataclass(frozen=True, slots=True)
class TickArgs:
    count: int = Flag(default=0, description="Ticks to emit, 0 for no end")
    interval: float = Flag(default=0.05, description="Seconds between ticks")
    stall_at: int = Flag(default=0, description="Wait forever before this tick, 0 for never")


@dataclass(frozen=True, slots=True)
class Tick:
    n: int
    tenant: str


class Feed:
    """An async resource the source reads from"""

    def __init__(self) -> None:
        self.open = True

    @classmethod
    async def acquire(cls, args: object, ctx: Ctx) -> Feed:
        await asyncio.sleep(0)
        note("feed open")
        return cls()

    async def release(self) -> None:
        await asyncio.sleep(0)
        self.open = False
        note("feed closed")


async def ticks(args: TickArgs, ctx: Ctx, feed: Feed) -> AsyncIterator[Tick]:
    TENANT.set("acme")  # set once: it survives between events
    n = 0
    try:
        while args.count == 0 or n < args.count:
            n += 1
            if n == args.stall_at:
                await asyncio.sleep(3600)
            assert feed.open
            ctx.progress(f"tick {n}")
            yield Tick(n, TENANT.get())
            await asyncio.sleep(args.interval)
    finally:
        await asyncio.sleep(0)
        note("source finally")


app.command(
    "ticks",
    description="Tick until stopped",
    danger_level="safe",
    exit_codes=(),
    streaming=True,
    timeout=None,
)(ticks)
app.command(
    "watch",
    description="Tick, each within an idle limit",
    danger_level="safe",
    exit_codes=(),
    streaming=True,
    timeout=5,
)(ticks)


@app.command(
    "stubborn",
    description="Tick, then ignore cancellation",
    danger_level="safe",
    exit_codes=(),
    streaming=True,
    timeout=None,
)
async def stubborn(args: NoArgs, ctx: Ctx) -> AsyncIterator[Tick]:
    yield Tick(1, "none")
    while True:
        try:
            note("waiting")  # the test signals once the source is inside its await
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            note("cancellation ignored")


@dataclass(frozen=True, slots=True)
class Applied:
    effect: str
    n: int


@app.command(
    "apply",
    description="Apply a change per tick",
    danger_level="mutating",
    exit_codes=(),
    streaming=True,
)
async def apply(args: TickArgs, ctx: Ctx) -> AsyncIterator[Applied]:
    for n in range(1, args.count + 1):
        await asyncio.sleep(0)
        yield Applied("created" if n % 2 else "noop", n)


if __name__ == "__main__":
    app.main()
