# Accept a raw JSON payload

**Goal:** a mutating command with many fields also takes them as one JSON object, checked
against the same types as its flags, so an agent that already holds the data as JSON passes
it without translating every field into a flag

**You need:** a treaty app, such as `todo` at the end of [Declare exit codes](exit-codes.md);
this chapter clears the audit rule `raw-payload`

**Done when:** the audit has no `raw-payload` finding:

<!-- check -->
```bash
uv run treaty audit examples.tutorial.todo_payload:app \
  | jq -e '[.data.rules[].findings[] | select(.rule == "raw-payload")] == []'
```

The chapter gives `todo` an `edit` command that changes an item's text or priority. It starts
from [`examples/tutorial/todo_exit_codes.py`](../../../examples/tutorial/todo_exit_codes.py)
and ends at [`examples/tutorial/todo_payload.py`](../../../examples/tutorial/todo_payload.py).

## Running the checks

Run the **Check** commands from the root of a treaty checkout, in order. `todo` runs this
chapter's example, and the first item is added with flags. The item file goes to a scratch
directory, and so do the idempotency records: when a call carries an `--idempotency-key`,
treaty stores its result under the key so a repeat can return it, and `TODO_STATE_DIR`
says where:

<!-- check -->
```bash
todo() { uv run examples/tutorial/todo_payload.py "$@"; }
rm -rf tmp/tutorial && mkdir -p tmp/tutorial
export TODO_STATE_DIR="$PWD/tmp/tutorial/state"
todo add "Buy milk" --db tmp/tutorial/todo.json > /dev/null
```

Each check exits non-zero when it fails: JSON output goes through `jq -e`, which exits 1 when
the condition is false.

## Why a payload

An agent often has the data before it has the command: a record from an API, a row it
built, the `data` of an earlier call. To pass it as flags, it turns each key into a flag
name and each value into argv text, quoting as it goes. Every step is a chance to get one
wrong: a key with an underscore becomes a flag with a hyphen, a `null` has no flag spelling,
a string with a quote needs escaping. With `--raw-payload`, the agent passes the object as
it is, and treaty checks it against the same field types as the flags.

## Step 1: Find the wide commands

The `raw-payload` rule reports mutating and destructive commands with more than three fields
that are not booleans, counting the ones they inherit. `edit` has four: `id`, `--text`,
`--priority`, and `--db` from the shared base:

```bash
$ uv run treaty audit myapp.cli:app --format plain
...
  1. (advice) raw-payload [edit]: mutating command with many fields; agents pass API-shaped JSON with less translation loss
     fix: supports_raw_payload=True
```

The finding is `advice`, so it does not fail `--strict`. Three fields is a rule of thumb,
not a limit: a command with two fields that mirrors an API's request body is worth the
payload too, and a `safe` command can declare it as well; the rule only asks where the gain
is largest.

## Step 2: Declare it

`supports_raw_payload=True` on the command:

<!-- file: examples/tutorial/todo_payload.py -->
```python
@app.command(
    "edit",
    description="Change an item's text or priority",
    danger_level="mutating",
    exit_codes=["NOT_FOUND", "STORE_CORRUPT", "STORE_UNWRITABLE"],
    examples=[
        ("Raise an item's priority", "todo edit 3 --priority high"),
        (
            "Apply fields from JSON",
            """todo edit --raw-payload '{"id": 3, "text": "Buy oat milk"}'""",
        ),
    ],
    supports_raw_payload=True,
)
```

The handler does not change: it receives the same `Edit` arguments however they arrived.
The second example shows the payload form, since agents copy examples before they read the
schema.

`--schema` now includes `raw_payload_schema`, the JSON Schema of the object: one key per
field, spelled with underscores as in `exec` lines (the JSON form of a call that `todo exec`
reads, one per line) and MCP calls, plus the framework keys the command takes, such as
`idempotency_key`. `id` is required because it has no default:

**Check:** the payload schema requires `id` and lists the fields and the idempotency key

<!-- check -->
```bash
todo edit --schema | jq -e '.data.raw_payload_schema.required == ["id"]
  and (.data.raw_payload_schema.properties | has("text") and has("priority") and has("db")
    and has("idempotency_key"))'
```

## Step 3: Call it with a payload

The object replaces the command's own arguments, positional ones included:

```bash
todo edit --raw-payload '{"id": 1, "text": "Buy oat milk", "priority": "high"}'
```

