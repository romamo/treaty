# Handoff

Status as of 2026-09-27. Read this before touching the code.

## What this is

`treaty` is a zero-dependency Python CLI framework that implements the
[CLI Agent Spec](../cli-agent-ergonomics). The spec repo is the source of truth for
every schema and requirement cited here; this repo is one implementation of it.
`agentyper` (sibling directory) is a separate, older library on pydantic and rich.
The two do not share code.

## State

| Check | Result |
|-------|--------|
| `uv run pytest` | 903 passed |
| `uv run mypy src` (strict) | clean |
| `uv run ruff check src tests examples` | clean |
| Spec conformance kit against `examples/deployctl.py` | 12 of 12, levels 1 to 3 |
| Git | `main`, pushed to `origin` (github.com/romamo/treaty) |
| PyPI | `treaty` is free; nothing published |

## Decisions already made

- **Zero runtime dependencies in core.** The only extra is `[mcp]`; `[rich]` and `[pydantic]`
  adapters are postponed and not declared yet (see `ROADMAP.md`)
- **Python 3.14 only.** Handlers rely on native deferred annotations; modules that declare
  args dataclasses inside functions must not use `from __future__ import annotations`
- **Args are frozen slotted dataclasses** with `Arg(...)` and `Flag(...)` markers; handlers
  are plain functions `def h(args: ArgsDC, ctx: Ctx) -> OutputType`, not methods
- **One flat registry keyed by dot-path.** `app.group("deploy")` is a prefix helper, not a
  sub-app; group metadata is never inherited
- **`pathlib.Path` is the only way to declare a path.** It is a `string` flag with
  `pattern_type: filepath`; `check_path` runs on argv, `exec`, and `--raw-payload` values
  and refuses `..`, `%XX`, and null bytes. `../x` from a subdirectory is therefore refused
  too; the suggestion carries the resolved absolute path, which is the intended recovery
- **A secret field has no direct flag.** `secret=True`, or a name containing token, secret,
  password, key, credential, or auth (never on a boolean), turns `--x` into
  `--x-from-env VAR` and `--x-from-file PATH` with `<APP>_<X>` as the default variable.
  `Command.secret_env_vars` maps field name to that variable; `_apply_secrets` resolves them
  in phase 1 on both parse routes and feeds the text through `FieldInfo.parse`
- **Positional arguments are also flags.** Every field appears in the manifest `flags` map
  and can be passed as `--name=value`, because `FlagEntry` has no positional marker
- **In-house parser.** `argparse` is not used anywhere; every parse failure is a
  `ParseError` with structured context and becomes exit `2`
- **Exit 2 only before user code (D2).** The args `__post_init__` is phase 1: `_finish`
  calls it whenever every field has a value and adds its `ParseError` (or the entries of a
  `ParseError.combine`, or an `InvalidValue` from a value object) to the collected errors;
  any other exception is `ArgsCrashed`, which every parse site reports as `HANDLER_CRASHED`. A `ParseError` or `Exit.ARG_ERROR` from a
  handler or `acquire` is `VALIDATION_AFTER_START`, exit 1, `phase: execution`
  (`_Run.after_start`). F-015's "execute hook registered before validate hooks" cannot
  happen: the framework owns the order and there are no hooks to register
- **Text arguments are single-line.** `FieldInfo.check_text` refuses `\n`, `\r`, and NUL
  in `str` values on both parse routes (REQ-F-044), except secrets; `Flag(multiline=True)`
  allows line breaks. `FlagEntry` admits no extra keys, so the manifest states the opt-out
  in the flag's description instead of a `multiline` field
- **Unconfirmed destructive commands exit 2**, running the handler in dry-run mode and
  returning the preview as `data` with error code `CONFIRMATION_REQUIRED` (REQ-O-021),
  whose message quotes `would_affect.summary`
- **Declarations are required (D3).** `App.command` raises `RegistrationError` when
  `exit_codes=` or `danger_level=` is left out (an `_Unset` sentinel, so the error names the
  command and the fix instead of a `TypeError`). The built-ins declare `safe` and `()`
