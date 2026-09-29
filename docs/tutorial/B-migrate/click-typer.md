# Migrate a click or typer CLI

**Goal:** every command of a click or typer CLI runs on treaty, with the same features and a
manifest an agent can read

**You need:** a working click or typer CLI, and treaty installed ([Before you
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

The chapter migrates one small CLI, `todo`, written both ways:
[`examples/tutorial/todo_click.py`](../../../examples/tutorial/todo_click.py) and
[`examples/tutorial/todo_typer.py`](../../../examples/tutorial/todo_typer.py). Both end at
the same treaty app,
[`examples/tutorial/todo_treaty.py`](../../../examples/tutorial/todo_treaty.py), which is
also where the [argparse chapter](argparse.md) ends. Each step shows the click code first
and the typer code where it differs; typer runs on click, so the two behave the same.

The two starting points carry inline script metadata, so `uv run` fetches click or typer
for them without adding either to your project.

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

## What is wrong with the click version

click already does a few things right. A bad value exits 2 before the command runs, and
`--help` is generated from the declarations. Run it the way an agent does, with stdout
piped, and the rest breaks:

```bash
$ uv run examples/tutorial/todo_click.py --db tmp/tutorial/old.json done 9
Error: no item #9                          # prose on stderr, exit 1
$ uv run examples/tutorial/todo_click.py --db tmp/tutorial/old.json purge </dev/null
Delete 0 completed items? [y/N]: Aborted!  # a prompt, then exit 1
$ uv run examples/tutorial/todo_click.py --db tmp/tutorial/old.json add x --priority urgent
Usage: todo_click.py add [OPTIONS] TEXT    # exit 2, but the reason is prose
Try 'todo_click.py add --help' for help.

Error: Invalid value for '--priority': 'urgent' is not one of 'low', 'normal', 'high'.
```

- **Output is prose.** `Added #3: Buy milk` has to be parsed with a regex that breaks when
  the wording changes
- **Every failure is exit 1.** `ClickException` and `typer.Exit(code=1)` look the same for a
  missing item, a full disk, and a bug, so an agent cannot tell whether to retry, fix its
  arguments, or stop
- **It prompts.** With stdin closed, `click.confirm` aborts; with stdin open and empty, as
  many agent harnesses leave it, `purge` waits for an answer that never comes
- **It cannot describe itself.** `--help` is text for people, one command at a time; typer
  draws its errors in a box

treaty fixes the first three by construction and the fourth with `manifest`.

## Step 1: Take inventory

click's decorators already list the arguments. What they do not say is what each command
does to state, and how it fails. Write that down; the table becomes the manifest:

| Command | Arguments | Writes? | Fails when | Danger level |
| --- | --- | --- | --- | --- |
| `add` | `text`, `--priority` (low, normal, high) | appends an item | never on purpose | `mutating` |
| `list` | `--all` / `-a` | nothing | never on purpose | `safe` |
| `done` | `id` (int) | updates an item | the item does not exist | `mutating` |
| `purge` | `--yes` / `-y` | deletes items | never on purpose | `destructive` |
| all | `--db PATH`, on the group | | | |

Danger level is the column click never asked for. `safe` commands only read, `mutating`
commands change state, and `destructive` commands remove something that cannot be restored.
It decides which framework flags a command gets and whether it can run without
confirmation.

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

**Check:** every command has a row, and every `ClickException`, `typer.Exit`, `ctx.exit`,
`click.confirm`, and `click.prompt` in the old code shows up in the "Fails when" or "Writes?"
column

## Step 2: Create the app

The root group becomes one `App`. click:

<!-- file: examples/tutorial/todo_click.py -->
```python
@click.group(help="Track todo items")
@click.version_option("1.0.0", prog_name="todo")
```

typer:

<!-- file: examples/tutorial/todo_typer.py -->
```python
app = typer.Typer(help="Track todo items")
```

treaty:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
app = App("todo", version="1.0.0", description="Track todo items")
```

The version is required, and `todo --version` and `todo version` report it, so
`click.version_option` goes away. A nested group, `@cli.group()` or `app.add_typer(sub,
name="remote")`, becomes `remote = app.group("remote", description="...")`, and its
commands register with `@remote.command("add", ...)`.

## Step 3: Move group options onto a base class

click puts `--db` on the group and hands it to commands through `ctx.obj`:

<!-- file: examples/tutorial/todo_click.py -->
```python
@click.option(
    "--db",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path.home() / ".todo.json",
    help="Where the items are stored",
)
@click.pass_context
def cli(ctx: click.Context, db: Path) -> None:
    ctx.obj = db
```

typer does the same in a callback:

<!-- file: examples/tutorial/todo_typer.py -->
```python
@app.callback()
def main(
    ctx: typer.Context,
    db: Annotated[Path, typer.Option(help="Where the items are stored")] = DEFAULT_DB,
) -> None:
    ctx.obj = db
```

treaty has no group-level options: every flag belongs to a command and goes after the
command path. Declare the shared flag once on a `kw_only` base dataclass that every
command's arguments extend:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
@dataclass(frozen=True, slots=True, kw_only=True)
class Common:
    db: Path | None = Flag(default=None, description="Item file; default ~/.todo.json")
```

The click default was `Path.home() / ".todo.json"`, computed at import. A default in the
manifest should be a fixed value, so the flag defaults to `None` and the fallback moves to
the code that opens the store. What `ctx.obj` and `@click.pass_obj` did becomes a resource:
a class with an `acquire` classmethod, which treaty calls once per run with the parsed
arguments and hands to every handler that annotates a parameter with it:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
@dataclass(frozen=True, slots=True)
class Store:
    path: Path

    @classmethod
    def acquire(cls, args: Common, ctx: Ctx) -> Self:
        return cls(args.db if args.db is not None else Path.home() / ".todo.json")
```

Unlike `ctx.obj`, the resource is typed, so a handler that asks for a `Store` gets one.

Typing `db` as `Path` also gets the argument checked before any handler runs: `..` segments,
percent-encoded bytes, and null bytes exit 2. `click.Path(exists=True)` has no equivalent
flag. Check existence in the handler, after `--cwd` has resolved the path, and raise a
declared code such as `NOT_FOUND`. Not in `__post_init__`: it runs once on the typed path
and again on the resolved one.

**Check:** the old order fails with the new order in the suggestion, so a caller that still
uses it is told how to fix the call

<!-- check -->
```bash
todo --db tmp/tutorial/todo.json list | jq -e '.meta.exit_code == 2
  and .error.suggestion == "flags go after the command: todo list [arguments] --db"'
```

## Step 4: Migrate one command end to end

A decorated click function becomes three things: an arguments dataclass, a decorated
handler, and a return type. click:

<!-- file: examples/tutorial/todo_click.py -->
```python
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
```

typer:

<!-- file: examples/tutorial/todo_typer.py -->
```python
@app.command(help="Add an item")
def add(
    ctx: typer.Context,
    text: Annotated[str, typer.Argument(help="What to do")],
    priority: Annotated[Priority, typer.Option(help="How urgent it is")] = Priority.normal,
) -> None:
```

treaty moves the parameters into a dataclass, one field each:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
@dataclass(frozen=True, slots=True)
class Add(Common):
    text: str = Arg(description="What to do")
    priority: Priority = Flag(default="normal", description="How urgent it is")
```

If you come from typer, this is the same idea with the metadata moved: the annotation is
the type, and `Arg` or `Flag` replaces `typer.Argument` or `typer.Option`. `Priority` is
`Literal["low", "normal", "high"]`, so `click.Choice` moves into the type and the manifest
lists the allowed values. typer's `StrEnum` works as it is.

The handler returns data instead of echoing it. Besides `args`, the handler takes `ctx`, the
run's context (logging, the time limit, and more, which later chapters use), and `store`,
the resource from Step 3, whose `load()` and `save()` read and write the item file.
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

The command name is given explicitly, `"add"`, rather than taken from the function, so
renaming the function never renames the command. `help=` becomes `description=`, and it is
required on every command and every field: the manifest has no undocumented flags.

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

## Step 5: Replace `ClickException` and `typer.Exit` with a named exit code

click:

<!-- file: examples/tutorial/todo_click.py -->
```python
    raise click.ClickException(f"no item #{id}")
```

typer:

<!-- file: examples/tutorial/todo_typer.py -->
```python
    typer.echo(f"error: no item #{id}", err=True)
    raise typer.Exit(code=1)
```

treaty:

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

`click.UsageError` and `click.BadParameter` become `ParseError`. Raised from the arguments
dataclass's `__post_init__`, it exits 2 before anything runs, as click's did. Raised from a
handler, after work may have started, it exits 1 with `VALIDATION_AFTER_START`, so checks
that only look at the arguments belong in `__post_init__`.

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

## Step 6: Replace the confirmation with a danger level

`purge` asked before deleting unless `--yes` was given:

<!-- file: examples/tutorial/todo_click.py -->
```python
    if not yes:
        click.confirm(f"Delete {len(completed)} completed items?", abort=True)
```

typer's `typer.confirm` is the same call, and `@click.confirmation_option` is the same
thing as a decorator. treaty never prompts. Instead, declare the command `destructive` and
give it a `dry_run` flag:

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

A question that is not about destroying something, such as `click.prompt` for a missing
name, becomes a required flag. When a person at a terminal should still be asked, declare
`interactive=True` and ask through `ctx.prompt` or `ctx.confirm`: off a terminal they exit 4
instead of waiting. `ctx.prompt(text, flag=...)` names the flag that answers it, and `--yes`
answers a `ctx.confirm`, the one place a `--yes` remains.

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

`list` formatted each line with `click.echo`:

<!-- file: examples/tutorial/todo_click.py -->
```python
            mark = "x" if item["done"] else " "
            click.echo(f"[{mark}] #{item['id']} {item['text']} ({item['priority']})")
```

Piped output is JSON. At a terminal, treaty prints `plain`: one `key: value` line per field.
When the old output was worth keeping, move the formatting into a renderer for the command.
It receives `data` as JSON values and returns text:

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
not change; the renderer only decides what a person sees. Other terminal output moves too:
`click.echo(..., err=True)` becomes `ctx.log(...)`, and `click.progressbar` becomes
`ctx.progress(...)`.

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

The module ran the group:

<!-- file: examples/tutorial/todo_click.py -->
```python
if __name__ == "__main__":
    cli()
```

and now runs the app:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
if __name__ == "__main__":
    app.main()
```

The console script changes the same way, from `todo = "todo.cli:cli"` (or `todo.cli:app`
for typer) to:

```toml
[project.scripts]
todo = "todo.cli:app.main"
```

If a library the CLI imports prints on import, that text reaches stdout before `app.main()`
can guard it. Point the script at a small entry module that calls
`treaty.intercept_stdout()` and only then imports the app, as the `entry.py` of
`treaty init` does; the text then goes to stderr and into a `THIRD_PARTY_STDOUT` warning.

Shell completion moves too. click's `_TODO_COMPLETE=bash_source todo` and typer's
`--install-completion` are replaced by the `completion` built-in, which generates the script
from the manifest: `source <(todo completion bash --format plain)`.

**Check:** the manifest lists the four commands, and `--version` reports the app's version

<!-- check -->
```bash
todo manifest | jq -e '.data.commands | has("add") and has("list") and has("done") and has("purge")'
todo --version | jq -e '.data == {"name": "todo", "version": "1.0.0"}'
```

## Step 9: Test through the envelope

click tests usually run the command through `CliRunner` and match the printed text:

```python
result = CliRunner().invoke(cli, ["--db", str(tmp_path / "todo.json"), "done", "9"])
assert result.exit_code == 1 and "no item #9" in result.output
```

treaty tests call the command in-process with `app.call()`, the same path `exec` and MCP
use, and get the envelope back. No runner, no parsing of printed text:

```python
env = app.call("done", {"id": 9, "db": str(tmp_path / "todo.json")}, env={"TODO_AUDIT_LOG": "off"})
assert env.exit_code == 5 and env.error.code == "NOT_FOUND"
```

A command in a group is called by its dotted path, as the manifest keys it:
in a CLI with a `remote` group, `app.call("remote.add", {...})` runs `mycli remote add`.

`app.run(argv, stdout=..., stderr=...)` covers the argv path and the plain renderers, where
`CliRunner` did. A whole test file for `todo` in this style, with its imports and a fixture
for a scratch item file, is
[`new_cli/test_cli.py`](../../../examples/tutorial/new_cli/test_cli.py): copy it into your
project's `tests/` and change `from todo.cli import app` to your app. Your existing tests
are callers too: those that pass flags before the command, read printed text, or expect exit
1 fail after the migration, as [What changes for the people using your
CLI](#what-changes-for-the-people-using-your-cli) lists. Update them or replace them, and
copy the example under another name if `tests/test_cli.py` exists. Treaty's own
[`tests/test_tutorial.py`](../../../tests/test_tutorial.py) tests the finished example both
ways.

**Check:** the chapter's **Done when** command exits 0

## Migrating a large CLI one command at a time

A CLI with dozens of commands does not have to move in one change. Keep the click group and
send the commands you have migrated to treaty:

```python
MIGRATED = {"done", "manifest"}

if __name__ == "__main__":
    if sys.argv[1:2] and sys.argv[1] in MIGRATED:
        sys.exit(app.run(sys.argv[1:]))
    cli()
```

Register only migrated commands on the app, so the manifest never lists a command that
still runs through click. The shim matches on the first word, so callers of a migrated
command have to move group options such as `--db` after the command already; that is the
order they will need once the shim is gone. Delete the shim when `MIGRATED` covers every
command.

## click and typer to treaty at a glance

| click | typer | treaty |
| --- | --- | --- |
| `@click.group()` | `typer.Typer()` | `App(name, version=..., description=...)` |
| `@cli.command()` | `@app.command()` | `@app.command("x", danger_level=..., exit_codes=...)` |
| `@cli.group()` | `app.add_typer(sub, name="x")` | `app.group("x", description=...)` |
| `@click.argument("name")` | `Annotated[str, typer.Argument()]` | `name: str = Arg(description=...)` |
| `@click.option("--flag", default=v)` | `Annotated[T, typer.Option()] = v` | `flag: T = Flag(default=v, description=...)` |
| `required=True` | an option with no default | a `Flag` with no default |
| `type=int`, `type=float` | the annotation | the field's annotation |
| `click.Choice([...])` | an `Enum` | `Literal[...]` or a `StrEnum` |
| `click.Path(path_type=Path)` | `Path` | `Path`, checked for `..` and encoded bytes |
| `click.Path(exists=True)` | `exists=True` | a check in the handler that raises `Exit.NOT_FOUND` |
| `click.IntRange` | `min=`, `max=` | a check in `__post_init__` that raises `ParseError` |
| `click.File("r")`, a `-` for stdin | `typer.FileText` | `stdin_input=True`: the text arrives as `ctx.stdin_text`, from a pipe or `--input-file PATH`, which replaces the file argument |
| an `--output FILE` the command writes itself | the same | keep an `output: Path` flag, and raise `treaty.already_exists` (`CONFLICT`) yourself when the file exists and `--force` is not given; `output_file=True` instead writes `data` in the `--format` representation |
| `is_flag=True` | `bool = False` | `bool = Flag(default=False, ...)` |
| `--x/--no-x` | `bool = True` | `bool = Flag(default=True, ...)`: treaty adds `--no-x` |
| `multiple=True` | `list[str]` option | `tuple[str, ...]` flag: repeats accumulate |
| `nargs=-1` | `list[str]` argument | `tuple[str, ...] = Arg(...)` |
| `"-a", "--all"` | `typer.Option("--all", "-a")` | `Flag(short="a", ...)` |
| `help=` | `help=` | `description=`, required on every command and field |
| `envvar="TODO_X"` | `envvar="TODO_X"` | a field of `App(settings=...)`, read from `TODO_X` and config files |
| `hide_input=True`, `password_option` | `hide_input=True` | `secret=True`: `--x-from-env`, `--x-from-file`, or `TODO_X` |
| `@click.pass_obj`, `ctx.obj` | `@app.callback()`, `ctx.obj` | a `kw_only` base dataclass, read by a resource |
| `@click.version_option` | a `--version` callback | built in, from `App(version=...)` |
| `count=True` (`-vvv`) | `count=True` | the framework's `--verbose` and `--debug` |
| `click.echo(...)` | `typer.echo(...)`, `rich.print` | return a dataclass; add a renderer for custom text |
| `click.echo(..., err=True)` | `typer.echo(..., err=True)` | `ctx.log(...)` |
| `click.progressbar` | `rich.progress` | `ctx.progress(...)` |
| `ClickException`, `ctx.exit(n)` | `typer.Exit(code=n)` | `raise Exit.NAME(msg, ...)`, declared in `exit_codes=` |
| `UsageError`, `BadParameter` | `typer.BadParameter` | `raise ParseError(msg)` in `__post_init__`: exit 2 |
| `click.confirm`, `confirmation_option` | `typer.confirm` | `danger_level="destructive"`, `dry_run`, `--confirm-destructive` |
| `click.prompt`, `prompt=True` | `typer.prompt` | a required flag, or `interactive=True` and `ctx.prompt` |
| `click.edit()` | `click.edit()` | `ctx.edit()` with `editor_alternatives=` |
| shell completion | `--install-completion` | `todo completion bash` |
| `CliRunner().invoke(...)` | `typer.testing.CliRunner` | `app.call(...)`, or `app.run(argv, ...)` |

## What changes for the people using your CLI

Migration is a breaking change for callers. Put this list in your release notes:

- Group options go after the command: `todo --db x list` becomes `todo list --db x`
- `--yes` is gone from destructive commands, which take `--confirm-destructive`, and without it they
  show what they would do and exit 2
- A command that returns a list, such as `list`, returns 20 items at a time; `--limit 0`
  returns all of them, and `--cursor` the next
  page ([Page long lists](../core/pagination.md))
- A text flag refuses a line break unless the field declares `multiline=True`; give
  every field that takes free text, such as a body or a message, `multiline=True`
- A command that read a file or `-` for stdin takes the file as `--input-file PATH` and
  otherwise reads its stdin
- A config file of the CLI's own moves to the one treaty reads
  ([Read settings and secrets](../core/config.md#a-command-that-writes-the-config-file))
- Output is JSON whenever stdout is not a terminal; scripts that grepped the old text should
  read JSON, or pass `--format plain`
- Exit codes change: failures that were all 1 now have their own numbers, listed in
  `todo manifest`
- Secret options (`--token`, `--password`) no longer take a value on the command line; use
  `--token-from-env VAR` or `--token-from-file PATH`
- `-v` is gone; pass `--verbose`
- `--config` and `--format` are now treaty's; the CLI's own options of those names are renamed (list the new names)
- Completion scripts have to be generated again with `todo completion`

## Next

Follow the audit's rules in order, one chapter each, starting with the first: [Describe
every command](../core/describe.md), then [Choose each command's danger
level](../core/danger-level.md). `todo` already passes both, since this chapter gave every
command an example and a danger level; they say how to do it well for your own CLI. The rule
`todo` still fails, `exit-codes`, comes third: [Declare exit codes](../core/exit-codes.md).
