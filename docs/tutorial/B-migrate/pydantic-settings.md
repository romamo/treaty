# Migrate a pydantic-settings CLI

**Goal:** every command of a pydantic-settings CLI (`CliApp`, `CliSubCommand`, `cli_cmd()`)
runs on treaty, with the same features and a manifest an agent can read

**You need:** a working pydantic-settings CLI, and treaty installed ([Before you
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

The chapter migrates one small CLI, `todo`, from
[`examples/tutorial/todo_pydantic.py`](../../../examples/tutorial/todo_pydantic.py) to
[`examples/tutorial/todo_treaty.py`](../../../examples/tutorial/todo_treaty.py), where the
[argparse](argparse.md) and [click or typer](click-typer.md) chapters end too. Where a step
is the same as in the click chapter, this one shows the pydantic-settings side and links
there. Validators and argument models shared by several CLIs, which `todo` does not have,
use a second example: two small market-data CLIs,
[`market_prices.py`](../../../examples/tutorial/market_prices.py) and
[`market_funds.py`](../../../examples/tutorial/market_funds.py), that share
[`market_args.py`](../../../examples/tutorial/market_args.py).

The starting point carries inline script metadata, so `uv run` fetches pydantic-settings for
it without adding it to your project. treaty has no pydantic dependency: arguments are
frozen dataclasses, so the models are rewritten, and this chapter shows how.

## Running the checks

Run the **Check** commands from the root of a treaty checkout, in order: each one uses the
item file the one before it left behind. Start with a `todo` command that runs the finished
example, a command for each market CLI, and an empty scratch directory:

<!-- check -->
```bash
todo() { uv run examples/tutorial/todo_treaty.py "$@"; }
prices() { uv run python -m examples.tutorial.market_prices "$@"; }
funds() { uv run python -m examples.tutorial.market_funds "$@"; }
rm -rf tmp/tutorial && mkdir -p tmp/tutorial
```

In your own project, `todo` is your CLI's command. Piped output is a JSON
[envelope](../envelope.md), and each check pipes it into `jq -e`, which exits 1 when the
condition is false: a check passes when every line in it exits 0.

## What is wrong with the pydantic-settings version

pydantic-settings gets the arguments right: they are typed models, a wrong flag exits 2, and
`--help` comes from the field descriptions. Run it the way an agent does, with stdout piped,
and the rest breaks:

```bash
$ uv run examples/tutorial/todo_pydantic.py done 9 --db tmp/tutorial/old.json
no item #9                                 # prose on stderr, exit 1
$ uv run examples/tutorial/todo_pydantic.py purge --db tmp/tutorial/old.json </dev/null
Delete 1 completed items? [y/N]: Traceback (most recent call last):
...
EOFError: EOF when reading a line          # a prompt, then a traceback, exit 1
$ uv run examples/tutorial/todo_pydantic.py add x --priority urgent --db tmp/tutorial/old.json
Traceback (most recent call last):
...
pydantic_core._pydantic_core.ValidationError: 1 validation error for Todo
add.priority
  Input should be 'low', 'normal' or 'high' [type=literal_error, input_value='urgent', input_type=str]
```

- **Output is whatever `cli_cmd()` prints.** `Added #3: Buy milk` has to be parsed with a
  regex that breaks when the wording changes
- **Every failure is exit 1.** `SystemExit("no item #9")`, a failed validator, and a bug all
  look the same, so an agent cannot tell whether to retry, fix its arguments, or stop
- **A value the model refuses is a traceback.** A value outside the `Literal` or a failed
  `model_validator` passes the parser and fails in pydantic, so it exits 1, not 2
- **It prompts.** `input()` in a `cli_cmd()` raises `EOFError` on a closed stdin, and waits
  forever on an open, empty one
- **It cannot describe itself.** The models hold everything a manifest needs, but only
  `--help` text for people, one command at a time, comes out

treaty fixes the first four by construction and the fifth with `manifest`.

## Step 1: Take inventory

The models already list the arguments. What they do not say is what each command does to
state, and how it fails. Write it down as [the click chapter's
table](click-typer.md#step-1-take-inventory) does: one row per `CliSubCommand`, with the
fields, whether `cli_cmd()` writes anything, every `SystemExit` it raises, and a danger
level of `safe`, `mutating`, or `destructive`.

Look for names treaty keeps for itself, from the same list: a field named `config`,
`format`, `verbose`, `quiet`, `debug`, `fields`, or `cwd` has to be renamed, and a field
whose name contains `token`, `secret`, `password`, `key`, or `auth` becomes a secret that
takes no value on the command line.

**Check:** every `CliSubCommand` has a row, and every `SystemExit` and `input()` in a
`cli_cmd()` shows up in the "Fails when" or "Writes?" column

## Step 2: Create the app

The root `BaseSettings` model with one `CliSubCommand` field per command becomes one `App`.
pydantic-settings:

<!-- file: examples/tutorial/todo_pydantic.py -->
```python
class Todo(BaseSettings):
    """Track todo items"""

    model_config = SettingsConfigDict(cli_prog_name="todo")

    add: CliSubCommand[Add]
    list: CliSubCommand[ListItems]
    done: CliSubCommand[Done]
    purge: CliSubCommand[Purge]

    def cli_cmd(self) -> None:
        CliApp.run_subcommand(self)
```

treaty:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
app = App("todo", version="1.0.0", description="Track todo items")
```

The subcommand fields go away: each command registers itself with `@app.command(...)`, as
Step 4 shows, and the root `cli_cmd()` that dispatched to them goes too. The version is
required, and `todo --version` and `todo version` report it. A `CliSubCommand` whose model
has subcommands of its own is a group: `remote = app.group("remote", description="...")`,
and its commands register with `@remote.command("add", ...)`, keyed `remote.add` in the
manifest.

## Step 3: Move the shared base model onto a base class

`todo` gives every command `--db` through a base model:

<!-- file: examples/tutorial/todo_pydantic.py -->
```python
class Common(BaseModel):
    db: Path = Field(Path.home() / ".todo.json", description="Where the items are stored")
```

treaty keeps the idea. The base becomes a frozen dataclass with `kw_only=True`, and `Field`
becomes `Flag`, with the default as `default=`:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
@dataclass(frozen=True, slots=True, kw_only=True)
class Common:
    db: Path | None = Flag(default=None, description="Item file; default ~/.todo.json")
```

`kw_only=True` is what lets a subclass add a field with no default, such as a positional
argument: without it, `Add(Common)` fails at import with `TypeError: non-default argument
'text' follows default argument 'db'`. Every base class of arguments needs it.

The pydantic default was `Path.home() / ".todo.json"`, computed at import. A default in the
manifest should be a fixed value, so the flag defaults to `None` and the fallback moves to
the resource that opens the store, as [the click
chapter](click-typer.md#step-3-move-group-options-onto-a-base-class) shows:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
    @classmethod
    def acquire(cls, args: Common, ctx: Ctx) -> Self:
        return cls(args.db if args.db is not None else Path.home() / ".todo.json")
```

A flag the root `BaseSettings` model declared itself, before the subcommand, moves onto this
base class too, and so after the command: treaty has no flags between the program and the
command. A root field that is configuration rather than an argument moves to settings
instead, in Step 8.

## Step 4: Migrate one command end to end

A subcommand model with a `cli_cmd()` method becomes two things: an arguments dataclass and
a decorated handler. pydantic-settings:

<!-- file: examples/tutorial/todo_pydantic.py -->
```python
class Add(Common):
    """Add an item"""

    text: CliPositionalArg[str] = Field(description="What to do")
    priority: Literal["low", "normal", "high"] = Field("normal", description="How urgent it is")

    def cli_cmd(self) -> None:
        items = load(self.db)
        next_id = max((i["id"] for i in items), default=0) + 1
        items.append({"id": next_id, "text": self.text, "priority": self.priority, "done": False})
        save(self.db, items)
        print(f"Added #{next_id}: {self.text}")
```

treaty keeps the fields and moves the method out:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
@dataclass(frozen=True, slots=True)
class Add(Common):
    text: str = Arg(description="What to do")
    priority: Priority = Flag(default="normal", description="How urgent it is")
```

Field by field: `CliPositionalArg[str] = Field(description=...)` becomes `str =
Arg(description=...)`, and `Field(default, description=...)` becomes `Flag(default=...,
description=...)`. The annotation stays the type; `Priority` is the same `Literal`. A
`CliImplicitFlag[bool]` is a plain `bool` flag, and `validation_alias=AliasChoices("a",
"all")` for a short name becomes `Flag(short="a", ...)`.

`cli_cmd()` becomes a function that returns data instead of printing it. The docstring that
was the command's help becomes `description=`:

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

What `self` held is now `args`, and the store comes from the resource of Step 3. `Changed`
and `Item` are dataclasses of the example file. What is new, `danger_level=`,
`exit_codes=`, `examples=`, the return type, and `effect`, is explained in [the click
chapter's Step 4](click-typer.md#step-4-migrate-one-command-end-to-end).

**Check:** the first item is created, and a value outside the `Literal` exits 2 before the
handler runs, where pydantic-settings exited 1 with a traceback

<!-- check -->
```bash
todo add "Buy milk" --priority high --db tmp/tutorial/todo.json | jq -e '.data == {
  "effect": "created", "item": {"id": 1, "text": "Buy milk", "priority": "high", "done": false}}'
todo add "x" --priority urgent --db tmp/tutorial/todo.json \
  | jq -e '.meta.exit_code == 2 and (.error.message | endswith("must be one of low, normal, high."))'
```

## Step 5: Move validators into `__post_init__`

`todo` has no validators, so this step uses the market example. A shared history model
checks its date range with a `model_validator`:

```python
class HistoryArgs(GlobalArgs):
    symbol: CliPositionalArg[str] = Field(description="Symbol to fetch")
    days: int | None = Field(None, description="Days back from today")
    start: str | None = Field(None, description="First day, YYYY-MM-DD")

    @model_validator(mode="after")
    def one_range(self) -> Self:
        if self.days is not None and self.start is not None:
            raise ValueError("pass --days or --start, not both")
        return self
```

A check on the values goes into the dataclass's `__post_init__`, and raises `ParseError`
instead of `ValueError`:

<!-- file: examples/tutorial/market_args.py -->
```python
@dataclass(frozen=True, slots=True, kw_only=True)
class HistoryArgs(GlobalArgs):
    symbol: str = Arg(description="Symbol to fetch")
    days: int | None = Flag(default=None, description="Days back from today")
    start: str | None = Flag(default=None, description="First day, YYYY-MM-DD")

    def __post_init__(self) -> None:
        if self.start is not None:
            try:
                date.fromisoformat(self.start)
            except ValueError:
                raise ParseError(f"--start {self.start!r} is not a YYYY-MM-DD date") from None
```

`__post_init__` runs before the handler, so a `ParseError` there exits 2 with `ARG_ERROR`,
as the parser's own errors do. Raised from a handler instead, after work may have started,
it exits 1 with `VALIDATION_AFTER_START`. `__post_init__` sees each value already parsed to
its annotation, so a `mode="before"` validator that turns text into a value object becomes
`app.scalar(T, parse=...)` instead, declared before the commands, and the field is annotated
`T`.

A rule between flags, such as "not both", can be moved into `__post_init__` too, but then
an agent learns it only from a failing call; the audit's `conditional-rules` advice says so.
Declare it on the command instead, where `--schema` and the manifest show it:

<!-- file: examples/tutorial/market_args.py -->
```python
HISTORY_RULES = [Excludes("days", prohibited=("start",))]
```

and pass `requires=HISTORY_RULES` on each command that takes `HistoryArgs`. `RequiredWhen`
and `DefaultWhenAbsent` cover the other common cross-field validators: a flag required when
another has a given value, and a default that depends on whether another flag was given.

**Check:** both flags together exit 2 before the handler runs, the rule is in the manifest,
and a date that does not parse exits 2 from `__post_init__`

<!-- check -->
```bash
prices history AAPL --days 5 --start 2026-09-01 | jq -e '.meta.exit_code == 2
  and .error.message == "--days and --start are mutually exclusive."'
prices manifest | jq -e '.data.commands.history.requires == [{"if_flag": "days", "prohibited": ["start"]}]'
prices history AAPL --start 2026-13-01 | jq -e '.meta.exit_code == 2 and .error.code == "ARG_ERROR"'
```

## Step 6: Replace `SystemExit` with a named exit code

pydantic-settings:

<!-- file: examples/tutorial/todo_pydantic.py -->
```python
        raise SystemExit(f"no item #{self.id}")
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

and the command declares it: `exit_codes=["NOT_FOUND"]`. A handler may only raise the codes
its manifest entry lists, so the manifest never lies about how a command can fail. [The click
chapter's Step 5](click-typer.md#step-5-replace-clickexception-and-typerexit-with-a-named-exit-code)
has the rest, and [Declare exit codes](../core/exit-codes.md) declares codes of your own.

**Check:** a missing item exits 5 with `NOT_FOUND`; completing an item twice is `updated`,
then `noop`

<!-- check -->
```bash
todo done 9 --db tmp/tutorial/todo.json | jq -e '.meta.exit_code == 5 and .error.code == "NOT_FOUND"'
todo add "Walk dog" --db tmp/tutorial/todo.json | jq -e '.data.item.id == 2'
todo done 1 --db tmp/tutorial/todo.json | jq -e '.data.effect == "updated"'
todo done 1 --db tmp/tutorial/todo.json | jq -e '.data.effect == "noop"'
```

## Step 7: Replace the prompt with a danger level

`purge` asked before deleting unless `-y` was given:

<!-- file: examples/tutorial/todo_pydantic.py -->
```python
        if not self.yes and input(f"Delete {len(completed)} completed items? [y/N]: ") != "y":
            raise SystemExit("Aborted!")
```

treaty never prompts. The command is declared `destructive` with a `dry_run` flag, and
`--confirm-destructive` replaces `-y`; without it, treaty runs the handler as a dry run and
exits 2 with what would have been deleted in `data`. [The click chapter's
Step 6](click-typer.md#step-6-replace-the-confirmation-with-a-danger-level) walks through
`purge`.

**Check:** without confirmation, `purge` exits 2 and lists item 1 without deleting it; with
confirmation it deletes item 1 and keeps item 2

<!-- check -->
```bash
todo purge --db tmp/tutorial/todo.json | jq -e '.meta.exit_code == 2
  and .error.code == "CONFIRMATION_REQUIRED" and [.data.deleted[].id] == [1]'
todo purge --db tmp/tutorial/todo.json --confirm-destructive | jq -e '[.data.deleted[].id] == [1]'
todo list -a --db tmp/tutorial/todo.json | jq -e '[.data[].id] == [2]'
```

## Step 8: Move `BaseSettings` fields into `App(settings=...)`

A `BaseSettings` root model is also configuration: each field reads an environment
variable, and the same value can come as a flag.

```python
class Todo(BaseSettings):
    model_config = SettingsConfigDict(cli_prog_name="todo", env_prefix="TODO_")

    feed_url: str = Field("", description="The list import reads when --url is not given")
```

treaty keeps arguments and settings apart. A value an agent passes per call is a `Flag` on
the command's arguments. A value that comes from the environment or a config file is a
field of a frozen settings dataclass, passed to the app, as [Read settings and
secrets](../core/config.md) does for `todo`:

<!-- file: examples/tutorial/todo_config.py -->
```python
@dataclass(frozen=True, slots=True)
class Settings:
    feed_url: str = ""
    """The list import reads when --url is not given; "" when there is none"""
```

`App(..., settings=Settings)` reads `TODO_FEED_URL`, then `.todo.toml` in the project, then
the user's `todo/config.toml`, then the default. The prefix is the app's name, so an
`env_prefix` that differs from it changes the variable's name. A handler or a resource takes
`settings: Settings` as a parameter, and `--show-config` reports each value and where it
came from. When a value should work both ways, as a pydantic-settings field did, keep a
`Flag` that defaults to `None` and fall back to the setting in the handler.

A key or a token, such as an `api_key` field, becomes a `Flag` on the commands that use it,
on a shared base class when several do. Its name makes it a secret: it takes no value on the
command line, and reads `PRICES_API_KEY` for an app named `prices`, or the variable or file
named by `--api-key-from-env` or `--api-key-from-file`, as [Read settings and
secrets](../core/config.md#step-5-take-secrets-from-the-environment-or-a-file) shows.

**Check:** the environment sets `feed_url`, and `--show-config` names the variable

<!-- check -->
```bash
TODO_FEED_URL=https://example.com/mine.json uv run examples/tutorial/todo_config.py import \
  --no-config --show-config | jq -e '.data.effective_config.feed_url == "https://example.com/mine.json"
    and .data.sources.feed_url == "env:TODO_FEED_URL"'
```

## Step 9: Swap the entry point

The module ran the root model:

<!-- file: examples/tutorial/todo_pydantic.py -->
```python
if __name__ == "__main__":
    CliApp.run(Todo)
```

and now runs the app:

<!-- file: examples/tutorial/todo_treaty.py -->
```python
if __name__ == "__main__":
    app.main()
```

The console script changes the same way, to `todo = "todo.cli:app.main"`. Tests that called
`CliApp.run(Todo, cli_args=[...])` and read what it printed call `app.call("done", {...})`
and read the envelope instead, as [the click chapter's
Step 9](click-typer.md#step-9-test-through-the-envelope) shows.

**Check:** the manifest lists the four commands, and `--version` reports the app's version

<!-- check -->
```bash
todo manifest | jq -e '.data.commands | has("add") and has("list") and has("done") and has("purge")'
todo --version | jq -e '.data == {"name": "todo", "version": "1.0.0"}'
```

## Share argument models across several CLIs

A shared package often holds the argument models of several CLIs, such as two market-data
providers whose `lookup` and `history` take the same arguments. Migrate the package and its
CLIs together: keep one set of frozen dataclasses in the shared package, rather than a copy
in each CLI, where the copies drift.

The shared classes are bases with `kw_only=True`, each adding to the one before:

<!-- file: examples/tutorial/market_args.py -->
```python
@dataclass(frozen=True, slots=True, kw_only=True)
class GlobalArgs:
    currency: str = Flag(default="USD", pattern="[A-Z]{3}", description="Currency to quote in")


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchArgs(GlobalArgs):
    query: str = Arg(description="Symbol, ISIN, or name")
```

A CLI that needs nothing more annotates its handler with a shared class directly:

<!-- file: examples/tutorial/market_funds.py -->
```python
def lookup(args: SearchArgs, ctx: Ctx) -> Fund:
    return Fund(name=args.query, currency=args.currency)
```

A CLI that adds a flag subclasses it, as a pydantic-settings command subclassed the shared
model:

<!-- file: examples/tutorial/market_prices.py -->
```python
@dataclass(frozen=True, slots=True)
class Lookup(SearchArgs):
    report_price: bool = Flag(default=False, description="Include the current price")
```

Because the bases are `kw_only`, the subclass can add a required field too, such as another
`Arg`; positionals come in field order, the base's first. Rules between flags live on the
command, not the dataclass, so the shared package exports them next to the class, as
`HISTORY_RULES` in Step 5, and each command passes `requires=`.

The shared package then depends on treaty, which needs Python 3.14. A library that supports
older versions keeps treaty out of its required dependencies: in an optional extra, or in a
separate CLI distribution that holds the shared models, which suits several provider CLIs
that migrate together.

**Check:** both CLIs take the shared positional and flag, and only `prices` adds
`--report-price`

<!-- check -->
```bash
prices lookup aapl --report-price | jq -e '.data == {"symbol": "AAPL", "currency": "USD", "price": 187.5}'
funds lookup "world index" --currency EUR | jq -e '.data == {"name": "world index", "currency": "EUR"}'
funds lookup x --report-price | jq -e '.meta.exit_code == 2'
```

## pydantic-settings to treaty at a glance

| pydantic-settings | treaty |
| --- | --- |
| `class Root(BaseSettings)`, `CliApp.run(Root)` | `App(name, version=..., description=...)`, `app.main()` |
| `x: CliSubCommand[Model]` | `@app.command("x", danger_level=..., exit_codes=...)` on a handler |
| a `CliSubCommand` model with subcommands | `app.group("x", description=...)` |
| `cli_cmd()`, `CliApp.run_subcommand(self)` | the decorated handler; treaty dispatches |
| a model's docstring | `description=` on the command |
| `CliPositionalArg[T] = Field(description=...)` | `T = Arg(description=...)` |
| `T = Field(v, description=...)` | `T = Flag(default=v, description=...)` |
| a field with no default | a `Flag` with no default |
| `CliImplicitFlag[bool]` | `bool = Flag(default=False, ...)`: treaty adds `--no-x` for a default of `True` |
| `validation_alias=AliasChoices("a", "all")` | `Flag(short="a", ...)` |
| `Field(pattern="^...$")` | `Flag(pattern="...")`, matched against the whole value and shown in the manifest |
| `Literal[...]`, an `Enum` | the same annotation |
| a shared `BaseModel` base | a frozen `kw_only` base dataclass |
| `model_validator(mode="after")`, `field_validator` | `__post_init__` raising `ParseError`: exit 2 |
| `model_validator(mode="before")` that builds a value object | `app.scalar(T, parse=...)`, and the field annotated `T` |
| a validator relating two flags | `requires=[Excludes(...)]`, `RequiredWhen`, or `DefaultWhenAbsent` on the command |
| `BaseSettings` env fields | `App(settings=Settings)`, read from `TODO_X` and config files |
| an `api_key` or `SecretStr` field | a `Flag`, secret by its name or `secret=True`: `TODO_API_KEY`, `--api-key-from-env`, `--api-key-from-file` |
| `print(...)` in `cli_cmd()` | return a dataclass; add a renderer for custom text |
| `SystemExit(msg)` | `raise Exit.NAME(msg, ...)`, declared in `exit_codes=` |
| `input()`, a `--yes` flag | `danger_level="destructive"`, `dry_run`, `--confirm-destructive` |
| `CliApp.run(Root, cli_args=[...])` in tests | `app.call(...)`, or `app.run(argv, ...)` |

## What changes for the people using your CLI

Migration is a breaking change for callers. Put this list in your release notes:

- Flags of the root model go after the command, or become settings read from the
  environment and config files
- Flag names use dashes: `--report_price` becomes `--report-price`, unless the CLI already
  set `cli_kebab_case=True`
- Environment variables take the app's name as their prefix: `TODO_FEED_URL` for the app
  `todo`, whatever `env_prefix` was
- `-y` and `--yes` are gone from destructive commands, which take `--confirm-destructive`,
  and without it they show what they would do and exit 2
- A value the model refused, or a failed validator, exits 2 instead of 1 with a traceback
- A command that returns a list, such as `list`, returns 20 items at a time; `--limit 0`
  returns all of them, and `--cursor` the next page ([Page long
  lists](../core/pagination.md))
- Output is JSON whenever stdout is not a terminal; scripts that read the printed text should
  read JSON, or pass `--format plain`
- Exit codes change: failures that were all 1 now have their own numbers, listed in
  `todo manifest`
- Secret fields (`--api_key`, `--token`) no longer take a value on the command line; use
  `--api-key-from-env VAR`, `--api-key-from-file PATH`, or the environment
- `--config` and `--format` are now treaty's; the CLI's own options of those names are
  renamed (list the new names)

## Next

Follow the audit's rules in order, one chapter each, starting with the first: [Describe
every command](../core/describe.md), then [Choose each command's danger
level](../core/danger-level.md). `todo` already passes both, since this chapter gave every
command an example and a danger level; they say how to do it well for your own CLI. The rule
`todo` still fails, `exit-codes`, comes third: [Declare exit codes](../core/exit-codes.md).
