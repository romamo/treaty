# Choose each command's danger level

**Goal:** every command declares the danger level that matches what it does to state, so
the framework gives it the right guards and agents know which calls are safe to repeat

**You need:** a treaty app, such as `todo` at the end of [Describe every command](describe.md);
this chapter clears the audit rule `danger-level`

**Done when:** the audit has no `danger-level` finding; in your own project, run
`uv run treaty audit todo.cli:app` and look for it:

<!-- check -->
```bash
uv run treaty audit examples.tutorial.todo_treaty:app \
  | jq -e '[.data.rules[].findings[] | select(.rule == "danger-level")] == []'
```

The chapter uses `todo` as the starting chapters leave it,
[`examples/tutorial/todo_treaty.py`](../../../examples/tutorial/todo_treaty.py).

## Running the checks

Run the **Check** commands from the root of a treaty checkout, in order. `todo` runs the
example, and the item file goes to a scratch directory. So do the idempotency records of Step
3, through `TODO_STATE_DIR`: every environment variable treaty reads for an app starts with
the app's name in capitals, and this one says where the records are kept.

<!-- check -->
```bash
todo() { uv run examples/tutorial/todo_treaty.py "$@"; }
rm -rf tmp/tutorial && mkdir -p tmp/tutorial
export TODO_STATE_DIR="$PWD/tmp/tutorial/state"
```

Each check exits non-zero when it fails: JSON output goes through `jq -e`, which exits 1 when
the condition is false.

## What the danger level decides

`danger_level=` is required on every command, and it is not a label: it switches framework
behaviour on and off. An agent reads it from the manifest before calling, and the framework
enforces it on every call:

| | `safe` | `mutating` | `destructive` |
| --- | --- | --- | --- |
| What it does to state | reads only | changes it | removes what cannot be restored |
| `effect` in the result | no | required | required |
| `--idempotency-key` | no | yes | yes |
| `CONFLICT` (6) without declaring it | no | yes | yes |
| `TIMEOUT` (10) | retryable, nothing changed | not retryable, state may be partial | not retryable, state may be partial |
| `dry_run` flag and `would_affect` | no | no | required |
| Runs without `--confirm-destructive` | yes | yes | no: previews and exits 2 |
| MCP tool hints | read-only, idempotent | none | destructive |
| Conformance kit probes it | yes, from its first example | never | yes, as a preview and a refusal |

The MCP row is what a client such as a desktop assistant is told about each tool: whether
it only reads, may be repeated, or destroys data. Two more rules follow from the level: only
a `safe` command may stream its output, one JSON line per event as it goes, and a command
that writes the app's config file cannot be `safe`. Registration enforces the requirements in the
table: a `mutating` command whose result has no `effect` field, or a `destructive` one
without a boolean `dry_run`, fails when the app is built.

## Step 1: Decide by the worst thing a call can do

Ask one question per command, in this order, and stop at the first yes:

1. **Can a call remove something that no command of the tool can bring back?** Then it is
   `destructive`. Deleting, overwriting without a backup, dropping, revoking, and wiping
   all count, even when the call usually removes nothing
2. **Can a call change state that the caller, another command, or another user can see?**
   Then it is `mutating`: creating, updating, setting, starting, sending, and deploying
3. **Otherwise** it is `safe`: it reads, and repeating it any number of times changes
   nothing a caller relies on

Judge by the worst call, not the typical one. `purge` with no completed items deletes
nothing, but the next call might delete everything completed, so it is `destructive`.
`done` changes an item but loses nothing: the item and its text are still there, so it is
`mutating`. Writes the caller never relies on, such as a cache or a log line, do not make a
command `mutating`.

For `todo`:

| Command | Worst call | Level |
| --- | --- | --- |
| `list` | reads the item file | `safe` |
| `add` | appends an item | `mutating` |
| `done` | marks an item completed | `mutating` |
| `purge` | deletes every completed item | `destructive` |

**Check:** the manifest carries the four levels

<!-- check -->
```bash
todo manifest | jq -e '.data.commands | {add, list, done, purge} | map_values(.danger_level)
  == {"add": "mutating", "list": "safe", "done": "mutating", "purge": "destructive"}'
```

## Step 2: What `safe` promises

A `safe` command promises that nothing changes, so an agent may call it whenever it wants
to know something, and retry it freely. The framework holds it to that: no idempotency key
(there is nothing to deduplicate), and a timeout is declared retryable with nothing
changed, because a read that ran out of time wrote nothing.

`list` is `safe`, and its schema shows it:

**Check:** no `--idempotency-key`, and `TIMEOUT` is retryable with no side effects

<!-- check -->
```bash
todo list --schema | jq -e '(.data.flags | has("idempotency-key") | not)
  and .data.exit_codes["10"].retryable and .data.exit_codes["10"].side_effects == "none"'
```

Declaring a command `safe` when it writes breaks all of that at once: agents retry it, MCP
clients call it without asking, and the conformance kit runs it against whatever the
launcher points at.

