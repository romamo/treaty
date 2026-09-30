# Serve commands over MCP

**Goal:** the same commands, with the same contract, as MCP tools for clients that have no
shell

**You need:** a treaty app, ideally one that passes [the conformance kit](conformance.md)

**Done when:** your MCP client lists one tool per command and a call returns the response
envelope. In this repository, the client session in the tests proves it:

```bash
uv run pytest tests/test_tutorial.py -k test_todo_over_mcp
# 1 passed, the rest deselected
```

## Why serve both

An agent with a shell calls `todo` directly and reads the manifest. Many agents have no
shell: desktop and IDE assistants, hosted agents, anything that only speaks MCP. For those,
`treaty-mcp` serves the same app as MCP tools over stdio.

There is nothing to write. The tools come from the registry the CLI already has, and every
call goes through `App.call`, the path `exec` lines take. Validation, confirmation of
destructive commands, idempotency keys, timeouts, declared exit codes, and output caps all
work as they do on the command line, so there is no second implementation to drift.

## Step 1: Install the extra

```bash
uv add "treaty[mcp]"
```

**Check:** with no arguments, `treaty-mcp` prints its usage and exits 2

<!-- check -->
```bash
uv run treaty-mcp 2>&1 | grep -q '^usage: treaty-mcp module:app'
```

## Step 2: Look at the tools you get

For `todo`, a client sees twelve tools: the four commands `todo` registers, and eight of
treaty's built-ins:

| Command | Tool | Hints |
| --- | --- | --- |
| `todo add` | `add` | |
| `todo done` | `done` | |
| `todo list` | `list` | read-only, idempotent |
| `todo purge` | `purge` | destructive |
| `todo manifest` | `manifest` | read-only, idempotent |
| `todo version` | `version` | read-only, idempotent |
| `todo doctor`, `todo status`, `todo audit-log`, `todo mcp-validate` | the same names | read-only, idempotent |
| `todo cleanup` | `cleanup` | destructive |
| `todo generate-skills` | `generate-skills` | |

`treaty-mcp --list-tools` prints the tool list a client would get, without starting a
session.

**Check:** the list has the twelve tools, with `list` read-only and `purge` destructive

<!-- check -->
```bash
uv run treaty-mcp examples.tutorial.todo_exit_codes:app --list-tools | jq -e '
  ([.tools[].name] | sort) == ["add", "audit-log", "cleanup", "doctor", "done",
    "generate-skills", "list", "manifest", "mcp-validate", "purge", "status", "version"]
  and (.tools[] | select(.name == "list") | .annotations.readOnlyHint)
  and (.tools[] | select(.name == "purge") | .annotations.destructiveHint)'
```

- **Names** are command paths with dots as underscores: a `deploy.rollback` command is the
  `deploy_rollback` tool. `exec` is not served, since a client batches by making several
  calls, and neither is `completion`, which only a shell can use
- **Hints** come from the danger level (`safe` is read-only and idempotent, `destructive` is
  destructive) and from `has_network_io`, which sets the open-world hint
- **Descriptions** get the danger level spelled out: `purge`'s tells the model that without
  `confirm_destructive=true` the call is a dry run, and mutating tools point at
  `idempotency_key`
- **The input schema** is the arguments dataclass, plus the framework keys the command
  takes: `idempotency_key` on mutating commands, `confirm_destructive` on destructive ones,
  `timeout` where `--timeout` exists, and `fields`, `stable_output`, and `validate_only` on
  every command. `add` takes `text` (required), `priority` (an enum of `low`, `normal`,
  `high`), and `db`, plus `idempotency_key` and the three every command has
- **The output schema** is the response envelope with the command's `output_schema` as
  `data`

## Step 3: Register the server with a client

The server needs to import your app, so start it from the project directory. `uv run
--directory` does that wherever the client launches it from. In Claude Code:

```bash
claude mcp add todo -- uv run --directory /abs/path/to/project treaty-mcp todo.cli:app
```

