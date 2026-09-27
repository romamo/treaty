# Migrate an argparse CLI

**Goal:** every command of an argparse CLI runs on treaty, with the same features and a
manifest an agent can read

**You need:** a working argparse CLI, and treaty installed ([Before you start](../index.md#before-you-start))

**Done when:** your tests pass, and `treaty audit` reports only `exit-codes` warnings, which
the next chapter clears:

```bash
uv run treaty audit examples.tutorial.todo_treaty:app \
  | jq -c '[.data.next_steps[] | select(.severity == "warning") | .rule] | unique'
# ["exit-codes"]
```

The chapter migrates one small CLI, `todo`, from start to finish. The starting point is
[`examples/tutorial/todo_argparse.py`](../../../examples/tutorial/todo_argparse.py), the
result is [`examples/tutorial/todo_treaty.py`](../../../examples/tutorial/todo_treaty.py).
Keep both open; the steps below show the parts that change.

## What is wrong with the argparse version

Nothing, for a person at a terminal. Run it the way an agent does, with stdout piped and no
terminal on stdin, and it fails in the four common ways:

```bash
$ todo --db tmp/b.json done 9
error: no item #9                     # prose on stderr, exit 1
$ todo --db tmp/b.json purge </dev/null
Delete 0 completed items? [y/N] Traceback (most recent call last):
  ...
EOFError: EOF when reading a line     # a prompt, then a crash, exit 1
```

- **Output is prose.** `Added #3: Buy milk` has to be parsed with a regex that breaks when
  the wording changes
- **Every failure is exit 1.** A missing item, a full disk, and a bug look the same, so an
  agent cannot tell whether to retry, fix its arguments, or stop
- **It prompts.** `purge` waits on `input()`; an agent either hangs or crashes it
- **It cannot describe itself.** The only way to learn the commands is `--help` text for
  each one

treaty fixes the first three by construction and the fourth with `manifest`.

## Step 1: Take inventory

Before writing code, list what the CLI does. The table becomes the manifest, so every
column matters:

| Command | Arguments | Writes? | Fails when | Danger level |
| --- | --- | --- | --- | --- |
| `add` | `text`, `--priority` (low, normal, high) | appends an item | never on purpose | `mutating` |
| `list` | `--all` / `-a` | nothing | never on purpose | `safe` |
| `done` | `id` (int) | updates an item | the item does not exist | `mutating` |
| `purge` | `--yes` / `-y` | deletes items | never on purpose | `destructive` |
| all | `--db PATH` | | | |

Danger level is the column argparse never asked for. `safe` commands only read,
`mutating` commands change state, and `destructive` commands remove something that cannot
be restored. It decides which framework flags a command gets and whether it can run without
confirmation.

**Check:** every subparser has a row, and every `sys.exit`, `parser.error`, and `input()`
call in the old code shows up in the "Fails when" or "Writes?" column

## Step 2: Create the app

`ArgumentParser` becomes one `App`. It needs a version, which `todo --version` and the
manifest report:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
app = App("todo", version="1.0.0", description="Track todo items")
```

## Step 3: Move shared options onto a base class

argparse puts `--db` on the root parser, before the subcommand. treaty has no root-level
command flags: every flag belongs to a command and goes after the command path. Declare the
shared flag once on a `kw_only` base dataclass that every command's arguments extend:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
@dataclass(frozen=True, slots=True, kw_only=True)
class Common:
    db: Path | None = Flag(default=None, description="Item file; default ~/.todo.json")
```

The argparse default was `Path.home() / ".todo.json"`, computed at import. A default in the
manifest should be a fixed value, so the flag defaults to `None` and the fallback moves to
the code that opens the store. That code was `load()` and `save()`, called by every
handler; it becomes a resource, which treaty builds once per run from the arguments and
hands to any handler that asks for it:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
@dataclass(frozen=True, slots=True)
class Store:
    path: Path

    @classmethod
    def acquire(cls, args: Common, ctx: Ctx) -> Self:
        return cls(args.db if args.db is not None else Path.home() / ".todo.json")
```

Typing `db` as `Path` also gets the argument checked before any handler runs: `..`
segments, percent-encoded bytes, and null bytes exit 2.

**Check:** the old order fails with the new order in the suggestion, so a caller that still
uses it is told how to fix the call

```bash
$ todo --db tmp/todo.json list
# exit 2, error.suggestion: "flags go after the command: todo list [arguments] --db"
```

## Step 4: Migrate one command end to end

A subparser, its arguments, and its `func` become three things: an arguments dataclass, a
decorated handler, and a return type.

Before:

<!-- file: examples/tutorial/todo_argparse.py -->
```python
add = sub.add_parser("add", help="Add an item")
add.add_argument("text", help="What to do")
add.add_argument(
    "--priority", choices=["low", "normal", "high"], default="normal", help="How urgent it is"
)
add.set_defaults(func=cmd_add)
```

After:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
@dataclass(frozen=True, slots=True)
class Add(Common):
    text: str = Arg(description="What to do")
    priority: Priority = Flag(default="normal", description="How urgent it is")
```

`Priority` is `Literal["low", "normal", "high"]`, so `choices=` moves into the type and the
manifest lists the allowed values. A `StrEnum` works the same way.

The handler returns data instead of printing it:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
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
```

Four things are new:

- **`danger_level=` and `exit_codes=`** are required on every command. `add` changes state,
  so it is `mutating`; its own failures come in the next chapter, so the list is empty for
  now. A read-only command such as `list` is `safe`
- **`examples=`** is the first thing an agent copies. The audit's `describe` rule asks for
  one on every command
- **The return type** is a dataclass, so the manifest carries an `output_schema` and the
  agent knows the shape of `data` before it calls
- **`effect`** is required on mutating and destructive results: `created`, `updated`,
  `deleted`, or `noop`. An agent that retries reads `noop` and knows nothing changed

Mutating commands also get `--idempotency-key` for free. `add` is not safe to repeat (a
retry adds a second item), and the key is how a caller makes it safe: the second call with
the same key returns the first result without running the handler.

**Check:**

```bash
$ todo add "Buy milk" --priority high --db tmp/todo.json
{"data":{"effect":"created","item":{"done":false,"id":1,"priority":"high","text":"Buy milk"}},"error":null,"meta":{...},"ok":true,"warnings":[]}
$ todo add "x" --priority urgent --db tmp/todo.json
# exit 2, error.message: "'priority' must be one of low, normal, high."
```

## Step 5: Replace `sys.exit(1)` with a named exit code

Before:

<!-- file: examples/tutorial/todo_argparse.py -->
```python
print(f"error: no item #{args.id}", file=sys.stderr)
sys.exit(1)
```

After:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
    raise Exit.NOT_FOUND(
        f"no item #{args.id}",
        context={"id": args.id},
        suggestion="todo list --all shows every item number",
    )
```

and the command declares it: `exit_codes=["NOT_FOUND"]`. A handler may only raise the
codes its manifest entry lists; anything else exits 1 with `UNDECLARED_EXIT_CODE`, so the
manifest never lies about how a command can fail.

`NOT_FOUND` is one of treaty's framework codes (0 to 13). When none of them fits, declare
your own in the 79 to 125 range with `app.exit_code(...)`; the next chapter does that.

`done` also shows the `noop` effect: completing an item that is already completed changes
nothing, and says so.

**Check:** `todo done 9 --db tmp/todo.json` exits 5 with `error.code` `NOT_FOUND` and the
suggestion in `error.suggestion`

## Step 6: Replace the prompt with a danger level

`purge` asked "Delete N completed items? [y/N]" unless `--yes` was given. treaty never
prompts. Instead, declare the command `destructive` and give it a `dry_run` flag:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
@dataclass(frozen=True, slots=True)
class Purge(Common):
    dry_run: bool = Flag(default=False, description="Show what would be deleted, delete nothing")
```

<!-- file: examples/tutorial/todo_treaty.py -->
```python
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
```

A dry run also returns `would_affect`, a `treaty.Affects` with a one-line summary, the
resources it would touch, and their count; the result type declares it as
`would_affect: Affects | None = None`, and treaty refuses a destructive dry run without it.

Without `--confirm-destructive`, treaty runs the handler as a dry run and exits 2 with
`CONFIRMATION_REQUIRED`. The dry-run result is in `data`, so the refusal also tells the
caller what would have been deleted. That preview is what the prompt used to show a person;
now an agent gets it too, as data. `--confirm-destructive` replaces `--yes`.

**Check:**

```bash
$ todo purge --db tmp/todo.json
# exit 2, error.code CONFIRMATION_REQUIRED, data.effect "would_delete", data.deleted lists the items
$ todo purge --db tmp/todo.json --confirm-destructive
# exit 0, data.effect "deleted"
```

## Step 7: Keep the output people are used to

Piped output is JSON. At a terminal, treaty prints `plain`: one `key: value` line per field.
When the old output was worth keeping, register a renderer for the command. It receives
`data` as JSON values and returns text:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
def render_items(data: Sequence[Mapping[str, object]]) -> str:
    lines = []
    for item in data:
        mark = "x" if item["done"] else " "
        lines.append(f"[{mark}] #{item['id']} {item['text']} ({item['priority']})\n")
    return "".join(lines)
```

and pass it as `renderers={Format.PLAIN: render_items}` on `list`. The JSON contract does
not change; the renderer only decides what a person sees.

`list` also passes `sort_key="id"`. treaty sorts every array in `data`, so two identical
calls return identical bytes; `sort_key` says which field orders an array of objects, and
`ordered=True` keeps the handler's order instead.

**Check:**

```bash
$ todo list -a --db tmp/todo.json --format plain
[x] #1 Buy milk (high)
[ ] #2 Walk dog (normal)
```

## Step 8: Swap the entry point

`main()` with `parse_args()` and `args.func(args)` becomes:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
if __name__ == "__main__":
    app.main()
```

If the CLI is installed as a console script, point the script at the same call:

```toml
[project.scripts]
todo = "todo.cli:app.main"
```

**Check:** `todo manifest` prints every command, and `todo --version` prints the version

## Step 9: Test through the envelope

Tests call commands in-process with `app.call()`, the same path `exec` and MCP use, and get
the envelope back. No subprocess, no parsing of printed text:

```python
env = app.call("done", {"id": 9, "db": str(tmp_path / "todo.json")})
assert env.exit_code == 5 and env.error.code == "NOT_FOUND"
```

`app.run(argv, stdout=..., stderr=...)` covers the argv path and the plain renderers.
[`tests/test_tutorial.py`](../../../tests/test_tutorial.py) tests the finished example both
ways.

**Check:** the chapter's **Done when** command prints `["exit-codes"]`

## Migrating a large CLI one command at a time

A CLI with dozens of commands does not have to move in one change. Keep the argparse
`main()` and send the commands you have migrated to treaty:

```python
MIGRATED = {"done", "manifest"}

if __name__ == "__main__":
    if sys.argv[1:2] and sys.argv[1] in MIGRATED:
        sys.exit(app.run(sys.argv[1:]))
    todo_argparse.main()
```

Register only migrated commands on the app, so the manifest never lists a command that
still runs through argparse. The shim matches on the first word, so callers of a migrated
command have to move root options such as `--db` after the command already; that is the
order they will need once the shim is gone. Delete the shim when `MIGRATED` covers every
command.

## argparse to treaty at a glance

| argparse | treaty |
| --- | --- |
| `ArgumentParser(prog, description)` | `App(name, version=..., description=...)` |
| `add_subparsers()`, `add_parser("x")` | `@app.command("x", ...)`; nested with `app.group("x")` |
| `add_argument("name")` | `name: str = Arg(description=...)` |
| `add_argument("--flag", default=v)` | `flag: T = Flag(default=v, description=...)` |
| `required=True` | a `Flag` with no default |
| `type=int`, `type=float`, `type=Path` | the field's annotation |
| `choices=[...]` | `Literal[...]` or a `StrEnum` |
| `action="store_true"` | `bool = Flag(default=False, ...)` |
| `nargs="*"`, `action="append"` | `tuple[str, ...]` (repeated flags accumulate) |
| `"-a", "--all"` | `Flag(short="a", ...)` |
| `help=` | `description=` (required on every field) |
| `set_defaults(func=f)` | the decorated function is the handler |
| root-parser options | a `kw_only` base dataclass, read by a resource |
| mutually exclusive group | check in the handler, `raise ParseError(...)`: exit 2 |
| `parser.error(msg)` | `raise ParseError(msg, context=...)`: exit 2 |
| `sys.exit(n)` | `raise Exit.NAME(msg, ...)`, declared in `exit_codes=` |
| `print(...)` | return a dataclass; add a renderer for custom text |
| `input("Sure?")`, `--yes` | `danger_level="destructive"`, `dry_run`, `--confirm-destructive` |
| a `--password` flag | `secret=True` (inferred from the name): read from env or file only |

## What changes for the people using your CLI

Migration is a breaking change for callers. Put this list in your release notes:

- Command flags go after the command: `todo --db x list` becomes `todo list --db x`
- `--yes` is gone; destructive commands take `--confirm-destructive`, and without it they
  show what they would do and exit 2
- Output is JSON whenever stdout is not a terminal; scripts that grepped the old text should
  read JSON, or pass `--format plain`
- Exit codes change: failures that were all 1 now have their own numbers, listed in
  `todo manifest`
- Secret-looking flags (`--token`, `--password`) no longer take a value on the command line;
  use `--token-from-env VAR` or `--token-from-file PATH`

## Next

Run the audit. For `todo`, the one warning left is `exit-codes`: `add` and `purge` change state
but declare no failures of their own, so an agent cannot tell their failures apart. The
[Declare exit codes](../core/exit-codes.md) chapter clears it.
