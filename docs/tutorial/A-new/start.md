# Start a new CLI

**Goal:** a new CLI project with your own commands, built on treaty from the first line:
its tests pass, AGENTS.md matches the binary, and `treaty audit` has only the findings the
core chapters clear

**You need:** Python 3.14 and [uv](https://docs.astral.sh/uv/); nothing else, since the
project brings its own treaty

**Done when:** in the project, the tests pass and `treaty audit` reports only `exit-codes`
warnings, which the next chapter clears:

```bash
uv run pytest -q
uv run treaty audit todo.cli:app \
  | jq -e '[.data.next_steps[] | select(.severity == "warning") | .rule] | unique == ["exit-codes"]'
```

The chapter builds one small CLI, `todo`, from an empty directory: add items, list them,
complete them, and purge the completed ones. It ends with the same app the migration
chapters end at, [`examples/tutorial/todo_treaty.py`](../../../examples/tutorial/todo_treaty.py),
so every core chapter after this one applies unchanged.

## Running the checks

Run the **Check** commands from the root of a treaty checkout, in order. The first ones
create the project under `tmp/tutorial/` and change into it; the rest run inside it. Start
with an empty scratch directory, and note where the tutorial's example files are:

<!-- check -->
```bash
rm -rf tmp/tutorial && mkdir -p tmp/tutorial
examples="$PWD/examples/tutorial"
```

Each check exits non-zero when it fails: JSON output goes through `jq -e`, which exits 1 when
the condition is false. `tests/test_tutorial.py` runs the checks the same way.

## Step 1: Scaffold the project

In your own work, one command creates the project and a second installs it:

```bash
uvx treaty init todo
cd todo && uv sync
```

`--dry-run` lists the files without writing them, and `--directory` puts the project
somewhere other than `./todo`. The project depends on `treaty` from PyPI; inside a treaty
checkout, the check below passes `--treaty-source` so the project uses the checkout
instead.

**Check:** the scaffold runs, its tests pass, and the strict audit exits 0 before you change
anything

<!-- check -->
```bash
uv run treaty init todo --directory tmp/tutorial/todo --treaty-source "$PWD" \
  | jq -e '.data.effect == "created"'
cd tmp/tutorial/todo
uv sync -q
uv run todo show widget | jq -e '.ok and .data.name == "widget"'
uv run pytest -q
uv run treaty audit todo.cli:app --strict > /dev/null
```

Keep that state as the bar: after each step, the tests pass and the audit has nothing new to
say.

## Step 2: Read what you got

| File | What it is |
| --- | --- |
| `src/todo/cli.py` | the app and three placeholder commands, one per danger level |
| `src/todo/entry.py` | the console script: guards stdout, then imports the app and runs it |
| `tests/test_cli.py` | tests that run the commands in-process with `app.run()` |
| `tests/test_agents_md.py` | fails when AGENTS.md no longer matches the commands |
| `AGENTS.md` | how an agent installs and calls the CLI, generated from the commands |
| `conformance/todo.json`, `conformance/todo` | a conformance profile and the launcher the kit runs |

The three commands in `cli.py` are there to be copied, not kept. Each shows the shape of one
kind of command:

| Command | Danger level | What to copy from it |
| --- | --- | --- |
| `show` | `safe` | an arguments dataclass, a typed result, and an example |
| `create` | `mutating` | an `effect` in the result, a `--dry-run`, and `already_exists` for a repeated create |
| `delete` | `destructive` | `would_affect` on a dry run, a `noop` for an item that is already gone, and a registered exit code (`ITEM_IN_USE`, 79) |

`entry.py` exists for one reason: a library that prints when it is imported would put text
on stdout ahead of the JSON envelope. `entry.py` calls `treaty.intercept_stdout()` before it
imports the app, so that text goes to stderr and into a `THIRD_PARTY_STDOUT` warning. Import
your own modules from `cli.py`, never from `entry.py`, and the guard covers them too.

## Step 3: Plan the commands

Before writing code, list what the CLI does. Every column ends up in the manifest:

| Command | Arguments | Writes? | Fails when | Danger level |
| --- | --- | --- | --- | --- |
| `add` | `text`, `--priority` (low, normal, high) | appends an item | never on purpose | `mutating` |
| `list` | `--all` / `-a` | nothing | never on purpose | `safe` |
| `done` | `id` (int) | updates an item | the item does not exist | `mutating` |
| `purge` | `--dry-run` | deletes items | never on purpose | `destructive` |
| all | `--db PATH` | | | |

The danger level is the column to get right. `safe` commands only read, `mutating` commands
change state, and `destructive` commands remove something that cannot be restored. It
decides which framework flags a command gets and whether it runs without confirmation.

Name commands with verbs an agent can guess, and keep flags after what they hold. The
[design guide](../../guide.md) covers naming, and what belongs in an error.

**Check:** every command has a danger level, and every failure a caller could act on is in
the "Fails when" column

## Step 4: Replace the app and add shared state

Replace `cli.py` from the top. The app keeps the name `todo`:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
app = App("todo", version="1.0.0", description="Track todo items")
```

`version` is what `todo --version`, the manifest, and AGENTS.md report. Keep it equal to the
version in `pyproject.toml` when you release; this chapter uses the `1.0.0` of the finished
example.

Every command reads the same item file, so the `--db` flag is declared once, on a
`kw_only` base dataclass that each command's arguments extend:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
@dataclass(frozen=True, slots=True, kw_only=True)
class Common:
    db: Path | None = Flag(default=None, description="Item file; default ~/.todo.json")
```

The default is `None` rather than `Path.home() / ".todo.json"`: a default in the manifest
should be a fixed value, not one computed on the machine that imported the module. The
fallback lives in the code that opens the store, a resource. treaty builds a resource once
per run, after the arguments are checked, and hands it to every handler that annotates a
parameter with its class:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
@dataclass(frozen=True, slots=True)
class Store:
    path: Path

    @classmethod
    def acquire(cls, args: Common, ctx: Ctx) -> Self:
        return cls(args.db if args.db is not None else Path.home() / ".todo.json")
```

`load()` and `save()` on the store read and write the file as JSON; they are plain Python,
in the [finished module](../../../examples/tutorial/todo_treaty.py). Typing `db` as `Path`
gets it checked before any handler runs: `..` segments, percent-encoded bytes, and null
bytes exit 2.

**Check:** in this repository the finished module is the tutorial's example, so the check
copies it over the scaffold's `cli.py` instead of you typing each step. The app loads, and
`--db` is on every command

<!-- check -->
```bash
cp "$examples/todo_treaty.py" src/todo/cli.py
uv run todo manifest | jq -e '[.data.commands | to_entries[]
  | select(.key == ("add", "list", "done", "purge")) | .value.flags | has("db")] == [true, true, true, true]'
