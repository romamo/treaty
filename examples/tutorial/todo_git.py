"""The wrapping chapter's end point: todo_exit_codes.py plus save, which commits the item
file to the git repository it is in

uv run examples/tutorial/todo_git.py save --message "Plan the week" --db repo/todo.json
uv run treaty audit examples.tutorial.todo_git:app --strict
"""

import json
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal, Self

from treaty import Affects, App, Arg, Ctx, Exit, Flag, Format, Out, Subprocess

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
    "NOT_A_REPOSITORY",
    82,
    description="The item file is not in a git repository; nothing was committed",
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
class Save(Common):
    message: str = Flag(default="Update todo items", description="Commit message", multiline=True)


@dataclass(frozen=True, slots=True)
class Saved:
    effect: str
    commit: str | None = Out(external=True)
    """The new commit's hash, as git printed it; null when the item file had no changes"""


@app.command(
    "save",
    description="Commit the item file to the git repository it is in",
    danger_level="mutating",
    exit_codes=["NOT_A_REPOSITORY"],
    required_tools={"git": "2.30.0"},
    subprocess=Subprocess(
        "git",
        user_controlled_args=("db",),
        hardcoded_args=("rev-parse", "add", "diff", "--cached", "commit", "--file", "-"),
    ),
    examples=[("Commit the items with a message", 'todo save --message "Plan the week"')],
)
def save(args: Save, ctx: Ctx, store: Store) -> Saved:
    here = store.path.parent
    inside = ctx.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=here, check=False)
    if inside.returncode != 0:
        raise Exit.NOT_A_REPOSITORY(
            f"{here} is not in a git repository",
            context={"directory": str(here), "git": inside.stderr.strip()},
            fix_required="--db must name a file inside a git working tree",
        )
    # "--" ends git's options: a file name can never be read as one
    ctx.run(["git", "add", "--", store.path.name], cwd=here)
    staged = ctx.run(["git", "diff", "--cached", "--quiet"], cwd=here, check=False)
    if staged.returncode == 0:
        return Saved(effect="noop", commit=None)
    # The message goes in on stdin, never as an argument: it is free text
    ctx.run(["git", "commit", "--file", "-"], cwd=here, input=args.message)
    head = ctx.run(["git", "rev-parse", "HEAD"], cwd=here)
    return Saved(effect="created", commit=head.stdout.strip())


if __name__ == "__main__":
    app.main()