- **Destructive outputs carry `would_affect`.** Registration requires the field on the
  output type (dict outputs are checked per run); `effect_problem` fails a destructive dry
  run whose `would_affect.summary` is missing. Mutating dry runs (from `exec --dry-run`)
  need no `would_affect`
- **`safe_default=True` is decided in `_Run.execute`**, before the idempotency path: no
  `--live`, or `--dry-run`, turns `dry_run` on, so the rest of the pipeline sees an
  ordinary dry run; `--live` alone sets `confirmed`, since it is the explicit confirmation
  (O-048), while `requires_confirmation` stays true in `--schema`. `--dry-run` wins over `--live` because the kit previews a destructive
  probe by appending `--dry-run` to its argv. `meta.dry_run` is added to every envelope of
  the command, argument errors included; `meta.confirmed` only to an applied live run
- **Timeouts use a daemon thread**, not `SIGALRM`, so they work on Windows, off the main
  thread, and inside blocking C calls. A timed-out handler is abandoned, not killed
- **`--format` is `json` or `plain`**, named for the representation, not the reader
  (REQ-O-001). `human` was removed after 0.0.3 with no alias
- **Commands may set `plain=`**, a renderer for plain mode; JSON mode ignores it. It
  receives `data` as JSON values, after secret redaction, not the handler's return value.
  It also renders `data` on failed runs, so a `CliExit` must carry `data` of the handler's
  return type (the error line goes to stderr). Without `plain=`, `_plain.py` prints flat
  `key: value` lines; `manifest` and `--schema` stay indented JSON
- **`treaty audit --strict` fails on warnings and errors, never advice.** It raises
  `AUDIT_FAILED` (79) with the full report as `data`; without `--strict` the audit always
  exits 0 on a completed run
- **`error.errors` is always present on a validation error.** A single failure lists
  itself, so agents read one shape. An unknown flag followed by a bare token consumes that
  token as its value rather than reporting it as an unexpected positional
- **Custom scalars are an app-level registry, not a protocol on the domain class.**
  `app.scalar(cls, parse=..., base=..., pattern=..., minimum=..., serialize=...)` records a
  `ScalarSpec`; `classify`, `schema_for`, and `to_jsonable` all take the registry, so the
  domain module never imports treaty. A registered class is a `string`, `integer`, or
  `number` flag; `apply_scalar` in `_flags.py` runs the declared constraint and then the
  parser on both input routes. Registration happens before the commands that use the class:
  an unregistered annotation is a `SchemaError` at command registration, and an
  unregistered dataclass in an output is still an ordinary nested object
- **Resources are handler parameters, not a pre-dispatch hook.** `_resources.py`:
  parameters after `ctx` name classes with a classmethod `acquire(cls, args, ctx, *deps)`;
  `dependency_params` validates the signature shape for handlers and `acquire` alike,
  `resource_graph` walks the closure at registration (missing `acquire`, cycles), and
  `Resolver` acquires each class once per run inside `_invoke`, which runs under the
  timeout so a `CliExit` or `ParseError` from `acquire` takes the normal envelope path. A
  wrong return type from `acquire` is a `TypeError`, reported as `HANDLER_CRASHED`
- **Streams are envelope lines, not bare items.** REQ-O-004 shows bare items plus a
  summary line; treaty writes one full envelope per yield with `meta.seq`, then a
  terminal envelope (`end`, `total`), because `exec` already speaks envelope lines and a
  mid-stream failure needs an `error`. `_Run.stream` is a generator; `drain()` throws a
  signal that lands between events back into it so the CANCELLED envelope is produced in
  one place; a timeout is an idle limit, the wait for each `next()` on a worker thread
  (REQ-F-011), and a whole-stream deadline only when buffered (`whole=True`: `--no-stream`,
  `App.call`). Streams inherit the app default like any command. The handler generator is
  closed when no worker holds it; on a signal the worker gets `GRACE_SECONDS` to hand it
  back first, so its `finally` runs. Streaming commands must be `safe`; `--no-stream`
  folds the stream into one envelope via `buffer_stream`
