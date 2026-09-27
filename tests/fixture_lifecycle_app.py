"""An app whose command holds a lock through a resource, for teardown tests; runnable."""

import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Self

from treaty import App, Ctx, Flag

app = App("lifectl", version="1.0.0")


LOG: list[str] = []
"""The log file of the latest run, from its LIFECTL_LOG; cleanup= takes no arguments"""


def note(line: str) -> None:
    with open(LOG[-1], "a", encoding="utf-8") as log:
        log.write(line + "\n")


@dataclass(frozen=True, slots=True)
class Hold:
    seconds: float = Flag(default=0.0, description="How long to hold the lease")


class Lease:
    """The deploy lock, held from acquire until the run releases it"""

    def __init__(self, held: AbstractContextManager[None]) -> None:
        self._held = held

    @classmethod
    def acquire(cls, args: Hold, ctx: Ctx) -> Self:
        held = ctx.lock("deploy", wait=0)
        held.__enter__()
        LOG.append(ctx.env["LIFECTL_LOG"])
        note("acquired")
        return cls(held)

    def release(self) -> None:
        self._held.__exit__(None, None, None)
        note("released")


@app.command(
    "hold",
    description="Hold the lease a while",
    danger_level="safe",
    exit_codes=(),
    has_network_io=True,
    cleanup=lambda: note("cleanup"),
)
def hold(args: Hold, ctx: Ctx, lease: Lease) -> dict[str, float]:
    time.sleep(args.seconds)
    return {"held": args.seconds}


if __name__ == "__main__":
    app.main()
