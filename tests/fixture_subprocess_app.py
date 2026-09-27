"""An app whose handlers run other programs, for subprocess tests; runnable as a tool."""

from dataclasses import dataclass
from pathlib import Path

from treaty import App, Ctx, Flag

app = App("procctl", version="1.0.0")


@dataclass(frozen=True, slots=True)
class Hold:
    pidfile: Path = Flag(description="Where the child writes its pid")


@app.command(
    "hold",
    description="Run a child that sleeps, with the handler on the main thread",
    danger_level="safe",
    exit_codes=(),
    timeout=None,
)
def hold(args: Hold, ctx: Ctx) -> dict[str, str]:
    script = 'echo $$ > "$1"; exec sleep 30'
    ctx.run(["sh", "-c", script, "_", args.pidfile])
    return {"status": "finished"}


@app.command(
    "hold-threaded",
    description="Run a child that sleeps, with the handler on a worker thread",
    danger_level="safe",
    exit_codes=(),
)
def hold_threaded(args: Hold, ctx: Ctx) -> dict[str, str]:
    return hold(args, ctx)


if __name__ == "__main__":
    app.main()
