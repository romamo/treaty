"""An async handler with an async resource, and a flag read from stdin, for the signal
regressions of test_hardening; runnable. Each command writes "started" on stderr once
it is waiting, so a test signals it only then."""

import asyncio
import sys
from dataclasses import dataclass
from typing import Self

from treaty import App, Ctx, Flag, NoArgs

app = App("hardctl", version="1.0.0")


class Pool:
    @classmethod
    async def acquire(cls, args: object, ctx: Ctx) -> Self:
        return cls()

    async def release(self) -> None:
        sys.stderr.write("released\n")


async def waiting() -> dict[str, str]:
    try:
        sys.stderr.write("started\n")
        sys.stderr.flush()
        await asyncio.sleep(60)
    finally:
        sys.stderr.write("handler finally\n")
    return {}


@app.command("wait", description="Wait, untimed", danger_level="safe", exit_codes=(), timeout=None)
async def wait(args: NoArgs, ctx: Ctx, pool: Pool) -> dict[str, str]:
    return await waiting()


@app.command(
    "wait-timed", description="Wait, timed", danger_level="safe", exit_codes=(), timeout=30
)
async def wait_timed(args: NoArgs, ctx: Ctx, pool: Pool) -> dict[str, str]:
    return await waiting()


@dataclass(frozen=True, slots=True)
class Named:
    name: str = Flag(description="The name; - reads it from stdin", from_stdin=True)


@app.command("greet", description="Greet", danger_level="safe", exit_codes=())
def greet(args: Named, ctx: Ctx) -> dict[str, str]:
    return {"greeting": f"hello {args.name}"}


if __name__ == "__main__":
    sys.exit(app.run(sys.argv[1:]))
