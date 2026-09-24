"""Deliberately imperfect app for audit tests."""

from dataclasses import dataclass

from treaty import App, Arg, Ctx, Flag

app = App("shopctl", version="0.1")
app.exit_code("FLAKY", 79, description="Upstream hiccup", retryable=True, side_effects="none")


@dataclass(frozen=True, slots=True)
class Name:
    name: str = Arg(description="Name")


@dataclass(frozen=True, slots=True)
class Wide:
    name: str = Arg(description="Name")
    sku: str = Flag(default="", description="SKU")
    price: float = Flag(default=0.0, description="Price")
    qty: int = Flag(default=0, description="Quantity")
    note: str = Flag(default="", description="Note")


@app.command("delete-item", description="Delete an item")
def delete_item(args: Name, ctx: Ctx) -> dict[str, object]:
    return {"deleted": args.name}


@app.command("create-item", description="Create", danger_level="mutating", exit_codes=["FLAKY"])
def create_item(args: Wide, ctx: Ctx) -> dict[str, object]:
    import urllib.request

    return {"created": args.name, "via": urllib.request.__name__}


@app.command(
    "good",
    description="Fully declared",
    examples=[("Run it", "shopctl good x")],
    has_network_io=True,
    cleanup=lambda: None,
)
def good(args: Name, ctx: Ctx) -> Name:
    return args
