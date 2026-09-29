# Declare exit codes

**Goal:** every way a command can fail that a caller can act on has its own exit code,
declared in the manifest with whether a retry is safe and what state the failure left behind

**You need:** a treaty app, such as the end of [Start a new CLI](../A-new/start.md),
[Migrate an argparse CLI](../B-migrate/argparse.md), or [Migrate a click or typer
CLI](../B-migrate/click-typer.md); this chapter clears the audit rules `exit-codes` and
`retryable`

**Done when:** the strict audit exits 0:

<!-- check -->
```bash
uv run treaty audit examples.tutorial.todo_exit_codes:app --strict > /dev/null
```

The chapter continues the `todo` CLI. It starts from
[`examples/tutorial/todo_treaty.py`](../../../examples/tutorial/todo_treaty.py) and ends at
[`examples/tutorial/todo_exit_codes.py`](../../../examples/tutorial/todo_exit_codes.py).

Run the **Check** commands from the root of a treaty checkout, in order: each one uses the
files the one before it left behind. Here `todo` runs this chapter's finished example:

<!-- check -->
```bash
todo() { uv run examples/tutorial/todo_exit_codes.py "$@"; }
rm -rf tmp/tutorial && mkdir -p tmp/tutorial
```

## What an agent does with an exit code

An exit code is a decision. Before it makes a call, an agent can read every code a command
may return from the manifest, each with a `retryable` flag and a `side_effects` value. After
the call, the code picks the next move:

| The run ended with | The agent |
| --- | --- |
| `0` | uses `data` |
| `2` (`ARG_ERROR`) | fixes the arguments listed in `error.errors` and reissues |
| a code with `retryable: true` | waits `error.retry_after_ms`, or backs off, and reissues the same call |
| a code with `retryable: false` | meets `error.fix_required`, running `error.fix_command` if there is one, then reissues once |
| `1` (`GENERAL_ERROR`) | assumes state is partly changed, inspects it, and does not retry blindly |

`side_effects` says what the failed call left behind: `none` (nothing changed), `partial`
(some of the work happened), or `complete` (the work happened, but the call still failed,
for example on the way out). An agent can safely reissue a call only if it failed with
`none`.

## Where todo stands

Point the migrated `todo` at a damaged file and it crashes:

<!-- check -->
```bash
echo '{not json' > tmp/tutorial/bad.json
uv run examples/tutorial/todo_treaty.py add x --db tmp/tutorial/bad.json 2> /dev/null \
  | jq -e '.meta.exit_code == 1 and .error.code == "HANDLER_CRASHED"
    and (.error.message | startswith("Command add raised JSONDecodeError"))'
```

The message is honest, but the exit code misleads. Exit 1 is `GENERAL_ERROR`, declared with
`side_effects: "partial"`, so the agent has to assume the item file may have changed, when
nothing was written. It also cannot tell this case from a real bug. The audit points at the
same gap:

```bash
$ uv run treaty audit examples.tutorial.todo_treaty:app --format plain
...
  1. (warning) exit-codes [add]: non-safe command declares no command-specific exit codes; agents cannot tell failures apart
  2. (warning) exit-codes [purge]: non-safe command declares no command-specific exit codes; agents cannot tell failures apart
```

## Step 1: List the failures a caller can act on

Go through each command and write down what can go wrong. Give a failure its own code only
when the caller would respond to it differently; one code per remedy, not one per exception
class:

| Failure | What the caller can do | Code |
| --- | --- | --- |
| the item file is not valid JSON, or not a list of items | pass `--db` with another path | `STORE_CORRUPT` |
| the item file's directory is missing or read-only | create the directory, or pass another path | `STORE_UNWRITABLE` |
| `done` names an item that does not exist | list the items and pick a real number | `NOT_FOUND` (already declared) |
| the file cannot be read for some other reason | nothing specific | none: let it crash |

Check the framework codes first. `NOT_FOUND`, `CONFLICT`, `PRECONDITION`, `PERMISSION_DENIED`,
`RATE_LIMITED`, `UNAVAILABLE`, and the rest of `0` to `13` are described in every
manifest, and agents already know them. Use one when its description fits. Declare your
own code when you need a name the caller can recognise on sight, as `STORE_CORRUPT` is
here.