```

## Step 5: Write the first command

A command is three things: an arguments dataclass, a decorated handler, and a return type.

<!-- file: examples/tutorial/todo_treaty.py -->
```python
@dataclass(frozen=True, slots=True)
class Add(Common):
    text: str = Arg(description="What to do")
    priority: Priority = Flag(default="normal", description="How urgent it is")
```

`Arg` is a positional argument and `Flag` is a `--flag`; the annotation is the type, and
`description` is required on every field. `Priority` is `Literal["low", "normal", "high"]`,
so a value outside it exits 2 and the manifest lists the three. A `StrEnum` works the same
way.

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

What each part is for:

- **`danger_level=` and `exit_codes=`** are required on every command. `add` changes state,
  so it is `mutating`. Its own failures come in the next chapter, so the list is empty for
  now
- **`examples=`** is the first thing an agent copies. The audit's `describe` rule asks for
  one on every command, and the conformance kit builds its probes from them
- **The return type** is a dataclass, so the manifest carries an `output_schema` and the
  agent knows the shape of `data` before it calls
- **`effect`** is required on mutating and destructive results: `created`, `updated`,
  `deleted`, or `noop`. An agent that retries reads `noop` and knows nothing changed

The handler never prints. It returns data, and treaty writes the envelope: JSON when stdout
is not a terminal, `key: value` lines when it is. Mutating commands also get
`--idempotency-key` without any code: a second call with the same key returns the first
result instead of adding a second item.

**Check:** the first item is created, and a value outside the `Literal` exits 2 before the
handler runs

<!-- check -->
```bash
uv run todo add "Buy milk" --priority high --db todo.json | jq -e '.data == {
  "effect": "created", "item": {"id": 1, "text": "Buy milk", "priority": "high", "done": false}}'
