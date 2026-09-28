# /// script
# requires-python = ">=3.14"
# dependencies = ["typer>=0.12"]
# ///
"""The typer migration's starting point: todo_argparse.py written the typer way

The inline metadata lets uv run it without adding typer to the project:

uv run examples/tutorial/todo_typer.py --db tmp/todo.json add "Buy milk" --priority high
uv run examples/tutorial/todo_typer.py --db tmp/todo.json list
"""

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

Item = dict[str, Any]


class Priority(StrEnum):
    low = "low"
    normal = "normal"
    high = "high"


app = typer.Typer(help="Track todo items")
DEFAULT_DB = Path.home() / ".todo.json"


def load(db: Path) -> list[Item]:
    if not db.exists():
        return []
    items: list[Item] = json.loads(db.read_text())
    return items


def save(db: Path, items: list[Item]) -> None:
    db.write_text(json.dumps(items, indent=2))


@app.callback()
def main(
    ctx: typer.Context,
    db: Annotated[Path, typer.Option(help="Where the items are stored")] = DEFAULT_DB,
) -> None:
    ctx.obj = db


@app.command(help="Add an item")
def add(
    ctx: typer.Context,
    text: Annotated[str, typer.Argument(help="What to do")],
    priority: Annotated[Priority, typer.Option(help="How urgent it is")] = Priority.normal,
) -> None:
    items = load(ctx.obj)
    next_id = max((i["id"] for i in items), default=0) + 1
    items.append({"id": next_id, "text": text, "priority": priority, "done": False})
    save(ctx.obj, items)
    typer.echo(f"Added #{next_id}: {text}")


@app.command("list", help="List open items")
def list_items(
    ctx: typer.Context,
    show_all: Annotated[bool, typer.Option("--all", "-a", help="Include completed items")] = False,
) -> None:
    for item in load(ctx.obj):
        if show_all or not item["done"]:
            mark = "x" if item["done"] else " "
            typer.echo(f"[{mark}] #{item['id']} {item['text']} ({item['priority']})")


@app.command(help="Mark an item completed")
def done(ctx: typer.Context, id: Annotated[int, typer.Argument(help="Item number")]) -> None:
    items = load(ctx.obj)
    for item in items:
        if item["id"] == id:
            item["done"] = True
            save(ctx.obj, items)
            typer.echo(f"Completed #{id}")
            return
    typer.echo(f"error: no item #{id}", err=True)
    raise typer.Exit(code=1)


@app.command(help="Delete completed items")
def purge(
    ctx: typer.Context,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation")] = False,
) -> None:
    items = load(ctx.obj)
    completed = [i for i in items if i["done"]]
    if not yes:
        typer.confirm(f"Delete {len(completed)} completed items?", abort=True)
    save(ctx.obj, [i for i in items if not i["done"]])
    typer.echo(f"Deleted {len(completed)} items")


if __name__ == "__main__":
    app()
