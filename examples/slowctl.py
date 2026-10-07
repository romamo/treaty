"""Timeout and cancellation demo.

uv run examples/slowctl.py fetch --seconds 5 --timeout 0.2
uv run examples/slowctl.py fetch --seconds 5 --timeout 0 &  kill -TERM $!
"""

import sys
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from treaty import App, Ctx, Flag

app = App("slowctl", version="0.1.0", default_timeout=2)


@dataclass(frozen=True, slots=True)
class Fetch:
    seconds: float = Flag(default=0.0, description="How long the fake network call blocks")
    cleanup_seconds: float = Flag(
        default=0.0, description="How long cleanup takes when the run ends"
    )


def release_resources() -> None:
    sys.stderr.write("cleanup: releasing resources\n")
    time.sleep(_cleanup_seconds[0])


_cleanup_seconds = [0.0]


def fake_upstream(seconds: float, *, proxies: Mapping[str, str]) -> None:
    """Stands in for a library's own HTTP client, which takes the proxies --proxy and
    --no-proxy resolved"""
    time.sleep(seconds)


@app.command(
    "fetch",
    description="Pretend to call a slow upstream",
    has_network_io=True,
    cleanup=release_resources,
    danger_level="safe",
    exit_codes=(),
)
def fetch(args: Fetch, ctx: Ctx) -> dict[str, object]:
    _cleanup_seconds[0] = args.cleanup_seconds
    ctx.log("fetching", seconds=args.seconds)  # with --verbose, tests wait for it to signal
    fake_upstream(args.seconds, proxies=ctx.network.proxies)
    return {"slept": args.seconds, "timeout_s": ctx.timeout.seconds}


@dataclass(frozen=True, slots=True)
class Serve:
    interval: float = Flag(default=0.2, description="Seconds between heartbeats")


@app.command(
    "serve",
    description="Announce a URL, then heartbeat until stopped",
    streaming=True,
    endless=True,
    danger_level="safe",
    exit_codes=(),
)
def serve(args: Serve, ctx: Ctx) -> Iterator[dict[str, object]]:
    yield {"event": "listening", "url": "http://127.0.0.1:0/"}
    try:
        n = 0
        while True:
            time.sleep(args.interval)
            n += 1
            yield {"event": "heartbeat", "n": n}
    finally:
        sys.stderr.write("serve: server closed\n")


if __name__ == "__main__":
    app.main()