Clients that read a JSON config (a project's `.mcp.json`, Claude Desktop) take the same
command:

```json
{
  "mcpServers": {
    "todo": {
      "command": "uv",
      "args": ["run", "--directory", "/abs/path/to/project", "treaty-mcp", "todo.cli:app"]
    }
  }
}
```

In this repository, the target is `examples.tutorial.todo_exit_codes:app`.

**Check:** the client lists the twelve tools, and calling `version` returns
`{"ok": true, "data": {"name": "todo", "version": "1.0.0"}, ...}`

## Step 4: What a caller sees

Every result carries the envelope twice: as `structuredContent`, and as JSON text for
clients that ignore structured content. `isError` is `true` exactly when `ok` is false. What
was a flag becomes a key:

| On the command line | In a tool call |
| --- | --- |
| `--priority high` | `"priority": "high"` |
| `--confirm-destructive` | `"confirm_destructive": true` |
| `--idempotency-key k1` | `"idempotency_key": "k1"` |
| `--token-from-env VAR` | `"token_from_env": "VAR"`, read from the server's environment |
| the exit code | `meta.exit_code` in the envelope |

A destructive call without confirmation is refused with its preview, as on the CLI:

```json
{"ok": false,
 "data": {"effect": "would_delete", "deleted": [{"id": 1, "text": "Buy milk", "priority": "high", "done": true}],
          "would_affect": {"summary": "Deletes 1 completed items", "resources": ["item/1"], "count": 1}},
 "error": {"code": "CONFIRMATION_REQUIRED",
           "message": "Command purge is destructive and was not applied; it would: Deletes 1 completed items.",
           "fix_required": "rerun with --confirm-destructive to apply (confirm_destructive: true in exec, MCP, or --raw-payload)", ...},
 "meta": {"exit_code": 2, "_cmd": "purge", ...}}
```

and a repeated `add` with the same `idempotency_key` returns the first item with
`"effect": "noop"` and `meta.idempotency_hit: true` instead of adding a second one. The exit
codes from [Declare exit codes](../core/exit-codes.md) arrive the same way: `done` on a
missing item returns `error.code` `NOT_FOUND` and `meta.exit_code` 5.

## Step 5: Mind where the server runs

The server is a separate process that the client starts, which catches CLIs that assume
they run in the caller's shell:

- **Relative paths resolve against the server's working directory**, not the agent's. A call
  with `"db": "todo.json"` writes `todo.json` in the directory `--directory` named. Ask for
  absolute paths in the field's description when it matters
- **The environment is the client's, not your shell's.** A client may start the server with
  a reduced environment; the MCP Python SDK's client passes only a few variables, such as
  `HOME` and `PATH`. Secret variables read through `<name>_from_env`, and settings such as
  `TODO_STATE_DIR` or `TODO_MAX_OUTPUT_BYTES` (the app name, uppercased, is the prefix), have
  to be set in the client config: `claude mcp add -e NAME=value`, or an `"env"` object in the
  JSON
- **A `print()` goes nowhere useful.** Over stdio, stdout carries the protocol, so treaty
  points `sys.stdout` at stderr for the whole server process: a stray `print()` from a
  handler or a library cannot corrupt the protocol, but it lands on the server's stderr and
  never reaches the model. Log through `ctx.log`, as
  [Log without touching stdout](../core/logging.md) describes, and put what the caller needs
  in the result
- **Messages name CLI commands.** `todo`'s `NOT_FOUND` suggestion says `todo list --all`;
  over MCP the model has to turn that into a `list` call with `all: true`. treaty's own
  `fix_required` texts name both forms, as the `CONFIRMATION_REQUIRED` example shows; do the
  same where a message tells the caller what to run

## Step 6: Test through a real client

`app.call()` covers the handler path, but only a client session covers the server: the
tool list, the schemas, and the transport. The MCP SDK ships a client, so a test can start
the server the way a client does and make calls:

<!-- file: tests/test_tutorial.py -->
```python
    server = shutil.which("treaty-mcp", path=str(Path(sys.executable).parent))
    assert server is not None, "treaty-mcp is not installed in this environment"
    params = StdioServerParameters(
        command=server,
        args=["examples.tutorial.todo_exit_codes:app"],
        cwd=str(tmp_path),
        env={
            "PYTHONPATH": str(ROOT),
            "TODO_STATE_DIR": str(tmp_path / "state"),
            "TODO_AUDIT_LOG": "0",
        },
    )
```

It starts the `treaty-mcp` command of the test's own environment, as a client would. The
environment keeps the idempotency records in the test's own directory and turns the audit
log off, so neither lands in your real ones, and `PYTHONPATH` lets the server import the
example app from this repository; in your project, where the app is installed, it is not
needed. The rest of [`test_todo_over_mcp`](../../../tests/test_tutorial.py) lists the tools,
replays an idempotency key, previews and confirms `purge`, and checks where a relative path
lands.

In your project, copy `test_todo_over_mcp` into `tests/`. It needs only these imports
(the others at the top of `test_tutorial.py` are for this repository):

```python
import asyncio
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
```

Change the app path to `todo.cli:app` and drop `PYTHONPATH`, and set the expected tool list
to your own: `uv run treaty-mcp todo.cli:app --list-tools` prints it. Every call already
passes `db` under the test's `tmp_path`, so the test never touches your real item file; keep
it that way for your own commands. The test needs the `mcp` extra, which Step 1 added.

**Check:** the chapter's **Done when** command

## Next

`todo` passes the audit and the conformance kit, and serves the same contract to agents
with a shell and without one. The next step is the docs those agents read before they call:
[Ship the agent docs](agent-docs.md).