- **MCP is a separate console script, not a `treaty` subcommand.** A stdio MCP server
  owns stdout, and every `treaty` command ends by writing an envelope there, so
  `treaty-mcp module:app` in `_mcp.py` bypasses the App runner. Only `build_server`,
  `serve`, and `main` import the `mcp` package (the `treaty[mcp]` extra, SDK 2.x with
  snake_case constructors); tool construction and `call_tool` are plain data over
  `App.call`. `App.call` is the in-process dispatch API: `build_from_mapping` then
  `execute` or a buffered `stream`, writing nothing. An unknown tool name is
  `UNKNOWN_TOOL` with the tool names, distinct from `App.call`'s `UNKNOWN_COMMAND`
- **Every exit writes an envelope.** A handler exception becomes `HANDLER_CRASHED`
  (exit 1) with the redacted traceback on stderr. Broad `except Exception` exists only
  where user code runs: the handler, a scalar's `parse=`/`serialize=`, `cleanup=`,
  `plain=`, `cursor_check=`, an args `__post_init__`, and an exception's `__str__`.
  Nowhere else
- **Exit codes must be declared.** A handler raises only what its manifest entry lists:
  `exit_codes=`, plus `_manifest.implicit_exit_codes`: `GENERAL_ERROR`, `ARG_ERROR`,
  `TIMEOUT`, and `PRECONDITION` (a stray `input()`) everywhere, `CONFLICT` on mutating
  commands, 7 and 8 on gated ones. Anything else is `UNDECLARED_EXIT_CODE`
- **Stdout hygiene is a process-wide swap.** `App.run` points `sys.stdout` at stderr for
  the whole run (handlers run on worker threads, so a context-local redirect would miss
  them) and restores it after; `_Run._write` adds a `THIRD_PARTY_STDOUT` warning to the
  next envelope. A module-level count in `guard_streams` lets overlapping runs on threads
  restore the original streams, the last one out. Only `App.main()` also redirects
  descriptor 1 to stderr and writes envelopes to a saved copy of it, so children and C
  code cannot leak either. `App.call` (MCP) has no swap, because MCP calls run
  concurrently on threads; `_mcp.serve` swaps both streams once for the whole process.
  `App.main()`, not `App.run()`, writes `PAGER`, `GIT_PAGER`, and `NO_COLOR` into
  `os.environ` so children inherit them; a stream closed at startup (None) is tolerated
- **Every written envelope is cleaned** (ANSI escapes removed, null bytes and lone
  surrogates to U+FFFD; keys and `\r` kept), so JSON stdout and MCP structured content
  agree; `cap_envelope` cleans `data` once before its byte loop and `to_json` cleans the
  rest. Plain mode never passes through it. `ErrorDetail.__post_init__` turns `message` into a
  sentence and fills `suggestion` for recoverable errors, which covers author messages and
  every framework path at once instead of a style rule on each message; framework messages
  that began with a command path now begin with `Command <path>` so capitalizing keeps
  the path intact
- **Signals interrupt only a running handler.** `Cancellation.armed()` windows cover the
  handler, the wait for its worker, the idempotency-key wait, and the exec stdin read; a
  signal elsewhere is held and raised at the next window, so a finished result is still
  written. An exec plan stops at the first signal whatever `--ignore-errors` says

- **Children run from argument lists, in their own session.** `_subprocess.Processes` is
  built per handler run in `_Run._ctx` with the hardened env (`_mode.child_settings`), the
  command's deadline, and the headless flag; `_Run.processes` points at the current one so
  `_cancelled` and both `TimeoutExpired` paths call `terminate()` (SIGTERM to each process
  group, exited leader or not, SIGKILL after 2 s) before the envelope is built; after it
  `_spawn` refuses new children, so an abandoned handler starts none. A failing child is
  a `CliExit` on `GENERAL_ERROR` with code `SUBPROCESS_FAILED`, so no new exit code and no
  declaration; its deadline expiry is a `CliExit` on the implicit `TIMEOUT`, and an
  explicit `timeout=` is capped by the deadline. `_exit_envelope` runs the redactor over
  every `CliExit` message and context string. Stage stderr goes to anonymous temp files,
  so a pipeline needs no reader threads. Windows stops only the child, not grandchildren
