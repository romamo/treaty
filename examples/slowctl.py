"""Timeout and cancellation demo.

uv run examples/slowctl.py fetch --seconds 5 --timeout 0.2
uv run examples/slowctl.py fetch --seconds 5 --timeout 0 &  kill -TERM $!
"""

import sys
import time
from dataclasses import dataclass

from treaty import App, Ctx, Flag

app = App("slowctl", version="0.1", default_timeout=2)


@dataclass(frozen=True, slots=True)
class Fetch:
    seconds: float = Flag(default=0.0, description="How long the fake network call blocks")
    cleanup_seconds: float = Flag(default=0.0, description="How long cleanup takes after a signal")


def release_resources() -> None:
    sys.stderr.write("cleanup: releasing resources\n")
    time.sleep(_cleanup_seconds[0])


_cleanup_seconds = [0.0]


@app.command(
    "fetch",
    description="Pretend to call a slow upstream",
    has_network_io=True,
    cleanup=release_resources,
)
def fetch(args: Fetch, ctx: Ctx) -> dict[str, object]:
    _cleanup_seconds[0] = args.cleanup_seconds
    time.sleep(args.seconds)
    return {"slept": args.seconds, "timeout_s": ctx.timeout.seconds}


if __name__ == "__main__":
    app.main()