A payload and the command's own flags do not mix: passing both exits 2, since a field given
twice could disagree. Framework flags are not fields, so `--validate-only`, `--format`, and
the like still go beside the payload.

`edit` treats a missing key as "leave it unchanged": `text` and `priority` default to
`None`, and the handler keeps the old value for `None`. That also means an explicit `null`
in the payload leaves the value as it is, so a field that can be cleared needs its own way
to say so.

**Check:** a payload edits the item; the same call with a flag added is refused; a payload
beside `--validate-only` checks without writing

<!-- check -->
```bash
todo edit --raw-payload '{"id": 1, "text": "Buy oat milk", "priority": "high", "db": "tmp/tutorial/todo.json"}' \
  | jq -e '.data.effect == "updated" and .data.item == {"id": 1, "text": "Buy oat milk",
    "priority": "high", "done": false}'
todo edit 1 --raw-payload '{"text": "x"}' --db tmp/tutorial/todo.json \
  | jq -e '.meta.exit_code == 2 and .error.message == "Cannot combine --raw-payload with individual flags."'
todo edit --raw-payload '{"id": 1, "priority": "low", "db": "tmp/tutorial/todo.json"}' --validate-only \
  | jq -e '.meta.exit_code == 0 and .meta.validation_only'
```

## Step 4: What the payload forgives, and what it does not

Agents write JSON the way people do, so the payload reader accepts the common slips and
parses them to the same value as strict JSON: single quotes, unquoted keys, trailing
commas, and `//` or `/* */` comments. Anything worse exits 2 with `INVALID_JSON`, and when
the reader can tell what was meant, `error.corrected_input` holds the repaired JSON to send
instead.

The values are held to the field types as strictly as flags are, and every problem is
reported at once: a wrong type, a value outside a `Literal`, and a key the command does not
have each get an entry in `error.errors`. A payload is never partly applied.

**Check:** a hand-written payload with unquoted keys and a trailing comma is accepted; a
payload with no punctuation at all is refused with the repaired JSON; two bad values are
both reported, and an unknown key is named

<!-- check -->
```bash
todo edit --raw-payload "{id: 1, text: 'Buy milk', db: 'tmp/tutorial/todo.json',}" \
  | jq -e '.meta.exit_code == 0 and .data.item.text == "Buy milk"'
todo edit --raw-payload '{id 1 priority high}' \
  | jq -e '.meta.exit_code == 2 and .error.code == "INVALID_JSON"
    and .error.corrected_input == "{\"id\": 1, \"priority\": \"high\"}"'
todo edit --raw-payload '{"id": "one", "priority": "urgent"}' \
  | jq -e '[.error.errors[].message] == ["'"'id'"' expects an integer.",
    "'"'priority'"' must be one of low, normal, high."]'
todo edit --raw-payload '{"id": 1, "txt": "x"}' \
  | jq -e '.meta.exit_code == 2 and .error.message == "Unknown field '"'txt'"'."'
```

## Step 5: Framework keys go in the payload too

The keys a command takes as framework flags have payload spellings, the same ones `exec`
lines and MCP calls use: `idempotency_key`, `confirm_destructive`, `dry_run`, and `timeout`
where the command has them. An agent that retries a payload call puts the key in the object:

**Check:** the same payload with the same `idempotency_key`, sent twice, changes the item once

<!-- check -->
```bash
todo edit --raw-payload '{"id": 1, "priority": "normal", "idempotency_key": "edit-1", "db": "tmp/tutorial/todo.json"}' \
  | jq -e '.data.effect == "updated"'
todo edit --raw-payload '{"id": 1, "priority": "normal", "idempotency_key": "edit-1", "db": "tmp/tutorial/todo.json"}' \
  | jq -e '.data.effect == "noop" and .meta.idempotency_hit'
```

## Passing a payload from a file

`--raw-payload` takes the JSON text itself; it does not read a file or stdin. From a shell,
substitute the file: `todo edit --raw-payload "$(cat edit.json)"`. For many calls, or objects
too large for a command line, write one `exec` line per call instead: each line is the same
object with `"_cmd": "edit"` added, and `todo exec --input-file plan.jsonl` runs them all.

## Next

The audit's next rule is `cleanup`, which asks network commands for a hook that runs however
the run ends: [Release what a run holds](cleanup.md).
