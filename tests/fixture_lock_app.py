"""An app whose commands share one lock, for ctx.lock tests; runnable as a tool."""

import time
from dataclasses import dataclass

from treaty import App, Ctx, Flag

app = App("lockctl", version="1.0.0")


@dataclass(frozen=True, slots=True)
class Hold:
    seconds: float = Flag(default=0.0, description="How long to hold the lock")
    wait: float | None = Flag(default=None, description="How long to wait for the lock")


@app.command("hold", description="Hold the lock a while", danger_level="safe", exit_codes=())
def hold(args: Hold, ctx: Ctx) -> dict[str, float]:
    with ctx.lock("deploy", wait=args.wait, retry_after_ms=250):
        time.sleep(args.seconds)
    return {"held": args.seconds}


if __name__ == "__main__":
    app.main()