The last row matters too. A failure nobody can act on stays unhandled and ends as
`HANDLER_CRASHED` with a traceback on stderr, which is the right answer for a bug.

**Check:** every mutating and destructive command has at least one row, and every row
names a different thing the caller does

## Step 2: Make the failure leave nothing behind

Before choosing `side_effects` for `STORE_UNWRITABLE`, look at what a failed write really
does. `Path.write_text` truncates the file and then writes it; if the disk fills halfway,
the item file is left half-written. That failure would have to be declared `partial`, and it
loses data.

Write to a sibling file and rename it over the old one instead. A rename is atomic, so the
file is either the old version or the new one:

<!-- file: examples/tutorial/todo_exit_codes.py -->
```python
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
```

`side_effects` describes what the code guarantees, so the code has to make it true.
Declaring `none` when a failure can leave half a file is worse than declaring nothing.

**Check:** add an item, make the directory read-only, and add another. The run exits 80,
the file still holds the first item, and no `.partial` file is left behind. The second
`add` fails on purpose, so its output goes to a file and the directory is made writable
again before anything is checked (run this as a normal user: root ignores the read-only bit)

<!-- check -->
```bash
mkdir tmp/tutorial/ro
todo add first --db tmp/tutorial/ro/todo.json | jq -e '.data.effect == "created"'
chmod a-w tmp/tutorial/ro
todo add second --db tmp/tutorial/ro/todo.json > tmp/tutorial/second.json || true
chmod u+w tmp/tutorial/ro
jq -e '.meta.exit_code == 80 and .error.code == "STORE_UNWRITABLE"' tmp/tutorial/second.json
jq -e '[.[].text] == ["first"]' tmp/tutorial/ro/todo.json
test "$(ls -A tmp/tutorial/ro)" = todo.json
```

## Step 3: Register the codes

<!-- file: examples/tutorial/todo_exit_codes.py -->
```python
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
```

Registration checks the rules:

- **Numbers are 79 to 125.** `0` to `13` belong to the framework, and `126` and up mean
  something to the shell (`127` is "command not found", `128 + n` is "killed by signal n");
  any other number is refused when the app is built
- **The description is 1 to 120 characters with no trailing period.** It is what an agent
  reads in the manifest, so say what happened and what state it left
