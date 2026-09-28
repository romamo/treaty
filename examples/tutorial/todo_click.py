# /// script
# requires-python = ">=3.14"
# dependencies = ["click>=8.1"]
# ///
"""The click migration's starting point: todo_argparse.py written the click way

The inline metadata lets uv run it without adding click to the project:

uv run examples/tutorial/todo_click.py --db tmp/todo.json add "Buy milk" --priority high
uv run examples/tutorial/todo_click.py --db tmp/todo.json list
"""

import json
from pathlib import Path
from typing import Any

import click

Item = dict[str, Any]


def load(db: Path) -> list[Item]:
    if not db.exists():
        return []
    items: list[Item] = json.loads(db.read_text())
    return items


def save(db: Path, items: list[Item]) -> None:
    db.write_text(json.dumps(items, indent=2))


@click.group(help="Track todo items")
@click.version_option("1.0.0", prog_name="todo")
@click.option(
    "--db",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path.home() / ".todo.json",
    help="Where the items are stored",
)
@click.pass_context
def cli(ctx: click.Context, db: Path) -> None:
    ctx.obj = db


@cli.command(help="Add an item")
@click.argument("text")
@click.option(
    "--priority",
    type=click.Choice(["low", "normal", "high"]),
    default="normal",
    help="How urgent it is",
)
@click.pass_obj
def add(db: Path, text: str, priority: str) -> None:
    items = load(db)
    next_id = max((i["id"] for i in items), default=0) + 1
    items.append({"id": next_id, "text": text, "priority": priority, "done": False})
    save(db, items)
    click.echo(f"Added #{next_id}: {text}")


@cli.command("list", help="List open items")
@click.option("-a", "--all", "show_all", is_flag=True, help="Include completed items")
@click.pass_obj
def list_items(db: Path, show_all: bool) -> None:
    for item in load(db):
        if show_all or not item["done"]:
            mark = "x" if item["done"] else " "
            click.echo(f"[{mark}] #{item['id']} {item['text']} ({item['priority']})")


@cli.command(help="Mark an item completed")
@click.argument("id", type=int)
@click.pass_obj
def done(db: Path, id: int) -> None:
    items = load(db)
    for item in items:
        if item["id"] == id:
            item["done"] = True
            save(db, items)
            click.echo(f"Completed #{id}")
            return
    raise click.ClickException(f"no item #{id}")


@cli.command(help="Delete completed items")
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation")
@click.pass_obj
def purge(db: Path, yes: bool) -> None:
    items = load(db)
    completed = [i for i in items if i["done"]]
    if not yes:
        click.confirm(f"Delete {len(completed)} completed items?", abort=True)
    save(db, [i for i in items if not i["done"]])
    click.echo(f"Deleted {len(completed)} items")


if __name__ == "__main__":
    cli()
