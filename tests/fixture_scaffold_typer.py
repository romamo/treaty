"""A small typer CLI for treaty scaffold-from: a root option, a nested group, and the
option shapes typer users write"""

from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer


class Kind(StrEnum):
    cash = "cash"
    card = "card"


app = typer.Typer(help="Keep a ledger")
transaction = typer.Typer(help="Work with transactions")
app.add_typer(transaction, name="transaction")


@app.callback()
def main(
    ledger: Annotated[Path, typer.Option(help="The ledger file", envvar="LEDGER_FILE")] = Path(
        "main.beancount"
    ),
) -> None:
    pass


@transaction.command("list", help="List transactions")
def list_transactions(
    account: Annotated[str, typer.Argument(help="Account to list")],
    limit_to: Annotated[int, typer.Option("--limit-to", "-n", help="How many")] = 10,
    kind: Annotated[Kind, typer.Option(help="Payment kind")] = Kind.cash,
    tag: Annotated[list[str], typer.Option(help="Tags to match")] = [],  # noqa: B006
    format: Annotated[str, typer.Option(help="Output style")] = "table",
    verbose: Annotated[bool, typer.Option(help="Say more")] = False,
) -> None:
    pass


@transaction.command("add", help="Add a transaction")
def add_transaction(
    payee: Annotated[str, typer.Argument(help="Who was paid")],
    amount: Annotated[float, typer.Option(help="How much")],
    note: Annotated[str | None, typer.Argument(help="A note")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask")] = False,
) -> None:
    pass


@app.command(help="Check the ledger")
def check(strict: bool = False) -> None:
    pass


@app.command(help="Print the version")
def version() -> None:
    pass
