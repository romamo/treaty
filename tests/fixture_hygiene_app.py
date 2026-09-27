"""An app whose handlers misbehave on stdout, for output hygiene tests; runnable as a tool."""

import datetime as dt
import subprocess
from dataclasses import dataclass
from decimal import Decimal

from treaty import App, Ctx, Exit, Flag, NoArgs

app = App("hygienectl", version="1.0.0")
app.exit_code(
    "UPSTREAM_BUSY",
    79,
    description="The upstream is busy",
    retryable=True,
    side_effects="none",
    suggestion="wait a minute, then retry",
)
app.exit_code(
    "UPSTREAM_DOWN", 80, description="The upstream is down", retryable=True, side_effects="none"
)


@dataclass(frozen=True, slots=True)
class Login:
    api_token: str = Flag(description="Token for the upstream")


@dataclass(frozen=True, slots=True)
class Report:
    price: float
    count: int
    ratio: Decimal
    active: bool
    created_at: dt.datetime
    day: dt.date
    at: dt.time


@app.command(
    "chatty", description="Print to stdout and log to stderr", danger_level="safe", exit_codes=()
)
def chatty(args: NoArgs, ctx: Ctx) -> dict[str, str]:
    print("initialized")
    ctx.log("connecting", host="db.example.com")
    return {"status": "ok"}


@app.command(
    "colored", description="Return text a library colored", danger_level="safe", exit_codes=()
)
def colored(args: NoArgs, ctx: Ctx) -> dict[str, str]:
    return {"text": "\x1b[31mred\x1b[0m", "progress": "50%\r100%", "raw": "a\x00b\udcff"}


@app.command("login", description="Log in to the upstream", danger_level="safe", exit_codes=())
def login(args: Login, ctx: Ctx) -> dict[str, bool]:
    ctx.log(
        f"sending {args.api_token}",
        token="abc",
        env={"AWS_SECRET_ACCESS_KEY": "wJalr", "DB_PASS": "hunter2", "HOME": "/home/me"},
        headers={"Authorization": "Bearer abc", "Cookie": "sid=1", "Accept": "*/*"},
    )
    return {"logged_in": True}


@app.command(
    "children", description="Show what a child process inherits", danger_level="safe", exit_codes=()
)
def children(args: NoArgs, ctx: Ctx) -> dict[str, str]:
    shown = subprocess.run(
        ["sh", "-c", 'echo "$PAGER $GIT_PAGER $NO_COLOR"'],
        capture_output=True,
        text=True,
        check=True,
    )
    return {"child": shown.stdout.strip()}


@app.command(
    "color", description="Report whether a renderer may color", danger_level="safe", exit_codes=()
)
def color(args: NoArgs, ctx: Ctx) -> dict[str, bool]:
    return {"color": ctx.color}


@app.command(
    "report", description="Numbers, dates, and booleans", danger_level="safe", exit_codes=()
)
def report(args: NoArgs, ctx: Ctx) -> Report:
    return Report(
        price=1234.56,
        count=1000000,
        ratio=Decimal("0.075"),
        active=True,
        created_at=dt.datetime(2026, 9, 27, 10, 0, tzinfo=dt.UTC),
        day=dt.date(2026, 9, 27),
        at=dt.time(10, 0),
    )


@app.command(
    "naive", description="Return a datetime without a zone", danger_level="safe", exit_codes=()
)
def naive(args: NoArgs, ctx: Ctx) -> dict[str, dt.datetime]:
    return {"at": dt.datetime(2026, 9, 27, 10, 0)}


@app.command(
    "offset", description="Return a datetime in a fixed zone", danger_level="safe", exit_codes=()
)
def offset(args: NoArgs, ctx: Ctx) -> dict[str, dt.datetime]:
    return {"at": dt.datetime(2026, 9, 27, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))}


@app.command(
    "busy",
    description="Fail in a retryable way",
    exit_codes=["UPSTREAM_BUSY", "UPSTREAM_DOWN"],
    danger_level="safe",
)
def busy(args: NoArgs, ctx: Ctx) -> None:
    if ctx.env.get("DOWN"):
        raise Exit.UPSTREAM_DOWN("\x1b[31mupstream is down\x1b[0m")
    raise Exit.UPSTREAM_BUSY("upstream is busy")


if __name__ == "__main__":
    app.main()
