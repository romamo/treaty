# Describe every command

**Goal:** every command says what it does, every field says what it holds, and every
command shows a call an agent can copy, with a test that fails when an example stops working

**You need:** a treaty app, such as `todo` at the end of any earlier chapter; this chapter
clears the audit rule `describe`

**Done when:** the audit has no `describe` finding, and a test runs every example with
`--validate-only`:

<!-- check -->
```bash
uv run treaty audit examples.tutorial.todo_exit_codes:app \
  | jq -e '[.data.next_steps[] | select(.rule == "describe")] == []'
```

The chapter uses `todo` as [Declare exit codes](exit-codes.md) left it,
[`examples/tutorial/todo_exit_codes.py`](../../../examples/tutorial/todo_exit_codes.py);
nothing here depends on its exit codes.

## Running the checks

Run the **Check** commands from the root of a treaty checkout. `todo` runs the example:

<!-- check -->
```bash
todo() { uv run examples/tutorial/todo_exit_codes.py "$@"; }
```

Each check exits non-zero when it fails: JSON output goes through `jq -e`, which exits 1 when
the condition is false. `tests/test_tutorial.py` runs the checks the same way.

## What an agent reads

An agent picks a command, fills in its arguments, and reads the result, all from text you
wrote. treaty requires two kinds of that text and asks for a third:

| Text | Declared as | Required | Where an agent reads it |
| --- | --- | --- | --- |
| What a command does | `description=` on `@app.command` | yes | the manifest, `--help`, AGENTS.md, the skill's `description`, the MCP tool description |
| What a field holds | `description=` on every `Arg` and `Flag` | yes | the manifest, `--schema`, `--help`, the skill's flag table, the MCP input schema |
| A call to copy | `examples=` on `@app.command` | no; the audit asks | the manifest, `--help`, the skill's usage, and the conformance probes |

An empty command or field description fails registration. Examples are optional, so the
audit's `describe` rule reports every command of yours without one:

```bash
$ uv run treaty audit myapp.cli:app --format plain
...
  1. (advice) describe [done]: no example invocation; agents copy examples verbatim as starting points
     fix: examples=[("What it does", "todo done <id>")]
```

The finding is `advice`, the lowest severity, so it never fails `--strict`. Fix it anyway:
of the three texts, the example is the one an agent uses without rewording.

## Step 1: Say what each command does

The command description is one line, starting with a verb, that says what the command does
to what: `Add an item`, `Mark an item completed`, `Delete completed items`. It is the
line an agent reads when it chooses between commands, in the manifest and in AGENTS.md's
command list, so it names the effect, not the implementation.

- Say what changes. `Delete completed items` tells an agent what it will lose;
  `Clean up the store` does not
- Leave out what the declarations say. The danger level, the exit codes, and the flags are
  in the manifest already; the MCP tool description even appends the danger level for you
- Keep it to one line with no trailing period, matching treaty's own commands, since it is
  printed in lists

**Check:** the description is what `todo add --help` prints under the usage line, and what
the manifest carries

<!-- check -->
```bash
todo add --help 2>&1 >/dev/null | sed -n 3p | grep -qx 'Add an item'
todo manifest | jq -e '.data.commands.add.description == "Add an item"'
```

`--help` writes its text on stderr when stdout is piped, and an envelope on stdout, so a
program reading stdout never gets prose.

## Step 2: Say what each field holds

A field description says what the value is, and anything about it an agent cannot tell from
the type: the unit, what an empty or missing value means, the default when the declared
default is `None`. `todo`'s `--db` defaults to `None` in the manifest, because the real
default depends on the machine, so the description names it:

<!-- file: examples/tutorial/todo_exit_codes.py -->
```python
    db: Path | None = Flag(default=None, description="Item file; default ~/.todo.json")
```

- Name the unit: `Seconds to wait`, not `Timeout`
- Say what the absence means when it is not obvious: `Include completed items` on `--all`
- Do not repeat the type, the allowed values, or the default the manifest already shows:
  `How urgent it is`, not `One of low, normal, high (default normal)`

**Check:** the description reaches `--schema` next to the type

<!-- check -->
```bash
todo add --schema | jq -e '.data.flags.db == {"description": "Item file; default ~/.todo.json",
  "pattern_type": "filepath", "required": false, "type": "string"}'
```

