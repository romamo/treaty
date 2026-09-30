"""The other CLI that shares market_args.py: lookup from a funds provider

uv run python -m examples.tutorial.market_funds lookup "world index" --currency EUR
"""

from dataclasses import dataclass

from examples.tutorial.market_args import SearchArgs
from treaty import App, Ctx

app = App("funds", version="1.0.0", description="Quotes from the funds provider")


@dataclass(frozen=True, slots=True)
class Fund:
    name: str
    currency: str


@app.command(
    "lookup",
    description="Look up a fund by symbol, ISIN, or name",
    danger_level="safe",
    exit_codes=[],
    examples=[("Look up a fund quoted in euros", 'funds lookup "world index" --currency EUR')],
)
def lookup(args: SearchArgs, ctx: Ctx) -> Fund:
    return Fund(name=args.query, currency=args.currency)


if __name__ == "__main__":
    app.main()
