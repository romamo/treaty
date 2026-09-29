# Log without touching stdout

**Goal:** a command says what it is doing on stderr, at a level the caller chooses, never
on stdout and never with a secret in it, and every run leaves a record in the audit log

**You need:** a treaty app, such as `todo` at the end of [Read settings and secrets](config.md);
this chapter clears the audit rule `log-not-print`

**Done when:** the audit finds no handler that prints:

<!-- check -->
```bash
uv run treaty audit examples.tutorial.todo_config:app \
  | jq -e '[.data.rules[].findings[] | select(.rule == "log-not-print")] == []'
```

The chapter uses [`examples/tutorial/todo_config.py`](../../../examples/tutorial/todo_config.py),
whose `import` logs each feed it imports.

## Running the checks

Run the **Check** commands from the root of a treaty checkout, in order. `todo` runs the
example; the audit log goes to a scratch directory, and a feed that refuses the connection
stands in for a real one:

<!-- check -->
```bash
todo() { uv run examples/tutorial/todo_config.py "$@"; }
rm -rf tmp/tutorial && mkdir -p tmp/tutorial
export TODO_AUDIT_LOG="$PWD/tmp/tutorial/audit.jsonl"
unset TODO_TOKEN
feed=http://127.0.0.1:9/todo.json
```

Each check exits non-zero when it fails: JSON output goes through `jq -e`, which exits 1 when
the condition is false. `tests/test_tutorial.py` runs the checks the same way; the calls
that need a feed run there too.

## Why not print

Stdout carries the envelope and nothing else: an agent parses it as JSON, and one stray
line in front of it breaks the parse. A `print()` also cannot be turned off, so a caller
that wants a quiet run still pays, in tokens, for every line. The audit reports it:

```bash
$ uv run treaty audit myapp.cli:app --format plain
...
  1. (warning) log-not-print [import]: handler calls print(), which --quiet cannot silence and an agent pays tokens to read; ctx.log is silent off a terminal unless --verbose (REQ-F-038)
     fix: ctx.log(...) for info, ctx.progress(...) for progress, ctx.log_error(...) for errors, ctx.debug(...) for --debug
```

treaty protects stdout even from code it does not own. While a handler runs, `sys.stdout`
points at stderr, so a `print()` from the handler or a library cannot land in front of the
envelope; the text is reported in a `THIRD_PARTY_STDOUT` warning instead, and off a terminal
it is not written anywhere else. A library that prints when it is imported, before the
handler runs, is caught by an entry module that calls `treaty.intercept_stdout()` before
importing the app, as the `entry.py` of `treaty init` does.

## Step 1: Log through `ctx`

`import` logs each feed it imports, with the facts as fields rather than in the message:

<!-- file: examples/tutorial/todo_network.py -->
```python
    ctx.log("imported feed", url=args.url, added=len(added))
```

`ctx` has one method per level:

| Method | For | Shown |
| --- | --- | --- |
| `ctx.log(message, **fields)` | what the command is doing | at a terminal, or with `--verbose` |
| `ctx.progress(message, done=, total=)` | how far along it is | the same |
| `ctx.debug(message, **fields)` | detail for whoever is debugging | with `--debug` |
| `ctx.log_error(message, **fields)` | an error the run survives | always, unless `--quiet` |
| `ctx.warn(code, message, **context)` | something the caller should know | in the envelope's `warnings`, not on stderr |

Off a terminal each line is one JSON object with `level`, `message`, and `fields`, so an
agent that asks for `--verbose` can parse its own stderr too; at a terminal it is
`message key=value`. Under `--debug`, records of Python's `logging` module, such as
`urllib3`'s, are written too, through the same redaction.

## Step 2: Let the caller pick the level

What reaches stderr depends on who is running the command:

