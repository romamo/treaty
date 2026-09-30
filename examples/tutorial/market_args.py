"""Argument models two market-data CLIs share, as a shared package would hold them

market_prices.py and market_funds.py build their commands' arguments on these classes.
"""

from dataclasses import dataclass
from datetime import date

from treaty import Arg, Excludes, Flag, ParseError


@dataclass(frozen=True, slots=True, kw_only=True)
class GlobalArgs:
    currency: str = Flag(default="USD", pattern="[A-Z]{3}", description="Currency to quote in")


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchArgs(GlobalArgs):
    query: str = Arg(description="Symbol, ISIN, or name")


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoryArgs(GlobalArgs):
    symbol: str = Arg(description="Symbol to fetch")
    days: int | None = Flag(default=None, description="Days back from today")
    start: str | None = Flag(default=None, description="First day, YYYY-MM-DD")

    def __post_init__(self) -> None:
        if self.start is not None:
            try:
                date.fromisoformat(self.start)
            except ValueError:
                raise ParseError(f"--start {self.start!r} is not a YYYY-MM-DD date") from None


HISTORY_RULES = [Excludes("days", prohibited=("start",))]
"""The rules between HistoryArgs' flags, for requires= on each command that takes them"""