- **A retryable code must have `side_effects="none"`.** See [Retryable codes](#retryable-codes)

Once released, a number is part of your contract. Scripts and agents branch on `79`, so
never renumber a code or reuse a retired number for something else.

## Step 4: Raise with enough context to act on

Every `Exit.NAME(...)` takes a message and a few optional fields. Fill in the ones the
caller can use:

| Field | Holds | todo example |
| --- | --- | --- |
| `message` | one line saying what failed | `tmp/bad.json is not a todo file` |
| `context` | the facts, as values a program can read | `{"path": ..., "cause": ...}` |
| `fix_required` | the condition the caller must fix before reissuing | `--db must name a todo file...` |
| `fix_command` | one command that fixes it: runs as is, no placeholders, never destructive | `mkdir -p tmp/nodir` |
| `suggestion` | the next step, phrased for an agent | `pass --db with another path...` |
| `retry_after_ms` | how long to wait, on retryable codes only | |

`fix_command` is the strongest hint an agent can get, so only set it when it is certain to
help. `save` sets `mkdir -p` only when the directory is missing; a read-only directory has
no safe one-command fix, so it gets `fix_required` alone.

treaty checks every `fix_command` before it reaches the agent: one command, no `<`, `>`,
`$`, pipes, or `;`, and never a destructive command of the tool. The program must be the
tool itself or a companion the app declares, so the todo app names `mkdir`:

<!-- file: examples/tutorial/todo_exit_codes.py -->
```python
app = App("todo", version="1.0.0", description="Track todo items", companions=("mkdir",))
```

A fix that fails the check ends the run as `INVALID_EXIT` instead. A fix that is the same
for every failure of a code can be declared once, where treaty checks it at startup:
`@app.command(..., fix_commands={"STORE_MISSING": "todo init"})`.

Loading follows the same pattern. Catch the exceptions that mean "damaged file", and
nothing wider:

<!-- file: examples/tutorial/todo_exit_codes.py -->
```python
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
```

`TypeError` covers valid JSON of the wrong shape: an object where the list should be, or
an item with missing or extra keys. A `PermissionError` on read is not caught and stays a
crash, as Step 1 decided.

## Step 5: Declare the codes on each command

A command may raise only the codes it lists in `exit_codes=`. The store is shared, so each
command lists what its own path can reach:

<!-- file: examples/tutorial/todo_exit_codes.py -->
```python
    danger_level="mutating",
    exit_codes=["STORE_CORRUPT", "STORE_UNWRITABLE"],
```

`list` only reads, so it declares `exit_codes=["STORE_CORRUPT"]`, and `done` keeps its
`NOT_FOUND`: `exit_codes=["NOT_FOUND", "STORE_CORRUPT", "STORE_UNWRITABLE"]`.

Forgetting one does not fail silently. A handler that raises a code it did not declare exits
1 with `UNDECLARED_EXIT_CODE`, and names the code it raised and the ones it declared:

```json
{"code": "UNDECLARED_EXIT_CODE", "message": "Command go raised STORE_CORRUPT, which it does not declare.",
 "context": {"declared": [], "original_message": "bad", "raised": "STORE_CORRUPT"}}
```

Mutating and destructive commands also get `CONFLICT` without declaring it, and every
command gets `GENERAL_ERROR`, `ARG_ERROR`, `PRECONDITION`, and `TIMEOUT`.

**Check:** the damaged file from [Where todo stands](#where-todo-stands) is now
`STORE_CORRUPT` and left as it was, a missing directory gets its `fix_command`, and the
schema lists both codes

<!-- check -->
```bash
todo add x --db tmp/tutorial/bad.json | jq -e '.meta.exit_code == 79 and .error.code == "STORE_CORRUPT"'
test "$(cat tmp/tutorial/bad.json)" = '{not json'
todo add x --db tmp/tutorial/nodir/todo.json | jq -e '.meta.exit_code == 80
  and .error.fix_command == "mkdir -p tmp/tutorial/nodir"'
todo add --schema | jq -e '.data.exit_codes | has("79") and has("80")'
```

## Retryable codes

`retryable: true` tells an agent it may reissue the identical call without asking anyone.
That is a strong promise, so treaty constrains it in two places:

- **Registration** refuses a retryable code whose `side_effects` is not `none`:
  `STORE_BUSY: retryable exit codes must declare side_effects 'none'`. A retry after a
  partial failure would apply the work twice
- **The `retryable` audit rule** warns when a mutating or destructive command declares a
  retryable code of its own. Even with `side_effects="none"`, a retry is only safe if the
  command is idempotent, and `add` is not: it adds a second item when a retry happens after
  a success whose response was lost

For a transient failure (an upstream timeout, a lock held by another process, a rate limit),
use the framework's `UNAVAILABLE` (12) or `RATE_LIMITED` (11) instead of a code of your
own. Agents already know both, the rule accepts them, and `retry_after_ms` carries the
back-off when you know it. On a non-idempotent command, point callers at the
`--idempotency-key` the command already has: with a key, a retry after a lost response
returns the first result instead of running again.

`todo` has no transient failures, so it declares no retryable codes.

## Next

Run the audit:

```bash
uv run treaty audit examples.tutorial.todo_exit_codes:app --strict
```

For `todo` it exits 0. The audit still lists two pieces of advice for `add`, which `todo`
leaves on purpose; [the index](../index.md#advice-you-can-leave) says why.

The audit's next rule is `typed-output`, which checks that every command's result has a
schema an agent can read: [Type every command's output](typed-output.md). Each core chapter
links the next one; when the strict audit of your own CLI exits 0 and you want to skip the
rules it already passes, go on to the conformance kit, which runs the commands and checks
their envelopes against the spec: [Run the conformance kit](../ship/conformance.md).