| The run | On stderr |
| --- | --- |
| off a terminal, or under `CI` | errors and warnings only |
| at a terminal | also `ctx.log` and progress |
| `--verbose` | info and progress anywhere, even under `CI` |
| `--debug` | also `ctx.debug`, and treaty's own trace: settings resolved, the command started, each `ctx.http` request, each child process |
| `--quiet` | nothing at all; the envelope still carries every error |

The three flags exclude each other. So an agent gets a quiet stderr by default, turns on
`--verbose` to watch a long run, and `--debug` to find out why something failed.

**Check:** off a terminal, a failed `import` writes nothing on stderr; `--debug` traces the
run, the refused request included; `--quiet` writes nothing; two levels at once is an
argument error

<!-- check -->
```bash
todo import --url "$feed" --db tmp/tutorial/todo.json > /dev/null 2> tmp/tutorial/default.err || true
test ! -s tmp/tutorial/default.err
todo import --url "$feed" --db tmp/tutorial/todo.json --debug > /dev/null 2> tmp/tutorial/debug.err || true
grep -q '"message":"command started"' tmp/tutorial/debug.err
grep '"message":"http request"' tmp/tutorial/debug.err | jq -e '.fields.error == "CONNECTION_FAILED"'
todo import --url "$feed" --db tmp/tutorial/todo.json --quiet > /dev/null 2> tmp/tutorial/quiet.err || true
test ! -s tmp/tutorial/quiet.err
todo import --url "$feed" --db tmp/tutorial/todo.json --verbose --debug \
  | jq -e '.meta.exit_code == 2 and .error.message == "--debug and --verbose are exclusive; pass one."'
```

The `--debug` trace has one line per `ctx.http` request, with its method, URL, headers, and
either the `status` it was answered with or, for a request that never got an answer such as
this refused connection, the `error` it failed with.

## Step 3: Keep secrets out of the logs

Every log line goes through the same redaction as the envelope: a declared secret, and any
field named like a credential (`token`, `password`, `api_key`, `Authorization`, `Cookie`, at
any depth), is written as `[REDACTED]`. The `--debug` trace of `import` with a token shows
the header as `"Authorization": "[REDACTED]"`, and the token itself appears nowhere on
stdout or stderr; the tests check exactly that.

Redaction works on names and declared secrets. A secret you put in a message string, as in
`ctx.log(f"using {token}")`, is text like any other: pass values as fields, and name them
for what they are.

**Check:** the logging tests pass: `import` logs only under `--verbose`, a stray `print`
never reaches stdout, and the `--debug` trace redacts the token

<!-- check -->
```bash
uv run pytest -q tests/test_tutorial.py -k "logs_what or stray_print or token_redacted"
```

## Step 4: Read the audit log

Every run, successful or not, adds one line to the audit log: the command, its parsed
parameters with secrets redacted, the exit code, the duration, the request and trace ids,
and `data` when it is small. It is how you find out, after the fact, what an agent ran.

The file is `$XDG_DATA_HOME/todo/audit.jsonl`, else `~/.local/share/todo/audit.jsonl`;
`TODO_AUDIT_LOG` names another file, and `TODO_AUDIT_LOG=off` turns it off for a run. It
rotates at 100 MiB and drops files older than 30 days. `meta.audit_log_path` names it on
every response, and the `audit-log` built-in reads it back, filtered by `--since`,
`--command`, or `--trace-id`, one envelope per entry.

**Check:** an `add` run is in the audit log under its request id; the refused `import` runs
are there too, exit 12 among them; the log names no token

<!-- check -->
```bash
request=$(todo add "Buy milk" --db tmp/tutorial/todo.json | jq -r .meta.request_id)
todo audit-log --since 1h | jq -se --arg r "$request" '[.[] | .data | select(. != null)]
  | (map(select(.request_id == $r)) | .[0].command == "add")
  and (map(select(.command == "import")) | any(.exit_code == 12))'
test "$(grep -c s3cret tmp/tutorial/audit.jsonl)" -eq 0
```

## Next

The audit's last rule, `profile`, asks for a conformance profile, which
[Run the conformance kit](../ship/conformance.md) writes and runs.
