"""A CLI that keeps a shared package's pydantic models as they are, through app.args_adapter

uv run python -m examples.tutorial.market_quotes lookup AAPL --report-price
"""

from typing import Any

from pydantic import BaseModel, Field

from treaty import App, Ctx


# The shared package's models, unchanged: the json_schema_extra "treaty" key is the one
# addition, for what pydantic has no word for, such as a positional
class GlobalArgs(BaseModel):
    currency: str = Field("USD", pattern="^[A-Z]{3}$", description="Currency to quote in")


class SearchArgs(GlobalArgs):
    query: str = Field(
        description="Symbol, ISIN, or name", json_schema_extra={"treaty": {"positional": True}}
    )


app = App("quotes", version="1.0.0", description="Quotes over the shared pydantic models")


def model_schema(cls: type[BaseModel]) -> dict[str, Any]:
    return cls.model_json_schema(by_alias=False)


def model_validate(cls: type[BaseModel], data: dict[str, object]) -> BaseModel:
    return cls.model_validate(data, by_name=True, by_alias=False)


app.args_adapter(BaseModel, schema=model_schema, validate=model_validate)


class LookupCommand(SearchArgs):
    report_price: bool = Field(False, description="Include the current price")


@app.command(
    "lookup",
    description="Look up a security by symbol, ISIN, or name",
    danger_level="safe",
    exit_codes=[],
    examples=[("Look up Apple with its price", "quotes lookup AAPL --report-price")],
)
def lookup(args: LookupCommand, ctx: Ctx) -> dict[str, object]:
    price = 187.5 if args.report_price else None
    return {"symbol": args.query.upper(), "currency": args.currency, "price": price}


if __name__ == "__main__":
    app.main()
