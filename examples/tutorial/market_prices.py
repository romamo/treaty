"""One of two CLIs that share market_args.py: lookup and history from a prices provider

uv run python -m examples.tutorial.market_prices lookup AAPL --report-price
uv run python -m examples.tutorial.market_prices history AAPL --days 5
"""

from dataclasses import dataclass

from treaty import App, Ctx, Flag

from .market_args import HISTORY_RULES, HistoryArgs, SearchArgs

app = App("prices", version="1.0.0", description="Quotes from the prices provider")


@dataclass(frozen=True, slots=True)
class Lookup(SearchArgs):
    report_price: bool = Flag(default=False, description="Include the current price")


@dataclass(frozen=True, slots=True)
class Security:
    symbol: str
    currency: str
    price: float | None


@app.command(
    "lookup",
    description="Look up a security by symbol, ISIN, or name",
    danger_level="safe",
    exit_codes=[],
    examples=[("Look up Apple with its price", "prices lookup AAPL --report-price")],
)
def lookup(args: Lookup, ctx: Ctx) -> Security:
    price = 187.5 if args.report_price else None
    return Security(symbol=args.query.upper(), currency=args.currency, price=price)


@dataclass(frozen=True, slots=True)
class History:
    symbol: str
    days: int | None
    start: str | None


@app.command(
    "history",
    description="Show a security's price history",
    danger_level="safe",
    exit_codes=[],
    requires=HISTORY_RULES,
    examples=[("The last five days of Apple", "prices history AAPL --days 5")],
)
def history(args: HistoryArgs, ctx: Ctx) -> History:
    return History(symbol=args.symbol.upper(), days=args.days, start=args.start)


if __name__ == "__main__":
    app.main()