uv run todo add "x" --priority urgent --db todo.json \
  | jq -e '.meta.exit_code == 2 and (.error.message | endswith("must be one of low, normal, high."))'
```

## Step 6: Add the other commands

`list` is `safe`: it only reads, so it gets no confirmation, no idempotency key, and no
`effect`:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
@app.command(
    "list",
    description="List open items",
    danger_level="safe",
    sort_key="id",
    renderers={Format.PLAIN: render_items},
    exit_codes=[],
    examples=[("List every item, completed ones too", "todo list --all")],
)
def list_items(args: ListArgs, ctx: Ctx, store: Store) -> list[Item]:
    return [i for i in store.load() if args.all or not i.done]
```

treaty sorts every array in `data`, so two identical calls return identical bytes;
`sort_key` says which field orders an array of objects. `renderers=` replaces the default
`key: value` lines at a terminal with the format a person expects, one line per item. The
renderer gets `data` as JSON values and returns text; the JSON an agent reads does not
change.

`done` fails when the item does not exist. The failure is a named exit code, not a print
and `sys.exit(1)`:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
    raise Exit.NOT_FOUND(
        f"no item #{args.id}",
        context={"id": args.id},
        suggestion="todo list --all shows every item number",
    )
```

and the command declares it with `exit_codes=["NOT_FOUND"]`. `NOT_FOUND` is one of treaty's
framework codes (0 to 13); raising a code the command does not declare exits 1 with
`UNDECLARED_EXIT_CODE`, so the manifest never lies about how a command can fail.

`purge` is `destructive`, and treaty never prompts. It declares a `dry_run` flag and returns
what it would delete:

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

Without `--confirm-destructive`, treaty runs the handler as a dry run and exits 2 with
`CONFIRMATION_REQUIRED`, and the dry-run result is in `data`: the caller sees what would
have been deleted, where a prompt would have asked a person.

**Check:** complete item 1, see `purge` refuse with a preview, confirm it, and read the
plain output of what is left

<!-- check -->
```bash
uv run todo done 9 --db todo.json | jq -e '.meta.exit_code == 5 and .error.code == "NOT_FOUND"'
uv run todo add "Walk dog" --db todo.json | jq -e '.data.item.id == 2'
uv run todo done 1 --db todo.json | jq -e '.data.effect == "updated"'
uv run todo purge --db todo.json | jq -e '.meta.exit_code == 2
  and .error.code == "CONFIRMATION_REQUIRED" and [.data.deleted[].id] == [1]'
uv run todo purge --db todo.json --confirm-destructive | jq -e '.data.effect == "deleted"'
diff <(uv run todo list -a --db todo.json --format plain) - <<'EOF'
[ ] #2 Walk dog (normal)
EOF
```

## Step 7: Replace the tests

The scaffold's tests exercise `show`, `create`, and `delete`, which are gone. Test the new
commands through the envelope: `app.call()` runs a command in-process, the same path `exec`
and MCP take, and returns the envelope, so a test reads fields instead of parsing text.
From [`examples/tutorial/new_cli/test_cli.py`](../../../examples/tutorial/new_cli/test_cli.py):

<!-- file: examples/tutorial/new_cli/test_cli.py -->
```python
def test_purge_previews_until_confirmed(db: str) -> None:
    app.call("add", {"text": "Walk dog", "db": db})
    app.call("done", {"id": 1, "db": db})
    preview = app.call("purge", {"db": db})
    assert preview.exit_code == 2 and preview.error is not None
    assert preview.error.code == "CONFIRMATION_REQUIRED"
    applied = app.call("purge", {"db": db, "confirm_destructive": True})
    assert applied.exit_code == 0
    assert app.call("list", {"all": True, "db": db}).data == []