## Step 3: What `mutating` adds

A `mutating` command changes state, so a caller needs to know what changed and needs a way
to retry without changing it twice:

- **`effect`** in the result: `created`, `updated`, `deleted`, or `noop`. An agent that
  retries reads `noop` and knows the first call already did the work
- **`--idempotency-key`**: a second call with the same key returns the first result without
  running the handler. A retry after a lost response is then safe even for `add`, which
  would otherwise add a second item. Reusing a key with different arguments exits 6
  (`CONFLICT`), which is why every `mutating` command can exit 6 without declaring it
- **`TIMEOUT` is not retryable**: the call may have written half its work, so an agent
  inspects state before calling again

**Check:** `add` has the key and exit 6 in its schema, and a repeated key returns the first
item instead of adding a second

<!-- check -->
```bash
todo add --schema | jq -e '(.data.flags | has("idempotency-key")) and (.data.exit_codes | has("6"))
  and .data.exit_codes["10"].side_effects == "partial"'
todo add "Buy milk" --idempotency-key k1 --db tmp/tutorial/todo.json | jq -e '.data.effect == "created"'
todo add "Buy milk" --idempotency-key k1 --db tmp/tutorial/todo.json \
  | jq -e '.data.effect == "noop" and .meta.idempotency_hit and .data.item.id == 1'
todo list --db tmp/tutorial/todo.json | jq -e '.data | length == 1'
```

## Step 4: What `destructive` adds

A `destructive` command gets everything `mutating` does, and a confirmation gate. It must
declare a boolean `dry_run` flag, and its result a `would_affect: Affects | None = None`
field that a dry run fills in. Without `--confirm-destructive`, treaty runs the handler as a
dry run and exits 2 with `CONFIRMATION_REQUIRED`, the preview in `data`. The manifest marks
the command `requires_confirmation: true`, and MCP clients see the destructive hint.

When most calls of a command are previews, `safe_default=True` turns the gate around: the
command previews and exits 0 by default, and `--live` applies it. Use it for commands such
as a cleanup an agent runs often to see what it would remove; keep the default gate when
applying is the usual intent.

A destructive command that takes an id has one more case: the id is already gone. A
retried delete should succeed rather than fail, so it answers `noop` when confirmed, and
on a dry run `would_delete` with an empty preview; the audit's `delete-not-found` rule asks
for this when such a command declares `NOT_FOUND`:

```python
if found is None:
    if args.dry_run:
        return Removed("would_delete", Affects(f"Deletes nothing: no #{args.id}", (), 0))
    return Removed("noop")
```

A `destructive` command is also never offered as a fix: a `fix_command` that runs one is
refused, since a fix must be safe to run twice, so an agent following a suggestion never
deletes anything by accident.

**Check:** `purge` requires confirmation, and without it previews and refuses

<!-- check -->
```bash
todo purge --schema | jq -e '.data.requires_confirmation
  and (.data.flags | has("confirm-destructive") and has("dry-run"))'
todo purge --db tmp/tutorial/todo.json \
  | jq -e '.meta.exit_code == 2 and .error.code == "CONFIRMATION_REQUIRED" and .data.effect == "would_delete"'
```

## Step 5: When the name and the level disagree

The `danger-level` rule compares each `safe` command's name with its level. Declare `purge`
as `safe` and the audit says:

```bash
$ uv run treaty audit myapp.cli:app --format plain
...
  1. (warning) danger-level [purge]: name suggests a destructive operation but danger_level is safe
     fix: danger_level="destructive" and add dry_run: bool = Flag(default=False, ...)
```

It is a warning, so `--strict` fails on it, and it matters more than most: a `safe`
`purge` registers without complaint, and `todo purge` then deletes the completed items and
exits 0, with no preview and no confirmation.

The rule is a heuristic on the leading word of the command's name, and knows two short
lists of verbs: `delete`, `remove`, `destroy`, `drop`, `purge`, `reset`, `rollback`, and
`wipe` suggest `destructive`; `create`, `update`, `set`, `add`, `apply`, `deploy`, `write`,
`push`, and `start` suggest `mutating`. So it cannot see:

- **A verb it does not know.** The same `safe` command named `clear` passes the rule.
  `clean`, `prune`, `kill`, `cancel`, `revoke`, `send`, `move`, and `import` are common ones
  to check by hand
- **A level that is too low but not `safe`.** A `delete` declared `mutating` passes; it
  loses the confirmation gate and the dry run
- **A name that hides the verb.** `todo done` changes state, and nothing in the word says so

So read the rule's silence as "no obvious mistake", and review each non-`safe` command
against Step 1 yourself. When the name and the level disagree because the name is wrong,
rename the command: an agent guesses from names too.

## Next

[Declare exit codes](exit-codes.md) gives each way a `mutating` or `destructive` command
can fail its own exit code, and clears the audit's `exit-codes` and `retryable` rules.
