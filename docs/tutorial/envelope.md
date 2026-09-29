# The response envelope

Every run of a treaty command writes one JSON object on stdout, the envelope, whatever
happened: a result, a refusal, an argument error, a crash. This page takes real envelopes
from `todo` apart key by key, then defines the terms the chapters use. Read it once before
the chapters, and come back to it when a key is unfamiliar.

Off a terminal the envelope is JSON; at a terminal the same run prints readable text, and
`--format json` asks for the JSON anyway. Everything below is the JSON.

## Running the checks

The checks on this page run `todo` from
[`examples/tutorial/todo_exit_codes.py`](../../examples/tutorial/todo_exit_codes.py). Run
them from the root of a treaty checkout, in order:

<!-- check -->
```bash
todo() { uv run examples/tutorial/todo_exit_codes.py "$@"; }
rm -rf tmp/tutorial && mkdir -p tmp/tutorial
```

## A result

`todo add "Buy milk" --priority high` answers:

```json
{
  "data": {
    "effect": "created",
    "item": {"done": false, "id": 1, "priority": "high", "text": "Buy milk"}
  },
  "error": null,
  "meta": {
    "audit_log_path": "/home/me/.local/share/todo/audit.jsonl",
    "command": "add",
    "config_sources": [],
    "cwd": "/home/me/project",
    "duration_ms": 0,
    "effective_config_hash": "44136fa355b3",
    "exit_code": 0,
    "headless": true,
    "request_id": "4dfe36f0ac6e",
    "schema_version": "1.0",
    "timeout_ms": 60000,
    "timestamp": "2026-09-28T18:50:51.722Z",
    "tool_version": "1.0.0"
  },
  "ok": true,
  "warnings": []
}
```

Five keys are always there:

| Key | Holds |
| --- | --- |
| `ok` | `true` when the run did what was asked, `false` otherwise. Read it first |
| `data` | the result, shaped as the command's `output_schema` says; `null` when there is none |
| `error` | `null` on success; on failure, what went wrong and what to do about it |
| `warnings` | things the caller should know that did not stop the run; `[]` when there are none |
| `meta` | facts about this run rather than its result: the exit code, timing, ids |

`data` and `meta` are kept apart on purpose. Two identical calls on the same state return
byte-identical `data`, so it can be cached, hashed, and diffed; everything that changes from
call to call (the request id, the timestamp, the duration) lives in `meta`. `meta.exit_code`
is always the process's exit code, so a caller that reads only stdout still knows it.

`effect` is not part of the envelope: it is a field of `add`'s own result, which every
mutating and destructive command must have. It says what the call changed: `created`,
`updated`, `deleted`, or `noop`, or a `would_` form such as `would_delete` on a dry run.

**Check:** the five keys, `ok` with a null `error`, and an exit code in `meta` that matches the
process's

<!-- check -->
```bash
todo add "Buy milk" --priority high --db tmp/tutorial/todo.json > tmp/tutorial/add.json
jq -e 'keys == ["data", "error", "meta", "ok", "warnings"] and .ok and .error == null
  and .meta.exit_code == 0 and .data.effect == "created"' tmp/tutorial/add.json
```

## A failure

`todo done 9`, for an item that does not exist:

```json
{
  "data": null,
  "error": {
    "code": "NOT_FOUND",
    "context": {"id": 9},
    "message": "No item #9.",
    "phase": "execution",
    "retryable": false,
    "suggestion": "todo list --all shows every item number"
  },
  "meta": {"command": "done", "exit_code": 5, "...": "..."},
  "ok": false,
  "warnings": []
}
```

An agent acts on `error` in this order:

| Key | Holds | The agent |
| --- | --- | --- |
| `code` | the name of the failure, from the command's declared exit codes | branches on it; it is stable across releases, where the message is not |
| `retryable` | whether the same call may simply be sent again | retries only when `true`, after `retry_after_ms` when it is there |
| `fix_required` | the condition to meet before sending the call again | meets it, or stops |
| `fix_command` | one command that meets it, runnable as is and never destructive | runs it, then retries once |
| `suggestion` | the next step, in words written for an agent | follows it when nothing above applies |
| `context` | the facts, as values a program can read, such as `{"id": 9}` | uses them instead of parsing `message` |
| `message` | one line for a person or a log | shows it; never parses it |
| `phase` | `validation` when nothing ran, `execution` when the handler had started | after `execution`, checks state before acting |

Only `code`, `message`, and `retryable` are always there; the rest appear when they apply,
never as `null`. treaty also tidies `message`: the handler raised `no item #9`, and the
envelope says `No item #9.`

Some failures carry keys of their own: `network_context` for a failed network call,
`corrected_input` for JSON that could be repaired, `conflict_id` for a create that found
the resource already there, `retries_exhausted` after the command's own retries. The
chapter that raises each one explains it.

**Check:** a failure has `ok: false`, `data: null`, and the declared code, with the exit code
`done --schema` lists for it

<!-- check -->
```bash
todo done 9 --db tmp/tutorial/todo.json > tmp/tutorial/done.json || true
jq -e '.ok == false and .data == null and .error.code == "NOT_FOUND" and .meta.exit_code == 5
  and .error.retryable == false and .error.phase == "execution"' tmp/tutorial/done.json
todo done --schema | jq -e '.data.exit_codes["5"].name == "NOT_FOUND"'
```

## An argument error

A bad argument fails before anything runs, always with exit 2 and `phase: validation`, and
lists every bad argument at once in `errors`, each with its own `field`, `message`, and
`context`:

```json
{
  "code": "ARG_ERROR",
  "context": {"allowed": ["low", "normal", "high"], "flag": "priority", "value": "urgent"},
  "errors": [
    {"field": "priority", "message": "'priority' must be one of low, normal, high.", "context": {"...": "..."}}
  ],
  "fix_required": "correct the arguments and reissue",
  "message": "'priority' must be one of low, normal, high.",
  "phase": "validation",
  "retryable": false,
  "suggestion": "correct the arguments and reissue"
}
```

Exit 2 always means this: the call was refused as written and nothing happened, so the
agent fixes every entry in `errors` and sends it once more.

**Check:** exit 2, `phase: validation`, and the bad field named in `errors`

<!-- check -->
```bash
todo add "x" --priority urgent --db tmp/tutorial/todo.json \
  | jq -e '.meta.exit_code == 2 and .error.phase == "validation"
    and [.error.errors[].field] == ["priority"] and (.error.context.allowed | length) == 3'
```

## A warning

A warning is something the caller should know that did not stop the run. It has a `code`, a
`message`, and sometimes a `context`. Passing `--no-injection-protection` produces one. That
flag turns off the trust markers: content a command got from outside the tool, such as a
web page or an API response, normally carries `"_trusted": false`, so an agent reads it as
data and never as instructions. A run without that protection says so:

```json
"warnings": [
  {
    "code": "INJECTION_PROTECTION_DISABLED",
    "message": "--no-injection-protection was active; external data is returned without trust markers"
  }
]
```

The run still succeeds, with `ok: true` and its `data`. A caller that must not proceed past
any warning passes `--warnings-as-errors`: the same run then exits 1 with
`WARNINGS_AS_ERRORS`, and keeps both the warnings and the `data`, since the work was done.

**Check:** a warning leaves the run successful; `--warnings-as-errors` fails it and keeps the
data

<!-- check -->
```bash
todo list --db tmp/tutorial/todo.json --no-injection-protection 2> /dev/null \
  | jq -e '.ok and [.warnings[].code] == ["INJECTION_PROTECTION_DISABLED"]'
todo list --db tmp/tutorial/todo.json --no-injection-protection --warnings-as-errors 2> /dev/null \
  | jq -e '.ok == false and .meta.exit_code == 1 and .error.code == "WARNINGS_AS_ERRORS"
    and (.data | length) == 1'
```

## What is in `meta`

These are on every run:

| Key | Holds |
| --- | --- |
| `exit_code` | the process's exit code |
| `command` | the command as the manifest names it, such as `add` or `deploy.rollback` |
| `request_id` | an id for this run, also in the audit log |
| `timestamp`, `duration_ms` | when the run started, in UTC, and how long it took |
| `tool_version` | the app's version, as `--version` reports it |
| `schema_version` | the `MAJOR.MINOR` version of this command's output contract |
| `cwd` | the working directory the run used, `--cwd` included |
| `timeout_ms` | the time limit the run had |
| `headless` | `true` when there is no person or display to open a window for |
| `config_sources`, `effective_config_hash` | the config files read, highest first, and a hash of the settings they produced |
| `audit_log_path` | the audit log this run was written to; absent when `TODO_AUDIT_LOG=off` turns the log off |

Others appear only when they apply: `pagination` on a list command (`returned`, `total`,
`has_more`, `next_cursor`, `truncated`), `idempotency_hit` when a repeated idempotency key
returned the first result, `validation_only` under `--validate-only`, `trace_id` when
`TOOL_TRACE_ID` is set, `retries` when the command retried. None of them is ever `null`: a
key that does not apply is left out.

**Check:** `list` carries `pagination`; a run without an idempotency key has no
`idempotency_hit` at all

<!-- check -->
```bash
todo list --db tmp/tutorial/todo.json \
  | jq -e '.meta.pagination.returned == 1 and (.meta | has("idempotency_hit") | not)'
```

## Terms the chapters use

- **Manifest**: the whole contract in one JSON document, from `todo manifest`: every
  command, flag, exit code, and output schema. An agent reads it instead of `--help`
- **Schema**: `todo <command> --schema` is one command's part of the manifest, with its
  `output_schema` (the shape of `data`) and, when it takes one, `raw_payload_schema`
- **Danger level**: `safe`, `mutating`, or `destructive`, declared on every command; it
  decides which guards the command gets. See
  [Choose each command's danger level](core/danger-level.md)
- **Effect**: the field of a mutating or destructive result that says what changed
- **Exit code**: 0 is success, 2 an argument error, 1 an unexpected failure, and the other
  framework codes up to 13 have fixed meanings; an app declares its own from 79 to 125.
  A run ended by a signal exits 130 or 143, and one whose reader closed stdout exits 141,
  so every command's `--schema` lists those three too. See
  [Declare exit codes](core/exit-codes.md)
- **Retryable**: an exit code's promise that the identical call may be sent again; only
  codes that changed nothing can make it
- **Idempotency key**: `--idempotency-key` on a mutating command; a repeat with the same key
  returns the first result instead of running again
- **Dry run and confirmation**: a destructive command previews with `--dry-run`, and applies
  only with `--confirm-destructive`; without it, it previews and exits 2
- **Resource**: a class a handler asks for by annotating a parameter with it, built once per
  run by its `acquire` classmethod and given back by `release`
- **`Ctx`**: the handler's second parameter, the run's context: `ctx.log`, `ctx.http`,
  `ctx.timeout`, `ctx.cwd`, and the rest of what treaty offers a handler
- **Audit findings**: `treaty audit` reports `error`, `warning`, and `advice`; the first two
  fail `--strict`. See [the index](index.md#advice-you-can-leave)

## Next

Back to [Pick a track](index.md#pick-a-track) to start on your CLI.
