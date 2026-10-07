"""#389's repro: a finite stream and an endless one, for the kit's stream checks."""

import time
from collections.abc import Iterator
from dataclasses import dataclass

from treaty import App, Ctx, NoArgs

app = App("ticks", version="0.1.0")


@dataclass(frozen=True, slots=True)
class Tick:
    n: int


@app.command(
    "count",
    description="Count up",
    danger_level="safe",
    exit_codes=(),
    streaming=True,
    timeout=None,
    examples=[("Count to three", "ticks count")],
)
def count(args: NoArgs, ctx: Ctx) -> Iterator[Tick]:
    for n in range(1, 4):
        yield Tick(n)
        time.sleep(0.3)


@app.command(
    "forever",
    description="Tick until interrupted",
    danger_level="safe",
    exit_codes=(),
    streaming=True,
    timeout=None,
)
def forever(args: NoArgs, ctx: Ctx) -> Iterator[Tick]:
    n = 0
    while True:
        n += 1
        yield Tick(n)
        time.sleep(0.2)


if __name__ == "__main__":
    app.main()
