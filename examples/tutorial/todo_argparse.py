"""The migration tutorial's starting point: a typical argparse CLI

It works for a person at a terminal and fails an agent in the usual ways: prose on stdout,
a prompt that blocks, exit 1 for every failure, and no way to list its commands.

uv run examples/tutorial/todo_argparse.py --db tmp/todo.json add "Buy milk" --priority high
uv run examples/tutorial/todo_argparse.py --db tmp/todo.json list
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

Item = dict[str, Any]


def load(db: Path) -> list[Item]:
    if not db.exists():
        return []
    items: list[Item] = json.loads(db.read_text())
    return items


def save(db: Path, items: list[Item]) -> None:
    db.write_text(json.dumps(items, indent=2))


def cmd_add(args: argparse.Namespace) -> None:
    items = load(args.db)
    item = {"id": len(items) + 1, "text": args.text, "priority": args.priority, "done": False}
    items.append(item)
    save(args.db, items)
    print(f"Added #{item['id']}: {item['text']}")


def cmd_list(args: argparse.Namespace) -> None:
    for item in load(args.db):
        if args.all or not item["done"]:
            mark = "x" if item["done"] else " "
            print(f"[{mark}] #{item['id']} {item['text']} ({item['priority']})")


def cmd_done(args: argparse.Namespace) -> None:
    items = load(args.db)
    for item in items:
        if item["id"] == args.id:
            item["done"] = True
            save(args.db, items)
            print(f"Completed #{args.id}")
            return
    print(f"error: no item #{args.id}", file=sys.stderr)
    sys.exit(1)


def cmd_purge(args: argparse.Namespace) -> None:
    items = load(args.db)
    done = [i for i in items if i["done"]]
    if not args.yes:
        answer = input(f"Delete {len(done)} completed items? [y/N] ")
        if answer.lower() != "y":
            print("Aborted")
            return
    save(args.db, [i for i in items if not i["done"]])
    print(f"Deleted {len(done)} items")


def main() -> None:
    parser = argparse.ArgumentParser(prog="todo", description="Track todo items")
    parser.add_argument(
        "--db", type=Path, default=Path.home() / ".todo.json", help="Where the items are stored"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="Add an item")
    add.add_argument("text", help="What to do")
    add.add_argument(
        "--priority", choices=["low", "normal", "high"], default="normal", help="How urgent it is"
    )
    add.set_defaults(func=cmd_add)

    ls = sub.add_parser("list", help="List open items")
    ls.add_argument("-a", "--all", action="store_true", help="Include completed items")
    ls.set_defaults(func=cmd_list)

    done = sub.add_parser("done", help="Mark an item completed")
    done.add_argument("id", type=int, help="Item number")
    done.set_defaults(func=cmd_done)

    purge = sub.add_parser("purge", help="Delete completed items")
    purge.add_argument("-y", "--yes", action="store_true", help="Do not ask for confirmation")
    purge.set_defaults(func=cmd_purge)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