- **Registration scans handler source.** `_scan.ctx_calls` parses the handler and lists
  calls on its second parameter; `build_command` refuses a shell string in
  `ctx.run`/`ctx.pipeline` (REQ-F-062 asks for registration time) and an undeclared
  `ctx.open_url`. Handlers without source are skipped; the same checks run at call time
- **Headless is decided once per run** (`_mode.is_headless`) and adds `meta.headless:
  true` in `_Run._envelope`, so every envelope of the run carries it. A headless
  `ctx.open_url` records the URL and `_execute` fills `data.open_url` when the handler
  left it `None`
- **Prompts run through `ctx`, and exit 4 when no one can answer.** `_prompt.Prompter` is
  built per run in `_Run._ctx`; `InputRequired` is a `BaseException`, caught in `_execute`
  and `stream` next to `Cancelled`, and becomes `PRECONDITION` (4) with `INPUT_REQUIRED`,
  `EDITOR_REQUIRED` (plus `error.alternatives`), or `INTERACTIVE_BLOCKED`.
  `_Run.guard_streams` swaps `sys.stdin` for `NoPromptStdin` in non-interactive runs: it
  delegates to the wrapped stream, refuses to read a terminal, and raises when the first
  `readline` (what `input()` calls) finds stdin empty; piped data reads normally.
  `App.call` swaps neither stream. `--yes` and `--non-interactive` exist only on
  `interactive=True` commands
- **Every list output is paginated.** `paginated=None` (the default) makes any
  non-streaming `list[T]` or `Page[T]` output a list command (REQ-F-018 is
  framework-automatic); `paginated=False` opts a `list[T]` out, and the `paginated-list`
  audit rule is advice on those opt-outs