```

Arguments are keyed by field name, and the framework flags by theirs: `--confirm-destructive`
is `"confirm_destructive": True`. `app.run(argv, stdout=..., stderr=...)` takes the argv
path instead, which is how a test reaches a renderer.

**Check:** the new tests pass

<!-- check -->
```bash
cp "$examples/new_cli/test_cli.py" tests/test_cli.py
uv run pytest -q tests/test_cli.py
```

## Step 8: Regenerate AGENTS.md

Run the whole suite and one scaffold test still fails:

```bash
$ uv run pytest -q
FAILED tests/test_agents_md.py::test_agents_md_matches_the_cli - AssertionError: ...
  - AGENTS.md:20 command create: not a command of todo
  - AGENTS.md:1 version 0.1.0: declares 0.1.0, todo --version is 1.0.0
```

AGENTS.md is what an agent reads first, and it still describes the scaffold. The test runs
`treaty check-docs`, which compares the file with the app: the declared version, the
generated sections, and every command, flag, and variable the file names. Regenerate it:

```bash
uv run treaty agents-md todo.cli:app
```

`agents-md` rewrites the text between the `<!-- treaty:begin -->` and `<!-- treaty:end -->`
markers and keeps the rest, so notes you add outside them survive. Run it whenever a
command, flag, or the version changes; the test fails until you do.

**Check:** AGENTS.md is rewritten, and every test passes

<!-- check -->
```bash
uv run treaty agents-md todo.cli:app | jq -e '.data.effect == "updated"'
uv run pytest -q
```

## Step 9: Rewrite the conformance profile

The profile in `conformance/todo.json` lists probes for `show` and `delete`. `treaty
conformance` writes a new one from the current commands, but it will not replace a profile
that differs from what it generates, since that may hold probes written by hand:

```bash
$ uv run treaty conformance todo.cli:app
# exit 6, error.code CONFLICT; error.context names the probes that changed:
# probes_only_in_file ["delete", "show"], probes_only_generated ["list", "purge"]
```

The scaffold's profile holds nothing you wrote, so replace it with `--force`.

**Check:** the old profile is refused, then replaced

<!-- check -->
```bash
uv run treaty conformance todo.cli:app | jq -e '.error.code == "CONFLICT"
  and .error.context.probes_only_generated == ["list", "purge"]'
uv run treaty conformance todo.cli:app --force | jq -e '.data.effect == "updated"'
```

Do not add `--run` yet. The kit runs the real CLI through `conformance/todo`, and one probe
is `todo purge`: against your real `~/.todo.json`, a bug in the dry run would delete your
completed items. [Run the conformance kit](../ship/conformance.md#step-2-keep-the-probes-away-from-real-data)
shows how to point the launcher at a sandbox first.

## Step 10: Read the audit

The scaffold passed the strict audit; `todo` does not yet:

```bash
$ uv run treaty audit todo.cli:app --format plain
...
Next steps
  1. (warning) exit-codes [add]: non-safe command declares no command-specific exit codes; agents cannot tell failures apart
  2. (warning) exit-codes [purge]: non-safe command declares no command-specific exit codes; agents cannot tell failures apart
  ...
```

`add` and `purge` change state but declare no failures of their own: a damaged item file
or a read-only directory ends as a crash with exit 1, which tells an agent nothing. That
is the next chapter's subject, and every chapter after it follows the audit the same way.

**Check:** the chapter's **Done when**

<!-- check -->
```bash
uv run pytest -q
uv run treaty audit todo.cli:app \
  | jq -e '[.data.next_steps[] | select(.severity == "warning") | .rule] | unique == ["exit-codes"]'
```

## Next

[Declare exit codes](../core/exit-codes.md) gives each failure an exit code an agent can act
on and clears the audit's `exit-codes` warnings. It continues this same `todo`, from the
same code.

The audit's first two rules, `describe` and `danger-level`, already pass for `todo`: this
chapter gave every command an example and a danger level. For your own CLI,
[Describe every command](../core/describe.md) and
[Choose each command's danger level](../core/danger-level.md) cover both in full.
