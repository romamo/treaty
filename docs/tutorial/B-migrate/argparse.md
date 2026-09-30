# Migrate an argparse CLI

**Goal:** every command of an argparse CLI runs on treaty, with the same features and a
manifest an agent can read

**You need:** a working argparse CLI, and treaty installed ([Before you
start](../index.md#before-you-start))

**Done when:** your tests pass, if the CLI has any, and, for `todo`, `treaty audit` reports
only `exit-codes` warnings, which [Declare exit codes](../core/exit-codes.md) clears.
Another CLI may get other rules too; [the
index](../index.md#after-the-first-chapter-follow-the-audit) maps each to its chapter. The
check, for `todo`:

<!-- check -->
```bash
uv run treaty audit examples.tutorial.todo_treaty:app \
  | jq -e '[.data.rules[].findings[] | select(.severity == "warning") | .rule] | unique == ["exit-codes"]'
```

In your project the audit reads `uv run treaty audit todo.cli:app`, with your app's import
path.

The chapter migrates one small CLI, `todo`, from start to finish. The starting point is
[`examples/tutorial/todo_argparse.py`](../../../examples/tutorial/todo_argparse.py), the
result is [`examples/tutorial/todo_treaty.py`](../../../examples/tutorial/todo_treaty.py).
Keep both open; the steps below show the parts that change.

## Running the checks

Run the **Check** commands from the root of a treaty checkout, in order: each one uses the
item file the one before it left behind. Start with a `todo` command that runs the finished
example, and an empty scratch directory:

<!-- check -->
```bash
todo() { uv run examples/tutorial/todo_treaty.py "$@"; }
rm -rf tmp/tutorial && mkdir -p tmp/tutorial
```

In your own project, `todo` is your CLI's command. Piped output is a JSON
[envelope](../envelope.md), and each check pipes it into `jq -e`, which exits 1 when the
condition is false: a check passes when every line in it exits 0.

## What is wrong with the argparse version

Nothing, for a person at a terminal. Run it the way an agent does, with stdout piped and no
terminal on stdin, and it fails in the four common ways:

```bash
$ uv run examples/tutorial/todo_argparse.py --db tmp/tutorial/old.json done 9
error: no item #9                     # prose on stderr, exit 1
$ uv run examples/tutorial/todo_argparse.py --db tmp/tutorial/old.json purge </dev/null
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

Danger level is the column argparse never asked for. `safe` commands only read, `mutating`
commands change state, and `destructive` commands remove something that cannot be restored.
It decides which framework flags a command gets and whether it can run without confirmation.

Look for names treaty keeps for itself. Registration refuses a clash, so rename the option
now; the audit never sees an app that does not build:

- **On every command**: `--verbose`, `--quiet`, `--debug`, `--config`, `--format`,
  `--fields`, `--cwd`, `-h`, and every global flag a command's `--help` lists, such as
  `--schema` (a `--schema FILE` option becomes `--schema-file`). A `-v` counter becomes the
  framework's `--verbose`, which has no `-v` short
- **With `has_network_io=True`**: `--timeout`, `--proxy`, and `--no-proxy`. Drop your own:
  the handler reads the limit as `ctx.timeout.seconds`, and `timeout=5` on
  `@app.command` keeps an old default of 5 seconds, where treaty's is 60
- **On a command that returns a list**: `--limit` and `--cursor`, as [Page long
  lists](../core/pagination.md) shows, or `paginated=False` to keep your own
- **Commands**: `manifest`, `version`, and `exec` are refused as names; a command named
  after another built-in (`status`, `doctor`, `cleanup`, `completion`, `audit-log`,
  `changelog`, `generate-skills`, `mcp-validate`) replaces it, with `builtin-shadowed`
  advice
- **Secrets**: a field whose name contains `token`, `secret`, `password`, `key`,
  `credential`, `auth`, or `cookie` (so `author` and `keyword` count), or has a `pass`
  segment, takes no value on the command line; booleans and enums are never secrets, and
  `secret=False` opts out

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
handler; it becomes a resource: a class with an `acquire` classmethod, which treaty calls
once per run with the parsed arguments, and hands the object to every handler that
annotates a parameter with the class, as `add` does with `store: Store` below:

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

<!-- check -->
```bash
todo --db tmp/tutorial/todo.json list | jq -e '.meta.exit_code == 2
  and .error.suggestion == "flags go after the command: todo list [arguments] --db"'
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

The handler returns data instead of printing it. Besides `args`, the handler takes `ctx`,
the run's context (logging, the time limit, and more, which later chapters use), and
`store`, the resource from Step 3, whose `load()` and `save()` read and write the item file.
`Changed` is a dataclass of the example file with two fields, the `effect` and the `item`;
`Item` is one todo item: `id`, `text`, `priority`, and `done`:

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
    next_id = max((i.id for i in items), default=0) + 1
    item = Item(id=next_id, text=args.text, priority=args.priority, done=False)
    store.save([*items, item])
    return Changed(effect="created", item=item)
```

Four things are new:

- **`danger_level=` and `exit_codes=`** are required on every command. `add` changes state,
  so it is `mutating`; its own failures come in [Declare exit codes](../core/exit-codes.md),
  so the list is empty for now. A read-only command such as `list` is `safe`
- **`examples=`** is the first thing an agent copies. The audit's `describe` rule asks for
  one on every command
- **The return type** is a dataclass, so the manifest carries an `output_schema` and the
  agent knows the shape of `data` before it calls
- **`effect`** is required on mutating and destructive results: `created`, `updated`,
  `deleted`, or `noop`, and on a dry run the `would_` form of what the real run would do,
  such as `would_delete`. An agent that retries reads `noop` and knows nothing changed

Mutating commands also get `--idempotency-key` for free. `add` is not safe to repeat (a
retry adds a second item), and the key is how a caller makes it safe: the second call with
the same key returns the first call's data with `effect: "noop"` and
`meta.idempotency_hit: true`, without running the handler.

**Check:** the first item is created, and a value outside the `Literal` exits 2 before the
handler runs

<!-- check -->
```bash
todo add "Buy milk" --priority high --db tmp/tutorial/todo.json | jq -e '.data == {
  "effect": "created", "item": {"id": 1, "text": "Buy milk", "priority": "high", "done": false}}'
todo add "x" --priority urgent --db tmp/tutorial/todo.json \
  | jq -e '.meta.exit_code == 2 and (.error.message | endswith("must be one of low, normal, high."))'
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
your own in the 79 to 125 range with `app.exit_code(...)`; [Declare exit
codes](../core/exit-codes.md) does that.

`done` also shows the `noop` effect: completing an item that is already completed changes
nothing, and says so.

**Check:** a missing item exits 5 with `NOT_FOUND` and the suggestion; completing an item
twice is `updated`, then `noop`

<!-- check -->
```bash
todo done 9 --db tmp/tutorial/todo.json | jq -e '.meta.exit_code == 5 and .error.code == "NOT_FOUND"
  and .error.suggestion == "todo list --all shows every item number"'
todo add "Walk dog" --db tmp/tutorial/todo.json | jq -e '.data.item.id == 2'
todo done 1 --db tmp/tutorial/todo.json | jq -e '.data.effect == "updated"'
todo done 1 --db tmp/tutorial/todo.json | jq -e '.data.effect == "noop"'
```

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

`Purged` is `purge`'s result dataclass:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
@dataclass(frozen=True, slots=True)
class Purged:
    effect: str
    deleted: list[Item] = Out(sort_key="id")
    would_affect: Affects | None = None
```

A dry run fills `would_affect` with a `treaty.Affects`: a one-line summary, the resources it
would touch, and their count, and treaty refuses a destructive dry run without it.
`Out(sort_key="id")` says the `deleted` items come in id order, which the audit's
`stable-order` rule asks of every list of objects; [Page long
lists](../core/pagination.md#step-1-keep-it-paginated-and-give-it-an-order) explains why.

The check reads the items back with `list -a`; `list` keeps its `-a` through `short=` on
the flag:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
@dataclass(frozen=True, slots=True)
class ListArgs(Common):
    all: bool = Flag(default=False, short="a", description="Include completed items")
```

Without `--confirm-destructive`, treaty runs the handler as a dry run and exits 2 with
`CONFIRMATION_REQUIRED`. The dry-run result is in `data`, so the refusal also tells the
caller what would have been deleted. That preview is what the prompt used to show a person;
now an agent gets it too, as data. `--confirm-destructive` replaces `--yes`.

**Check:** without confirmation, `purge` exits 2 and lists item 1, the one completed item,
without deleting it; with confirmation it deletes item 1 and keeps item 2

<!-- check -->
```bash
todo purge --db tmp/tutorial/todo.json | jq -e '.meta.exit_code == 2
  and .error.code == "CONFIRMATION_REQUIRED" and .data.effect == "would_delete"
  and [.data.deleted[].id] == [1]'
todo list -a --db tmp/tutorial/todo.json | jq -e '[.data[].id] == [1, 2]'
todo purge --db tmp/tutorial/todo.json --confirm-destructive \
  | jq -e '.meta.exit_code == 0 and .data.effect == "deleted" and [.data.deleted[].id] == [1]'
todo list -a --db tmp/tutorial/todo.json | jq -e '[.data[].id] == [2]'
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

**Check:** add a third item and complete it, then compare the plain output with the old
format. The new item is #3, not #2: `add` numbers after the highest id, so a purged number
is never handed out again

<!-- check -->
```bash
todo add "Call mom" --priority low --db tmp/tutorial/todo.json | jq -e '.data.item.id == 3'
todo done 3 --db tmp/tutorial/todo.json | jq -e '.data.effect == "updated"'
diff <(todo list -a --db tmp/tutorial/todo.json --format plain) - <<'EOF'
[ ] #2 Walk dog (normal)
[x] #3 Call mom (low)
EOF
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

If a library the CLI imports prints on import, that text reaches stdout before `app.main()`
can guard it. Point the script at a small entry module that calls
`treaty.intercept_stdout()` and only then imports the app, as the `entry.py` of
`treaty init` does; the text then goes to stderr and into a `THIRD_PARTY_STDOUT` warning.

**Check:** the manifest lists the four commands, and `--version` reports the app's version

<!-- check -->
```bash
todo manifest | jq -e '.data.commands | has("add") and has("list") and has("done") and has("purge")'
todo --version | jq -e '.data == {"name": "todo", "version": "1.0.0"}'
```

## Step 9: Test through the envelope

Tests call commands in-process with `app.call()`, the same path `exec` and MCP use, and get
the envelope back. No subprocess, no parsing of printed text:

```python
env = app.call("done", {"id": 9, "db": str(tmp_path / "todo.json")}, env={"TODO_AUDIT_LOG": "off"})
assert env.exit_code == 5 and env.error.code == "NOT_FOUND"
```

A command in a group is called by its dotted path, as the manifest keys it:
in a CLI with a `remote` group, `app.call("remote.add", {...})` runs `mycli remote add`.

`app.run(argv, stdout=..., stderr=...)` covers the argv path and the plain renderers. A
whole test file for `todo` in this style, with its imports and a fixture for a scratch item
file, is [`new_cli/test_cli.py`](../../../examples/tutorial/new_cli/test_cli.py): copy it
into your project's `tests/` and change `from todo.cli import app` to your app. Your
existing tests are callers too: those that pass flags before the command, read printed text,
or expect exit 1 fail after the migration, as [What changes for the people using your
CLI](#what-changes-for-the-people-using-your-cli) lists. Update them or replace them, and
copy the example under another name if `tests/test_cli.py` exists. Treaty's own
[`tests/test_tutorial.py`](../../../tests/test_tutorial.py) tests the finished example both
ways.

**Check:** the chapter's **Done when** command exits 0

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
| `add_subparsers()`, `add_parser("x")` | `@app.command("x", ...)`; nested with `app.group("x", description=...)` |
| `add_argument("name")` | `name: str = Arg(description=...)` |
| `add_argument("--flag", default=v)` | `flag: T = Flag(default=v, description=...)` |
| `required=True` | a `Flag` with no default |
| `type=int`, `type=float`, `type=Decimal`, `type=Path` | the field's annotation |
| `type=resource_id` (a converter returning a value object) | `app.scalar(ResourceId, parse=..., pattern=...)` before the commands, then annotate the field `ResourceId`; the pattern reaches the manifest and is checked before the handler runs |
| `choices=[...]` | `Literal[...]` or a `StrEnum` |
| `action="store_true"` | `bool = Flag(default=False, ...)` |
| `"-v", action="count"` verbosity | built in: `-v` (`--verbose`), `-vv` (`--debug`) |
| `nargs="*"`, `action="append"` | `tuple[str, ...]` (repeated flags accumulate) |
| `"-a", "--all"` | `Flag(short="a", ...)` |
| `help=` | `description=` (required on every field) |
| `set_defaults(func=f)` | the decorated function is the handler |
| root-parser options | a `kw_only` base dataclass, read by a resource |
| mutually exclusive group | `requires=[Excludes("x", prohibited=("y",))]` on the command: exit 2 |
| `parser.error(msg)` | `raise ParseError(msg, context=...)` in the arguments' `__post_init__`: exit 2; from a handler it exits 1 |
| `sys.exit(n)` | `raise Exit.NAME(msg, ...)`, declared in `exit_codes=` |
| `print(...)` | return a dataclass; add a renderer for custom text |
| `type=argparse.FileType("r")`, `-` for stdin | `stdin_input=True`: the text arrives as `ctx.stdin_text`, from a pipe or `--input-file PATH` |
| an `--output FILE` the command writes itself | keep an `output: Path` flag, and raise `treaty.already_exists` (`CONFLICT`) yourself when the file exists and `--force` is not given; `output_file=True` instead writes `data` in the `--format` representation |
| `subprocess.run([...])` | `ctx.run([...])`, with `check=False` if you read `returncode`, declared with `subprocess=` ([Run other programs](../core/programs.md)) |
| a helper that `os.chdir`s into the project so relative paths land there | `project_root=(".git",)` on the command and paths joined onto `ctx.project_root`; the `no-chdir` audit rule finds the `chdir`, also in a first-party helper the handler calls |
| long in-process work (Ansible runs, migrations) | `timeout=` on the command; without it the 60 s default applies ([Run long work](../core/long-running.md#step-1-set-the-time-limit)) |
| `-v`/`--verbose` printing progress | `ctx.log(...)`, shown under the framework's `--verbose` or its `-v`; drop the app's own `-v` flag |
| `input("Sure?")`, `--yes` | `danger_level="destructive"`, `dry_run`, `--confirm-destructive` |
| `--yes` meaning "apply; without it, print the plan and exit 0" | `safe_default=True`: the command previews and exits 0, and `--live` applies it ([Preview by default](../core/danger-level.md#preview-by-default)) |
| a hand-written MCP server declaring the same operations again | `treaty-mcp module:app` serves every command as a tool ([Serve commands over MCP](../ship/mcp.md)) |
| a `--password` flag | `secret=True` (inferred from the name): read from env or file only |

## What changes for the people using your CLI

Migration is a breaking change for callers. Put this list in your release notes:

- Command flags go after the command: `todo --db x list` becomes `todo list --db x`
- `--yes` is gone; destructive commands take `--confirm-destructive`, and without it they
  show what they would do and exit 2
- A command that returns a list, such as `list`, returns 20 items at a time; `--limit 0`
  returns all of them, and `--cursor` the next
  page ([Page long lists](../core/pagination.md))
- A text flag refuses a line break unless the field declares `multiline=True`; give
  every field that takes free text, such as a body or a message, `multiline=True`
- `-v` is gone; pass `--verbose`
- `--config` and `--format` are now treaty's; the CLI's own options of those names are renamed (list the new names)
- Completion scripts come from `todo completion`, generated from the manifest
- A command that read a file or `-` for stdin takes the file as `--input-file PATH` and
  otherwise reads its stdin
- A config file of the CLI's own moves to the one treaty reads
  ([Read settings and secrets](../core/config.md#a-command-that-writes-the-config-file))
- Output is JSON whenever stdout is not a terminal; scripts that grepped the old text should
  read JSON, or pass `--format plain`
- Exit codes change: failures that were all 1 now have their own numbers, listed in
  `todo manifest`
- Secret-looking flags (`--token`, `--password`) no longer take a value on the command line;
  use `--token-from-env VAR` or `--token-from-file PATH`

### A CLI that already emits JSON

If the argparse CLI printed its own JSON envelope, agents already parse it, and the break is
in the envelope rather than in the text:

- A top-level key the old envelope had and treaty's does not, such as `status`, is gone;
  put its value in `data`
- Exit codes are renumbered, and a number can change meaning rather than just split: an old
  `3` for "unknown" is treaty's `PARTIAL_FAILURE`. List the old and new codes side by side
- A command that exited non-zero with data, such as a failed health check returning the
  probe results, now answers `data: null` and carries the results in `error.context`
- `compat=` keeps an older shape of one command's `data`, not of the envelope, so there is
  no shim for this: bump each command's `schema_version` major and say so in the release notes

## Next

Follow the audit's rules in order, one chapter each, starting with the first: [Describe
every command](../core/describe.md), then [Choose each command's danger
level](../core/danger-level.md). `todo` already passes both, since this chapter gave every
command an example and a danger level; they say how to do it well for your own CLI. The rule
`todo` still fails, `exit-codes`, comes third: [Declare exit codes](../core/exit-codes.md).
