"""The network chapter's end point: todo_exit_codes.py plus an import over HTTP

uv run examples/tutorial/todo_network.py import --url https://example.com/todo.json
uv run treaty audit examples.tutorial.todo_network:app --strict
"""

import json
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal, Self, get_args

from treaty import Affects, App, Arg, Ctx, Exit, Flag, Format, Out

Priority = Literal["low", "normal", "high"]

# mkdir is the one other program a fix_command may run (REQ-C-030)
app = App("todo", version="1.0.0", description="Track todo items", companions=("mkdir",))
app.exit_code(
    "STORE_CORRUPT",
    79,
    description="The item file is not a valid todo file; nothing was changed",
    retryable=False,
    side_effects="none",
)
app.exit_code(
    "STORE_UNWRITABLE",
    80,
    description="The item file could not be written; the previous file is intact",
    retryable=False,
    side_effects="none",
)
app.exit_code(
    "FEED_INVALID",
    81,
    description="The URL did not answer with a list of items; nothing was changed",
    retryable=False,
    side_effects="none",
)


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
        try:
            return [Item(**raw) for raw in json.loads(self.path.read_text())]
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise Exit.STORE_CORRUPT(
                f"{self.path} is not a todo file",
                context={"path": str(self.path), "cause": str(exc)},
                fix_required="--db must name a todo file, or a path that does not exist yet",
                suggestion="pass --db with another path; the damaged file is left as it is",
            ) from exc

    def save(self, items: Sequence[Item]) -> None:
        # Write a sibling file, then rename it over the old one: a failure leaves the old
        # file untouched, which is what lets STORE_UNWRITABLE declare side_effects "none"
        partial = self.path.with_name(f".{self.path.name}.partial")
        try:
            partial.write_text(json.dumps([asdict(i) for i in items], indent=2))
            partial.replace(self.path)
        except OSError as exc:
            partial.unlink(missing_ok=True)
            missing = not self.path.parent.exists()
            raise Exit.STORE_UNWRITABLE(
                f"cannot write {self.path}: {exc.strerror}",
                context={"path": str(self.path), "errno": exc.errno},
                fix_required="the directory holding the item file must exist and be writable",
                fix_command=f"mkdir -p {shlex.quote(str(self.path.parent))}" if missing else None,
            ) from exc


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
    exit_codes=["STORE_CORRUPT", "STORE_UNWRITABLE"],
    examples=[("Add an urgent item", 'todo add "Buy milk" --priority high')],
)
def add(args: Add, ctx: Ctx, store: Store) -> Changed:
    items = store.load()
    next_id = max((i.id for i in items), default=0) + 1
    item = Item(id=next_id, text=args.text, priority=args.priority, done=False)
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
    sort_key="id",
    renderers={Format.PLAIN: render_items},
    exit_codes=["STORE_CORRUPT"],
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
    exit_codes=["NOT_FOUND", "STORE_CORRUPT", "STORE_UNWRITABLE"],
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
    deleted: list[Item] = Out(sort_key="id")
    would_affect: Affects | None = None


@app.command(
    "purge",
    description="Delete completed items",
    danger_level="destructive",
    exit_codes=["STORE_CORRUPT", "STORE_UNWRITABLE"],
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


@dataclass(frozen=True, slots=True)
class Import(Common):
    url: str = Flag(description="URL of a JSON list of items to add", pattern_type="url")


@dataclass(frozen=True, slots=True)
class Imported:
    effect: str
    added: list[Item] = Out(sort_key="id", external=True)


def feed_entries(body: bytes) -> list[tuple[str, Priority]] | None:
    """The text and priority of each entry, or None when the body is not a list of items"""
    try:
        entries = json.loads(body)
    except UnicodeDecodeError, json.JSONDecodeError:
        return None
    if not isinstance(entries, list):
        return None
    found: list[tuple[str, Priority]] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"text", "priority"}:
            return None
        text, priority = entry["text"], entry["priority"]
        if not isinstance(text, str) or priority not in get_args(Priority):
            return None
        found.append((text, priority))
    return found


@app.command(
    "import",
    description="Add the items listed at a URL",
    danger_level="mutating",
    has_network_io=True,
    exit_codes=["FEED_INVALID", "STORE_CORRUPT", "STORE_UNWRITABLE"],
    examples=[("Add a shared list", "todo import --url https://example.com/todo.json")],
)
def import_items(args: Import, ctx: Ctx, store: Store) -> Imported:
    response = ctx.http.get(args.url)
    entries = feed_entries(response.body) if response.status == 200 else None
    if entries is None:
        raise Exit.FEED_INVALID(
            f"{args.url} did not answer with a list of items",
            context={"url": args.url, "status": response.status},
            fix_required="--url must answer 200 with a JSON list of {text, priority} objects",
        )
    items = store.load()
    first = max((i.id for i in items), default=0) + 1
    added = [
        Item(id=first + n, text=text, priority=priority, done=False)
        for n, (text, priority) in enumerate(entries)
    ]
    if not added:
        return Imported(effect="noop", added=[])
    store.save([*items, *added])
    return Imported(effect="created", added=added)


if __name__ == "__main__":
    app.main()