## Step 3: Write examples an agent can copy

Each example is a pair: what the call does, and the call. `add`'s:

<!-- file: examples/tutorial/todo_exit_codes.py -->
```python
    examples=[("Add an urgent item", 'todo add "Buy milk" --priority high')],
```

An agent copies the command and edits the values, so write it the way it should be called:

- **Real values, never placeholders.** The audit's suggested fix writes `todo done <id>`;
  replace `<id>` with `3`. A placeholder copied as is fails, and so does the test in Step 4
- **The whole call.** Start with the program name, then the command path, then the
  arguments, with flags after the path, as the parser wants them
- **One example per way of using the command.** `purge` could show the preview as well as
  the real run: `("Preview what would be deleted", "todo purge --dry-run")`
- **Put the most useful call first.** `--help` and the skill file list examples in order,
  and the conformance kit builds its probe from the first one: a `safe` command's first
  example runs as it is, so it has to work on an empty sandbox, and a `destructive`
  command's first example has `--confirm-destructive` and `--dry-run` removed, then runs as
  a preview and as a refused call

The first example of a command with required arguments matters even more: without any
example, the kit cannot make up values for them and gives that command no probe at all.

**Check:** every `todo` command has an example, and `--help` shows it under Examples

<!-- check -->
```bash
todo manifest | jq -e '[.data.commands | .add, .list, .done, .purge | .examples | length > 0] | all'
todo add --help 2>&1 >/dev/null | grep -qx '  todo add "Buy milk" --priority high'
```

## Step 4: Test every example

Registration checks only that an example is valid shell: an unclosed quote fails, but
nothing checks the call against the command. Rename `--priority` to `--urgency` and the
example still registers, still ships in the manifest, and fails the first agent that copies
it:

```bash
$ todo add "Buy milk" --priority high --validate-only    # after the rename
# exit 2, error.code ARG_ERROR: "Unknown flag '--priority'."
```

`--validate-only` is the check: it parses the arguments and stops, exit 0 or exit 2 with
every error, without running the handler. A test that passes every example through it
catches a stale flag, a wrong argument type, and a leftover placeholder. It reads the
examples from the manifest, so a new command's examples are tested without touching the
test:

<!-- file: tests/test_tutorial.py -->
```python
def app_examples(app: App) -> list[str]:
    """Every example of the app's own commands, in manifest order; built-ins have their own"""
    commands = app.manifest()["commands"]
    assert isinstance(commands, dict)
    builtins = {path.value for path in app.builtins}
    return [
        example["command"]
        for name, command in commands.items()
        if name not in builtins
        for example in command.get("examples", [])
    ]


EXAMPLE_APPS = [app, todo_exit_codes.app, todo_network.app, todo_payload.app]
"""todo as the chapters leave it: todo_treaty.py, todo_exit_codes.py, and its two branches"""


@pytest.mark.parametrize(
    ("cli_app", "example"), [(a, e) for a in EXAMPLE_APPS for e in app_examples(a)]
)
def test_an_example_parses(cli_app: App, example: str) -> None:
    """Registration checks only the quoting: a renamed flag or a <placeholder> fails here"""
    argv = shlex.split(example)[1:]  # without the program name
    out = io.StringIO()
    code = cli_app.run([*argv, "--validate-only"], stdout=out, stderr=io.StringIO(), env={})
    assert code == 0, out.getvalue()
```

The tutorial checks every version of `todo` it ships; in your project, `EXAMPLE_APPS` is
just `[app]`. The test calls
`app.run` with the parsed words, so no shell ever runs an example; an example that uses a
pipe or a redirect belongs in prose, not in `examples=`.

**Check:** a renamed flag and a leftover placeholder both fail validation, and `todo`'s own
examples pass the test

<!-- check -->
```bash
todo add "Buy milk" --urgency high --validate-only \
  | jq -e '.meta.exit_code == 2 and (.error.message | startswith("Unknown flag"))'
todo done '<id>' --validate-only \
  | jq -e '.meta.exit_code == 2 and (.error.message | endswith("expects an integer."))'
uv run pytest -q tests/test_tutorial.py -k test_an_example_parses
```

## Next

The audit's next rule is `danger-level`, which checks that a command's danger level
matches what its name implies: [Choose each command's danger level](danger-level.md).
