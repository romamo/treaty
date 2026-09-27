"""The migration tutorial's end point: todo_argparse.py on treaty

uv run examples/tutorial/todo_treaty.py add "Buy milk" --priority high --db tmp/todo.json
uv run examples/tutorial/todo_treaty.py list --db tmp/todo.json --format plain
uv run examples/tutorial/todo_treaty.py manifest
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal, Self

from treaty import Affects, App, Arg, Ctx, Exit, Flag, Format

Priority = Literal["low", "normal", "high"]

app = App("todo", version="1.0.0", description="Track todo items")


@dataclass(frozen=True, slots=True)
class Item:
    id: int
    text: str
    priority: Priority
    done: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class Common:
    db: Path | None = Flag(default=None, description="Item file; default ~/.todo.json")


@dataclass(frozen=True, slots=True)
class Store:
    path: Path

    @classmethod
    def acquire(cls, args: Common, ctx: Ctx) -> Self:
        return cls(args.db if args.db is not None else Path.home() / ".todo.json")

    def load(self) -> list[Item]:
        if not self.path.exists():
            return []
        return [Item(**raw) for raw in json.loads(self.path.read_text())]

    def save(self, items: Sequence[Item]) -> None:
        self.path.write_text(json.dumps([asdict(i) for i in items], indent=2))


@dataclass(frozen=True, slots=True)
class Add(Common):
    text: str = Arg(description="What to do")
    priority: Priority = Flag(default="normal", description="How urgent it is")


@dataclass(frozen=True, slots=True)
class Changed:
    effect: str
    item: Item


@app.command(
    "add",
    description="Add an item",
    danger_level="mutating",
    exit_codes=[],
    examples=[("Add an urgent item", 'todo add "Buy milk" --priority high')],
)
def add(args: Add, ctx: Ctx, store: Store) -> Changed:
    items = store.load()
    item = Item(id=len(items) + 1, text=args.text, priority=args.priority, done=False)
    store.save([*items, item])
    return Changed(effect="created", item=item)


@dataclass(frozen=True, slots=True)
class ListArgs(Common):
    all: bool = Flag(default=False, short="a", description="Include completed items")


def render_items(data: Sequence[Mapping[str, object]]) -> str:
    lines = []
    for item in data:
        mark = "x" if item["done"] else " "
        lines.append(f"[{mark}] #{item['id']} {item['text']} ({item['priority']})\n")
    return "".join(lines)


@app.command(
    "list",
    description="List open items",
    danger_level="safe",
    renderers={Format.PLAIN: render_items},
    exit_codes=[],
    examples=[("List every item, completed ones too", "todo list --all")],
)
def list_items(args: ListArgs, ctx: Ctx, store: Store) -> list[Item]:
    return [i for i in store.load() if args.all or not i.done]


@dataclass(frozen=True, slots=True)
class Done(Common):
    id: int = Arg(description="Item number")


@app.command(
    "done",
    description="Mark an item completed",
    danger_level="mutating",
    exit_codes=["NOT_FOUND"],
    examples=[("Complete item 3", "todo done 3")],
)
def done(args: Done, ctx: Ctx, store: Store) -> Changed:
    items = store.load()
    for n, item in enumerate(items):
        if item.id == args.id:
            if item.done:
                return Changed(effect="noop", item=item)
            items[n] = replace(item, done=True)
            store.save(items)
            return Changed(effect="updated", item=items[n])
    raise Exit.NOT_FOUND(
        f"no item #{args.id}",
        context={"id": args.id},
        suggestion="todo list --all shows every item number",
    )


@dataclass(frozen=True, slots=True)
class Purge(Common):
    dry_run: bool = Flag(default=False, description="Show what would be deleted, delete nothing")


@dataclass(frozen=True, slots=True)
class Purged:
    effect: str
    deleted: list[Item]
    would_affect: Affects | None = None


@app.command(
    "purge",
    description="Delete completed items",
    danger_level="destructive",
    exit_codes=[],
    examples=[("Delete completed items", "todo purge --confirm-destructive")],
)
def purge(args: Purge, ctx: Ctx, store: Store) -> Purged:
    items = store.load()
    completed = [i for i in items if i.done]
    if args.dry_run:
        affects = Affects(
            f"Deletes {len(completed)} completed items",
            tuple(f"item/{i.id}" for i in completed),
            len(completed),
        )
        return Purged(effect="would_delete", deleted=completed, would_affect=affects)
    if not completed:
        return Purged(effect="noop", deleted=[])
    store.save([i for i in items if not i.done])
    return Purged(effect="deleted", deleted=completed)


if __name__ == "__main__":
    app.main()