- **The framework slices every page.** A paginated handler returns the whole
  `list[T]` or one `Page[T]` batch; `_page.take` cuts it to the limit. The cursor is
  base64url JSON of the command path, a digest of the non-secret args (checked in
  `_Run._position` with the command's `cursor_check`, before user code), the handler's own
  cursor, and a skip count into the batch that cursor returns, so one shape resumes a sliced batch, a handler batch, and a
  page the byte cap cut (`_cap.Rerun` carries the position and the argv for the hint).
  `meta.pagination` holds exactly the five spec keys (`additionalProperties: false`);
  `paginated` and `default_limit` are only in `--schema`. Streams are not paginated.
  `ParseError(code=...)` sets `error.code` for a single error (`INVALID_CURSOR`)
- **A closed stdout exits 0 after a delivered envelope (D1).** `_Run.delivered` turns true
  once an envelope, event, or rendered result was written and flushed; `output_closed`
  then returns `0` (REQ-F-014), else `141` (`OUTPUT_CLOSED`). Heartbeats do not count
- **Heartbeats tick on the waiting thread.** `call_with_timeout(heartbeat=...)` joins the
  worker in slices and calls `Heartbeat.tick` between them, outside the armed window; only
  `heartbeat=True` commands, in JSON mode, from argv (`_Run.argv` is None in `exec` and
  `App.call`). `App.main()` sets `PYTHONUNBUFFERED=1` and line-buffers a piped stdout
- **Payloads are read in `_Run.execute`, before anything else.** `stdin_input=True` reads
  `--input-file` or the capped stdin through `_read_input`, which `exec` shares; the text
  reaches the handler as `ctx.stdin_text`. `_Run.payload_stdin` is None in `App.call` and
  after `exec` read its plan, so those need `input_file`. `ErrorDetail.hint` names the flag
- **`jsonl` is `json` under another name.** `_route` maps it to `Format.JSON` right after
  resolving the mode, so handlers see `json`; only `--output` keeps the distinction (one
  item per line in the file). `tsv` is built in (`_table.table("\t")`), replaceable with
  `app.format`. `--output PATH` exists only on `output_file=True` commands and only from
  argv; the stdout envelope is always JSON
- **Credentials are one app method, called where the handler runs.** `App(credentials=)`
  takes an object with `active_scopes(ctx)`; `_invoke` calls `App._gate` before resources
  for `requires_auth` commands, on the worker thread and under the timeout, so its
  `AUTH_REQUIRED` (8) or `PERMISSION_DENIED` (7) is an ordinary `CliExit` and an exception
  from it is `HANDLER_CRASHED`. Both codes are implicit for gated commands and listed in
  their manifest entry. `check-permissions` exists only with `credentials=`
- **Warnings collect on the run.** `ctx.warn` appends to `_Run.warnings`, and `_envelope`
  adds them to every envelope built afterwards; `exec` clears them, and the login token,
  per line
- **A login token is read in `_Run.execute`**, before idempotency, like a payload:
  `--token-env-var NAME`, else the first set variable of `Command.token_env_vars`
  (`<APP>_TOKEN` first). A browser login in a headless run, or a named variable that is
  empty, exits 4 with `TOKEN_REQUIRED` and `ErrorDetail.auth_methods`. `_Run.token` joins
  the redactor's spellings. `--headless` also makes `ctx.headless` and `ctx.open_url`
  headless, but not `meta.headless`, which describes the environment
- **A `Job` in data gets its links in `_Run._payload`.** Any `treaty.Job` result or exit
  `data` (the built-ins' too) gains `terminal`, `status_command`, and `cancel_command`;
  `descriptor_schema` adds them to the output schema, which the manifest also serves as
  `job_descriptor_schema`. `job status` reuses framework codes: 3 is `PARTIAL_FAILURE`
  (`JOB_RUNNING`), 4 `PRECONDITION` (`JOB_FAILED`, `JOB_CANCELLED`), 5 `NOT_FOUND`, each
  with the job as `data`. `App(jobs=)` registers the `job` group before any user command
- **Config paths are decided in `_Run.execute`**, before idempotency: the project file is
  `Path.cwd() / .<app>.toml`, the user file comes from the run's env. `ConfigFile` reaches
  the handler as `ctx.config`; only a global write locks (`<file>.lock`, left in place)
  and warns. `_atomic.write_atomic` (mkstemp in the target's directory, fsync, rename) is
  shared by idempotency records, config writes, and `--output`
- **`meta` is built in one place, `_Run._envelope`,** from `_Run.current` (the command
  being answered, set by `_route`, `App._call`, and each `exec` line), `pinned` (the
  `--schema-version` shim), `retrier` (`meta.retries`), and what `_Run.__init__` read once:
  `timestamp`, `cwd` (logical `PWD`), and `TOOL_TRACE_ID`. `Envelope.meta` is a frozen
  `treaty.Meta`; `extra_meta` holds the rest (`pagination`, `dry_run`, `_line`, ...)
- **Reserved flag names are one table** (`_framework.RESERVED_GLOBAL`, `RESERVED_OPT_IN`,
  `UNIMPLEMENTED`). `framework_collisions` refuses fields taking one; `split_globals` and
  `parse_command_args` answer an unimplemented one with `RESERVED_FLAG`. Implementing a
  name means moving it into `IMPLEMENTED`
- **`--schema-version` is global in argv and the key `schema_version` in JSON**, never a
  `FLAGS` row, because a `CommandEntry.flags` map may not repeat a root flag;
  `Command.pin` turns the major into the compat version, or refuses it
- **A keyword field is spelled without its trailing underscore.** `for_` is `--for` and
  the JSON key `for` (`_flags.flag_name`); `payload_schema` keys follow the flag

## Layout

```
src/treaty/
  __init__.py    public API; everything else is private
  _app.py        App, Group, run(), _Run (envelope construction, exec loop, --schema)
  _audit.py      ordered static rules over a registry; RULES tuple is the audit order
  _cli.py        the `treaty` console script (audit, rules, init, conformance)
  _profile.py    probes from examples and danger levels, profile writer, kit runner
  _scaffold.py   file templates for `treaty init`; generated projects pass the audit
  _scalars.py    ScalarSpec and ScalarRegistry: custom scalar classes and their constraints
  _resources.py  ResourceSpec, resource_graph(), Resolver: typed handler resources
  _mcp.py        the `treaty-mcp` console script: tool entries over App.call, stdio server
  _cap.py        OutputCap, cap_envelope(): byte cap with per-field truncation; StdinCap
  _page.py       Page, PageRequest, Limit, Position (cursor tokens), take(): list commands
  _table.py      table(): delimited rows under a header, the built-in tsv renderer
  _command.py    Command record, build_command(), handler signature inspection
  _framework.py  FLAGS: one row per per-command framework flag (parse, JSON, manifest, help)
  _context.py    Ctx handed to handlers (mode, env, timeout, color, headless, log, run, ...)
  _dispatch.py   DispatchRequest line parser for exec
  _effect.py     effect contract: registration check and per-run validation
  _envelope.py   Envelope, ErrorDetail, WarningDetail, write_envelope()
  _errors.py     TreatyError family (registration), ParseError, CliExit, Exit factory
  _exit.py       ExitCodeEntry, FrameworkCode, ExitCodeRegistry, signal entries
  _flags.py      Arg/Flag markers, FieldInfo, inspect_fields(), token coercion
  _help.py       plain-mode help renderer (root, group, command)
  _idempotency.py  IdempotencyKey VO, per-key locked record store, state dir lookup
  _manifest.py   build_manifest(), command_entry(), command_schema(), etag
  _mode.py       OutputMode resolution (--format, TREATY_FORMAT, CI, tty)
  _plain.py      plain fallback: flat key: value lines for commands without plain=
  _parse.py      globals, path routing, per-command parsing, mapping builder, raw payload
  _schema.py     annotation to draft-07 schema, to_jsonable()
  _paths.py      check_path(): null bytes, percent-encoding, and .. in Path flags
  _secrets.py    secret sources (--x-from-env, --x-from-file) and their resolution
  _auth.py       Credentials protocol, AuthKind, scope coverage for the gate and check-permissions
  _jobs.py       Job descriptor, JobStore protocol, descriptor schema and links
  _config.py     ConfigScope, project and user config paths, ConfigFile (ctx.write_config)
  _atomic.py     write_atomic() and the advisory file locks idempotency and config share
  _scan.py       registration-time scan of a handler's ctx.<method>() calls
  _prompt.py     Prompter (ctx.prompt, ctx.confirm, ctx.edit), InputRequired, stdin guard
  _subprocess.py Processes (ctx.run, ctx.pipeline, ctx.open_url), Completed, group kill
  _signals.py    SIGINT/SIGTERM handlers, Cancellation (armed windows, held signals)
  _timeout.py    Timeout VO, call_with_timeout()
  _types.py      annotation classification shared by _flags and _schema
  _values.py     CommandPath, ExitCodeName, ExitCode, Scope, Etag
examples/        deployctl.py (destructive, raw payload, async job, config write),
                 slowctl.py (timeout, cleanup),
                 authctl.py (credentials, login)
conformance/     deployctl.json profile and launcher for the spec kit
tests/           one file per feature; conftest.py holds the shared app fixture
```

## Execution path

1. `split_globals` strips `--format`, `--help`, `--schema`
2. `resolve_mode` picks plain or JSON
3. `resolve_path` consumes tokens by longest known prefix
4. `parse_command_args` (argv) or `build_from_mapping` (exec, raw payload) yields an
   `Invocation`: args dataclass plus `timeout` and `confirmed`. Field errors are collected
   in a `_Collector` and raised together as one `ParseError.combine(...)`; every token error,
   framework flags and a trailing flag with no value included, is collected; only bad
   raw-payload JSON aborts at once
5. `_Run.execute` applies the destructive preview rule, runs the handler under
   `call_with_timeout` inside `cancellation_handlers`, and returns an `Envelope`
6. `_Run.emit` writes JSON or plain output and returns the exit code

`exec` loops steps 4 to 6 per stdin line with `_cmd` and `_line` added to `meta`.

## Spec coverage

Implemented: REQ-F-001, F-002, F-003, F-004, F-005, F-006, F-007, F-008, F-009, F-010,
F-011, F-012, F-013, F-014, F-015, F-016, F-018, F-019, F-021, F-022, F-023, F-024,
F-025 (not the audit log), F-027, F-031, F-034, F-044, F-045 (paths),
F-046, F-047, F-048, F-051, F-052, F-053, F-054, F-055, F-057, F-062, F-065, F-069, F-070, F-078,
C-001, C-002, C-003, C-004, C-005, C-007, C-012, C-013, C-015, C-016, C-020 (all presets),
C-021, C-022, C-023, C-025, C-029, O-001, O-003, O-013, O-014, O-021, O-022, O-032, O-033, O-039, O-041,
O-047, O-048, O-050. Every Level 2 requirement is done. See `COMPLIANCE.md` for
the stricter per-criterion status.

Framework flags the parser knows: `--format`, `--help`, `--schema` (and `--print-schema`),
`--output-schema`, `--schema-version`, `--max-output`, and per
command `--timeout` (network and streaming), `--confirm-destructive` (destructive),
`--idempotency-key` (non-safe), `--raw-payload` (opt-in), `--no-stream` (streaming), `--live`
(`safe_default`), `--yes` and `--non-interactive` (`interactive=True`), `--limit` and
`--cursor` (list outputs), `--heartbeat-ms` (`heartbeat=True`), `--input-file`
(`stdin_input=True`), `--output` (`output_file=True`), `--headless` and `--token-env-var`
(`auth=`), `--global` (`config_write_scope=`), `--retries` and `--retry-delay` (`retry=`), and
`--<name>-from-env` / `--<name>-from-file` for each secret field. The per-command ones are
rows of `_framework.FLAGS`: argv parsing, the JSON routes, the `--raw-payload` merge (one
"given twice with different values" check; `--limit` and `--cursor` from argv win),
`known_flags`, the field collision check in `App._register`, the manifest entry, the
payload schema, and the help all iterate it. A new framework flag is one row plus an
`Invocation` field.

## Gotchas

- `tests/conftest.py` locates the spec checkout at `../cli-agent-ergonomics`; override with
  `TREATY_SPEC_DIR`. Schema and kit tests skip when it is absent
- `conformance/deployctl` runs the example through `.venv/bin/python`, so `uv sync` first
- Signal tests spawn real subprocesses and sleep; they add about three seconds
- `ruff --fix` once rewrote a deliberate `getattr` into attribute access and broke mypy;
  check the diff after autofix
- `tests/fixture_audit_app.py` is a deliberately flawed app; the audit tests count its
  findings exactly, so adding a rule means updating `failed == 9` there
- The kit resolves a `command` path containing a slash against the profile's directory;
  `_profile.build_profile` makes a relative `--command` absolute (with `absolute()`, so a
  venv's `bin/python` symlink survives) and keeps the scaffold's `./<name>` launcher,
  found next to the profile, relative
- `run_kit` strips `VIRTUAL_ENV` before calling `uv run --project <spec>`, or uv warns
  about the mismatched environment on stderr
- `treaty init` needs `--treaty-source <checkout>` until the package is on PyPI
- The `treaty` CLI owns command-specific exit codes 79 (`AUDIT_FAILED`) and 80
  (`CONFORMANCE_FAILED`); pick the next free code in 79..125 for new ones
- Exit code entries reject descriptions over 120 characters or ending in a period; that is
  the spec's rule, not a style choice

## Commands

```bash
uv sync
uv run pytest
uv run mypy src
uv run ruff check src tests examples && uv run ruff format --check src tests examples
uv run examples/deployctl.py deploy rollback api --dry-run
uv run --project ../cli-agent-ergonomics ../cli-agent-ergonomics/conformance/run.py conformance/deployctl.json
```
