"""A producer and a consumer of typed records (#32); runnable as a tool."""

from collections.abc import Iterator
from dataclasses import dataclass

from treaty import App, Ctx, Exit, Flag, NoArgs, ParseError

app = App("secctl", version="1.0.0", default_timeout=5)


@dataclass(frozen=True, slots=True)
class Sec:
    isin: str
    figi: str
    ticker: str
    weight: float = 1.0

    def __post_init__(self) -> None:
        if len(self.isin) != 12:
            raise ParseError("an ISIN has 12 characters", context={"field": "isin"})


@dataclass(frozen=True, slots=True)
class Resolved:
    isin: str
    ticker: str


@dataclass(frozen=True, slots=True)
class Summary:
    count: int
    tickers: list[str]


SECURITIES = (
    Sec("IE00B3RBWM25", "BBG000BDTF76", "VWRL"),
    Sec("LU1900066033", "BBG00MGQZSP1", "LYXGOLD"),
)


@dataclass(frozen=True, slots=True)
class FeedArgs:
    fail: bool = Flag(default=False, description="Fail after the first security")


@app.command(
    "securities",
    description="Stream the securities",
    danger_level="safe",
    exit_codes=(),
    streaming=True,
)
def securities(args: FeedArgs, ctx: Ctx) -> Iterator[Sec]:
    yield SECURITIES[0]
    if args.fail:
        raise Exit.PRECONDITION(
            "the feed went away",
            code="FEED_GONE",
            context={"api_key": "sk-live-0123456789abcdef", "feed": "primary"},
        )
    yield SECURITIES[1]


@app.command("list", description="List the securities", danger_level="safe", exit_codes=())
def listing(args: NoArgs, ctx: Ctx) -> list[Sec]:
    return list(SECURITIES)


@app.command(
    "resolve",
    description="Resolve each security as it arrives",
    danger_level="safe",
    exit_codes=(),
    streaming=True,
    stdin_records=Sec,
)
def resolve(args: NoArgs, ctx: Ctx) -> Iterator[Resolved]:
    for sec in ctx.stdin_records:
        yield Resolved(sec.isin, sec.ticker)


@app.command(
    "summary",
    description="Count the securities",
    danger_level="safe",
    exit_codes=(),
    stdin_records=Sec,
)
def summary(args: NoArgs, ctx: Ctx) -> Summary:
    secs: list[Sec] = list(ctx.stdin_records)
    return Summary(len(secs), [s.ticker for s in secs])


if __name__ == "__main__":
    app.main()
