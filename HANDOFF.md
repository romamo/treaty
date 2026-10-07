# Handoff

Status as of 2026-09-30. Read this before touching the code.

## What this is

`treaty` is a zero-dependency Python CLI framework that implements the
[CLI Agent Spec](../cli-agent-ergonomics). The spec repo is the source of truth for
every schema and requirement cited here; this repo is one implementation of it.
`agentyper` (sibling directory) is a separate, older library on pydantic and rich.
The two do not share code.

## State

| Check | Result |
|-------|--------|
| `uv run pytest` | 2133 passed, 1 skipped |
| `uv run mypy src` (strict) | clean |
| `uv run ruff check src tests examples` and `ruff format --check` | clean |
| `uv run treaty check-docs treaty._cli:cli AGENTS.md` | no mismatches |
| Spec conformance kit against `examples/deployctl.py` | 12 of 12, levels 1 to 3 |
| Git | `main`, pushed to `origin` (github.com/romamo/treaty) |
| PyPI | `1.0.0rc5`, classifier `4 - Beta` |

## Release

`plans/1.0/` is built and merged. Five release candidates went out ahead of the consumer
ports: rc1 and rc2 on 2026-09-28, rc3 to rc5 on 2026-09-30. rc3 and rc4 each have a
Breaking section in `CHANGELOG.md`, and rc5's `stable-order` fix can fail a
`treaty audit --strict` that rc4 passed, so the API has not yet held still through a
release candidate.

shipmill cuts releases (`.github/workflows/release.yml`, policy in
`.github/shipmill.toml`); nobody tags by hand. The `rc` lane releases the next `1.0.0rcN`
from main once main has been quiet for 30 minutes with entries under Unreleased. An rc
writes no CHANGELOG section: its entries stay under Unreleased until the stable release.
The `stable` lane promotes an rc that soaked 3 days to `1.0.0`, and only once the `1.0.0`
milestone has closed issues and none open, so the remaining 1.0 tasks belong in that
milestone. `hotfix` runs by hand. Each release stamps `pyproject.toml`, `uv.lock`, the
AGENTS.md `cli-version` comment, and the ROADMAP.md release line, runs CI on the release
commit, tags it, and starts `publish.yml`, which runs CI on the tag again before uploading.
An open `release-blocker` issue holds `rc` and `stable`; an open `shipmill-hold` issue
stops every release. `uvx --from git+https://github.com/shipmill/shipmill@v0 shipmill
plan --lane rc --dry-run` previews the next release.

Before `1.0.0` (the open tasks of `plans/1.0/15-release-readiness.md`):

- Finish the cloudfall port and port a second, read-heavy CLI (pagination, streaming).
  Gaps they find go into another release candidate
- Soak one release candidate with both consumers for two weeks with no API change
- Name the spec version treaty conforms to. CI pins `SPEC_REF` to `5e6f0d8`, the spec's
  1.9.0 release commit (for REQ-O-030's `audit-log-entry.json`, #71); either the spec tags
  it `v1.9.0`, or 1.0 names the commit
- Run `benchmark/` against the release candidate for the release notes, and check
  `docs/tutorial/` against the frozen API
- At the tag: classifier `5 - Production/Stable`; update `COMPLIANCE.md`, `README.md`,
  `ROADMAP.md`, and this file

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
- **Streams are envelopes inside, spec lines on stdout (D-10, #389).** `_Run.stream`
  yields one envelope per event with `meta.seq`, then a terminal envelope (`end`,
  `total`), which `exec` lines and `App.call` keep; `_write_item_stream` turns them into
  REQ-O-004's lines on stdout in `json`/`jsonl`: bare items (`_seq` when `_numbered`),
  then a `_summary` line or the error envelope. `_Run.stream` is a generator; `drain()` throws a
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
- **`data` goes through one pipeline, `_Run._payload`**: `to_jsonable` (bytes and
  `Binary` to the base64 wrapper, a relative `Path` joined to `meta.cwd`), then
  `_out.arrange`, which walks the output annotation beside the JSON value to sort arrays
  (`Command.order` for the top level, `Out` metadata per field, untyped content sorted by
  kind unless an enclosing declaration is `ordered`) and, under `--stable-output`, drop
  `Out(volatile=True)` fields. A list command sorts its whole list in `_sorted_list`
  before `take` slices it. The manifest command is `ordered=True`: positional order is
  meaning
- **Output schemas are built with `schema_for(..., output=True)`**: every dataclass key
  is `required` except a volatile one, and a nullable collection is a
  `RegistrationError`. Args schemas keep `output=False`, where a default makes a key
  optional
- **`--stable-output` is a global, not a `FLAGS` row**, like `--schema-version`;
  `_Run.stable_all` holds the argv flag and `_Run.stable` the current envelope's (an exec
  line or MCP call may set `stable_output` for itself). `Meta.request_id` and
  `Meta.timestamp` are None under it and left out of `meta`
- **Settings are read once per run, before routing** (`_Run.load_settings`, from
  `_settings.options` and `resolve`), so every envelope, help and errors included, carries
  `config_sources` and `effective_config_hash` from `Resolved.meta()`. A bad file is held
  in `_route` as `config_error`: help, `--schema`, `--output-schema`, `version`, and
  `manifest` (`PURE_PATHS`) still answer, with empty config meta (REQ-F-068); every other
  command and `--show-config` answer `CONFIG_INVALID`. The settings class is not a resource: `build_command`
  gets it as `provided`, `resource_graph` skips it, and `Resolver` starts with its value
  cached. `App.call` and MCP read `<APP>_CONFIG`, `<APP>_CONTEXT`, `<APP>_INSTANCE_ID`
- **Every variable treaty reads is `_env.app_var(app, key)`**; `_env.KNOWN` lists the
  framework's own, and `App.environment()` adds settings fields and secret defaults for the
  Environment section of `--help`. Global flag help rows come from the manifest's root
  `flags` (`_help.global_rows`), so the two cannot drift
- **`App.builtins`** is every path `_register_builtins` added (including `init` under
  `App(init=)`); the audit skips them and the `INIT_REQUIRED` gate in `_invoke` lets them
  through
- **Error fields only the framework sets ride on `CliExit` subclasses** read in
  `_Run._exit_envelope`: `RetriesExhausted` (`retries_exhausted`), `_auth.AuthFailure`
  (`hint`, `refresh_command`, `expires_at`, `required_permission`), and `_locks.LockHeld`
  (retryable though `PRECONDITION` is not, 03-D1). `CliExit` itself takes only what a
  handler may set; `network_context` has no keyword at all, for `ctx.http` (10) to fill
- **`fix_command` is checked twice**: a declared `fix_commands=` value's shape at
  registration and its target in `App.check_fixes`, which `run`, `call`, and `manifest`
  call once (reset by every `_register`); a raised one in `_exit_envelope`
- **`App.redirect`** keeps `_redirects` (old path to `Moved`) and adds the old path to the
  target's `Command.aliases`; `App.moved` is checked only where a path failed to resolve:
  `_route`'s unknown-command branch, `_call`, and `_exec_lines`
- **Strict placement is a `--` inserted before routing.** `_parse.strict_argv` finds the
  path in raw argv (skipping globals), and for an `option_placement="strict"` command puts
  `--` before its first positional; `split_globals` and `parse_command_args` already stop
  there, so nothing after it is parsed
- **`requires=` rules run in `_finish`** (`_rules.check_rules`) on the fields the caller
  supplied, after the missing check and before defaults fill in; `DefaultWhenAbsent`
  writes into those values, so `__post_init__` sees the rule's default.
  `Invocation.given` keeps the supplied names, which `_Run._deprecations` reads for
  `DEPRECATED_FLAG`
- **`--validate-only` returns from `_Run.execute` and `_Run.stream`** after `_pin` and
  the paginated cursor check, before the login token, config file, stdin payload,
  idempotency store, and handler
- **JSON input goes through `_json5.loads_forgiving`**: strict first, so valid JSON (and
  its NaN, digit-limit, and nesting errors) behaves as before; the forgiving parser only
  runs on a `JSONDecodeError`, and any repair it needs makes the input `INVALID_JSON`
  with `corrected_input`
- **A keyword field is spelled without its trailing underscore.** `for_` is `--for` and
  the JSON key `for` (`_flags.flag_name`); `payload_schema` keys follow the flag
- **One `Teardown` per handler run** (`_lifecycle.py`), built in `_Run._ctx` and carried
  on `Ctx.teardown`. `Resolver.get` adds each acquired resource's `release`; `_invoke`
  calls `begin()` and runs it in a `finally` on the handler's thread, except for a stream,
  whose `stream()` runs it after closing the generator and before building the terminal
  envelope. The timeout path (`_after_grace`) and `_cancelled` join the worker for
  `GRACE_SECONDS` when something is pending, then run it beside the handler (06-D3);
  `output_closed` runs it for an in-flight stream. Only the first `run()` calls hooks; a
  concurrent one waits for it. A later workstream's framework resource (09's session temp
  dir) calls `ctx.teardown.add(name, fn)`; `ctx.lock` still releases in its `with` block
- **Step fields are added after the envelope is built.** `_Run._execute` wraps
  `_run_handler` and `_stepped` merges the `StepTracker` snapshot into `data` for any
  execution-phase envelope, rewriting the exit to 3 when a step completed, unless the
  error is `TIMEOUT` or `CANCELLED` (they keep 10, 130, 143). Replays and validation-phase
  envelopes (previews, refusals) pass through untouched. `ctx.step` and rollback run on
  the handler's thread; rollback in `_invoke`'s `except`, before the teardown, never for
  `Cancelled` or `KeyboardInterrupt`
- **`Batch[T]` commands keep `output_type = T`** with `Command.batch`; `_run_handler`
  builds `summary` and `results` in `_batch_data` without passing the whole `data`
  through `arrange`, so results keep the handler's order; `_batch_envelope` exits 3
- **`_Run._present` is the one output-security step** (`_protect.py`), applied where a
  command's envelope is made, not at each sink: `execute` wraps `_answer`, and `stream`
  presents each event and the terminal envelope, so `--output`, `exec`, `App.call`, and
  `--no-stream` all get the same masked, tagged `data`. It runs after `_keyed`, so the
  idempotency store holds the raw result and a replay with `--unmask` returns it.
  Built-ins are skipped. The typed walk uses `_output(command)` for a success, `object`
  for exit data and a batch; `buffer_stream` carries the events' warnings over. Workstream
  12's `--fields` and token budget belong after it
- **Two secret-name checks, one vocabulary** (`_redact.py`): `SECRET_NAME` (substring,
  REQ-F-034's list plus `cookie` and a `pass` segment) for inputs, settings, logs, and
  stderr, where over-redaction is harmless; `secret_field` (the last word) for masking
  output, where it is not (`author`, `token_count`)
- **Yielding built-ins** (13-D1): `doctor`, `cleanup`, and `audit-log` (`_builtins.py`)
  are registered on every app and listed in `App._yielding`; `_yield_to` drops one when an app command,
  group, or redirect takes its path, and `shadowed_builtins` feeds the `builtin-shadowed`
  audit rule. `manifest`, `version`, and `exec` stay reserved. Workstream 13 adds its
  built-ins the same way
- **Commands a declaration names are checked late** (08-D2): `clearable_with` and
  `Background.cleanup_command` go through `App.named_commands` in `check_fixes`, when the
  manifest is built or the first run starts, and in the `declared-commands` audit rule
- **Session temp dir** (`_session.py`): `_Run.session_for` makes one `Session` per
  command (the first prunes expired output files and day-old session dirs); the
  directory is made on first use and removed by a `last=True` teardown hook, after
  `cleanup=`. `Processes` gives children its `TMPDIR` and rewrites `children.pids` on
  each start and exit; `Teardown.pending` ignores `last` hooks so a timeout does not wait
  on them
- **Descriptor 1 is a pipe under `App.main()`** (`_stdout.py`): a daemon thread tees it
  to stderr and `_Run._write` calls `take()`, which writes a marker and waits for the
  reader to pass it, before each envelope. `App.run()` swaps only `sys.stdout`
- **`App.main()` runs with `CI` as it was started**: it sets `CI=1` in `os.environ` off a
  terminal for libraries and children, but passes the run an env with the original, so
  a terminal's plain output never turns into JSON
- **Every stderr line has a level** (`_verbosity.py`): `_Stderr.write(text, level)` drops
  what the run's `Verbosity` hides; tracebacks and error prose default to ERROR, the
  deprecation and `--no-injection-protection` lines are WARN (so AUTO keeps F-075's line),
  `ctx.log` and stray `print()` are INFO, `ctx.progress` and step events PROGRESS. The
  verbosity is resolved in `_route` right after `split_globals`; `App.call` stays AUTO
- **`--debug` is the `logging` module**: `_verbosity.trace(event, **fields)` logs to the
  `treaty` logger, and `_Run.attach_trace` adds a `_TraceHandler` to the root logger (level
  DEBUG) until `App.run` returns, so framework events and any library's records go through
  `_log_line`'s redaction. `_http.py`, `_subprocess.py`, and `_locks.py` call `trace`
- **`_Run.settle` is the one place an answer is finished**: `--warnings-as-errors`, then
  the audit entry, called by `_write` and `_emit_text` (not for stream events or help,
  `settle=False`) and by `App.call`. `_write` returns the exit code it wrote, since settling
  can change it. `self.args` holds the parsed args for the entry; `exec` resets it per line
- **The audit log is `_journal.py`** (`_audit.py` is the linter), opt-in (REQ-O-030):
  `resolve()` reads `<APP>_AUDIT_LOG` over `App(audit_log=)` once per run (`env_error`
  holds a bad value with code `INVALID_AUDIT_LOG_SETTING`, which only `--help` and
  `--version` answer over; `_answers_over`). `_Run._logged` skips unresolved invocations and
  the `UNLOGGED` built-ins; `encoded()` caps an entry at 16 KiB
- **Output selection is `_select.py`** (12): `--fields` projects at the end of
  `_Run._present`, per envelope, with `self.fields` set in `_pin` like `stable` (an exec
  line's `fields` wins over argv's). The token budget (`TokenBudget.apply`) runs in
  `_write` and `_emit_text` after `settle` and before the byte cap, over `data` as compact
  JSON; its cuts reuse `_cap.shrink`, the byte cap's cut search with a pluggable `fits`.
  `--token-count` forces JSON mode in `_route`. `--format id` is checked in `_present`
  (`INVALID_OUTPUT`) and written by `id_lines` as the renderer
- **`call_with_timeout` takes `heartbeats`**: each ticks on its own due time on the
  waiting thread, so `--heartbeat-ms` (JSON on stdout) and `--heartbeat-interval` (plain
  text on stderr, at ERROR level so only `--quiet` hides it) run together; `ctx.progress`
  stores the redacted status in `_Run.status` whatever the verbosity
- **`ctx.http` never uses urllib's proxy logic**: `ProxyHandler` and `proxy_bypass*`
  read `os.environ` and, on macOS, the system settings, so the opener gets
  `ProxyHandler({})` and `_Proxied`, a pre-processor that routes each request (redirects
  included) from `ProxyConfig`; a retry builds a new `Request`, since `set_proxy` edits it
  in place. Retries go through `Retrier.call(on=, give_up=)`, which re-raises the
  `NetworkFailure` with `retried` set; `_Run` reads `network_context`, `retried`, and
  `permanent` (TLS) off it
- **Built-ins are in `_builtins.py`** (13): each `register_*` returns its path for
  `App._yielding`. `manifest --etag` raises `NotModified`, which `_Run._run_handler` maps to
  `data: null` and `meta.not_modified`. `inventory()` and `declared()` feed both `cleanup`
  and `status`. `mcp-validate` compares through `_tools.tool_fields`, so `_builtins` never
  imports `_mcp` (which imports `_app`)
- **`ctx.spawn` children are not in `Processes._live`**, so the run's teardown leaves
  them; their pid and deadline go to `<state>/background/<command>.pids`, and each later
  spawn of the command SIGTERMs expired entries whose pid still leads its process group
- **The public surface is snapshotted** (15): `tests/test_public_api.py` fails on any
  change to exports, keywords, `Ctx` members, wire keys, exit codes, or env vars.
  Regenerate with `uv run python tests/test_public_api.py > tests/data/public_api.json`,
  record the decision in `docs/api.md`, and add a `CHANGELOG.md` entry. `Ctx` fields
  the run uses internally are `_`-prefixed; a new one should be too
- **`framework_version` is treaty's version**; `removals()` in `_audit.py` takes the
  released app version from the baseline envelope's `meta.tool_version`

## Layout

```
src/treaty/
  __init__.py    public API; everything else is private
  _app.py        App, Group, run(), _Run (envelope construction, exec loop, --schema)
  _audit.py      ordered static rules over a registry; RULES tuple is the audit order
  _cli.py        the `treaty` console script (audit, init, conformance, agents-md, ...)
  _profile.py    probes from examples and danger levels, profile writer, kit runner
  _scaffold.py   file templates for `treaty init`; generated projects pass the audit
  _scalars.py    ScalarSpec and ScalarRegistry: custom scalar classes and their constraints
  _resources.py  ResourceSpec, resource_graph(), Resolver: typed handler resources
  _lifecycle.py  Teardown: one run's release hooks, then cleanup=, once on every exit
  _steps.py      StepName, StepTracker, Rollback: steps=, ctx.step, resume and rollback
  _batch.py      Batch, Item, ItemError, batch_schema(): per-item results (REQ-C-009)
  _declare.py    Subprocess, SideEffect, Background, platform=: 08's declarations
  _session.py    SessionRoot, Session: ctx.tmp_dir, output files, children.pids (09)
  _stdout.py     Interceptor, intercept_stdout(): descriptor 1 as a pipe (REQ-F-060)
  _update.py     UpdateCheck, the cached daily check behind meta.update_available
  _cache.py      CachePolicy, Cache: ctx.cache, --no-cache, --cache-ttl (REQ-O-018)
  _http.py       ProxyConfig, Http, HttpResponse, NetworkFailure: ctx.http (10)
  _walk.py       Walk, WalkEntry, Traversal: ctx.walk, loop and depth limits (10)
  _deps.py       Version, Dependency, doctor's dependency and required-tool checks
  _builtins.py   doctor, cleanup, status, changelog, generate-skills, mcp-validate, audit-log:
                 built-ins that yield to an app command (13-D1)
  _changelog.py  ChangelogEntry, manifest field diff: the schema changelog (13)
  _skills.py     CONTEXT.md and SKILL-<command>.md from the manifest (13)
  _agents_md.py  AGENTS.md sections from the registry, marker rewrite, check-docs (14)
  _tools.py      ToolEntry, tool_entries(), tool_list(): MCP tools as plain data, no App import
  _redact.py     SECRET_NAME, secret_field(), scrub(): what a secret name is (REQ-F-034)
  _verbosity.py  Verbosity, Level, resolve_verbosity(), trace(): stderr levels (11)
  _journal.py    AuditLog, Journal, resolve(), read_entries(): the opt-in audit log (11)
  _protect.py    protect(), tagged(): masking and trust tags of data (REQ-F-058, F-035)
  _mcp.py        the `treaty-mcp` console script: stdio server over App.call, --list-tools
  _cap.py        OutputCap, cap_envelope(): byte cap with per-field truncation; StdinCap
  _select.py     --fields, TokenBudget, tokenizers, --format id lines (12)
  _page.py       Page, PageRequest, Limit, Position (cursor tokens), take(): list commands
  _table.py      table(): delimited rows under a header, the built-in tsv renderer
  _command.py    Command record, build_command(), handler signature inspection
  _framework.py  FLAGS: one row per per-command framework flag (parse, JSON, manifest, help)
  _context.py    Ctx handed to handlers (mode, env, timeout, color, headless, log, run, ...)
  _dispatch.py   DispatchRequest line parser for exec, INVALID_JSON errors
  _json5.py      loads_strict(), loads_forgiving(): JSON5 input and corrected_input
  _rules.py      RequiredWhen, Excludes, DefaultWhenAbsent: bound at registration, checked in _finish
  _deprecation.py  Deprecated: command and flag retirement (REQ-F-075)
  _effect.py     effect contract: registration check and per-run validation
  _envelope.py   Envelope, ErrorDetail, WarningDetail, write_envelope()
  _errors.py     TreatyError family (registration), ParseError, CliExit, Exit factory
  _exit.py       ExitCodeEntry, FrameworkCode, ExitCodeRegistry, signal entries
  _flags.py      Arg/Flag markers, FieldInfo, inspect_fields(), token coercion
  _help.py       plain-mode help renderer (root, group, command)
  _idempotency.py  IdempotencyKey VO, per-key locked record store, state dir lookup,
                 session_key for <APP>_SESSION
  _fix.py        fix_problem(): what makes a fix_command unsafe to run verbatim
  _locks.py      ctx.lock: named flock under <state>/locks, LockHeld (LOCK_HELD)
  _manifest.py   build_manifest(), command_entry(), command_schema(), etag
  _mode.py       OutputMode resolution (--format, <APP>_FORMAT, CI, tty)
  _env.py        app_var(): the <APP>_ prefix, KNOWN framework variables, UNPREFIXED list
  _settings.py   App(settings=): layered read, contexts, --show-config data, CONFIG_INVALID
  _init.py       Init protocol, the init built-in, INIT_REQUIRED and INIT_FAILED
  _plain.py      plain fallback: flat key: value lines for commands without plain=
  _parse.py      globals, path routing, per-command parsing, mapping builder, raw payload
  _schema.py     annotation to draft-07 schema, to_jsonable()
  _paths.py      check_path(): null bytes, percent-encoding, and .. in Path flags
  _secrets.py    secret sources (--x-from-env, --x-from-file) and their resolution
  _auth.py       Credentials protocol, AuthKind, scope coverage for the gate and check-permissions
  _jobs.py       Job descriptor, JobStore protocol, descriptor schema and links
  _config.py     ConfigScope, project and user config paths, ConfigFile (ctx.write_config)
  _atomic.py     write_atomic() and the advisory file locks idempotency and config share
  _scan.py       registration-time scan of a handler's ctx.<method>() calls and shell calls
  _prompt.py     Prompter (ctx.prompt, ctx.confirm, ctx.edit), InputRequired, stdin guard
  _subprocess.py Processes (ctx.run, ctx.pipeline, ctx.spawn, ctx.open_url), Completed,
                 HeadlessBehavior, group kill
  _signals.py    SIGINT/SIGTERM handlers, Cancellation (armed windows, held signals)
  _timeout.py    Timeout VO, call_with_timeout()
  _aio.py        async handlers and resources on one event loop per run (REQ-F-049)
  _completion.py shell completion scripts generated from the manifest
  _meta.py       what a run knows for meta: start time, cwd, trace id
  _out.py        Out marker, Binary, and the sort order of data arrays (REQ-F-020)
  _retry.py      Retry, ctx.retry, and meta.retries (REQ-F-078)
  _suggest.py    did-you-mean for an unknown command
  _types.py      annotation classification shared by _flags and _schema
  _values.py     CommandPath, ExitCodeName, ExitCode, Scope, Etag, InstanceId
examples/        deployctl.py (destructive, raw payload, async job, config write),
                 slowctl.py (timeout, cleanup),
                 authctl.py (credentials, login)
conformance/     deployctl.json profile and launcher for the spec kit
tests/           one file per feature; conftest.py holds the shared app fixture
```

## Execution path

1. `split_globals` strips `--format`, `--help`, `--schema`, and the other globals
2. `resolve_mode` picks plain or JSON; `load_settings` reads the config layers, and
   `--show-config` answers here
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
F-011, F-012, F-013, F-014, F-015, F-016, F-017, F-018, F-019, F-020, F-021, F-022, F-023, F-024,
F-025, F-026, F-027, F-028, F-029, F-030, F-031, F-032, F-034, F-035, F-036, F-037, F-038, F-040, F-041, F-042, F-043, F-044, F-045 (paths),
F-046, F-047, F-048, F-050, F-051, F-052, F-053, F-054, F-055, F-057, F-058, F-060, F-061, F-062, F-063, F-064, F-065, F-066, F-069,
F-070, F-072, F-073 (not the manifest list), F-074, F-076, F-078,
C-001, C-002, C-003, C-004, C-005, C-007, C-008, C-009, C-010, C-011, C-012,
C-013, C-015, C-016, C-017, C-018, C-019,
C-020 (all presets), C-021, C-022, C-023, C-024, C-025, C-029, O-001, O-002, O-003, O-004, O-005, O-007, O-008, O-010, O-011, O-012, O-013, O-014, O-015, O-016, O-017, O-018, O-019, O-020, O-021, O-022,
O-023, O-024, O-025, O-026, O-027, O-028, O-029, O-030, O-031, O-032, O-033, O-034, O-035, O-036, O-037, O-039, O-040, O-041, O-042, O-047, O-048, O-049, O-050. Every Level 2 requirement is done. See `COMPLIANCE.md` for
the stricter per-criterion status.

Framework flags the parser knows: `--format`, `--help`, `--schema` (and `--print-schema`),
`--output-schema`, `--schema-version`, `--stable-output`, `--unmask`,
`--no-injection-protection`, `--max-output`, `--config`,
`--context`, `--no-config`, `--show-config`, `--instance-id`, `--cwd`, `--no-update-check`,
`--quiet`, `--verbose`, `--debug`, `--warnings-as-errors`, `--fields`, `--stream`,
`--token-limit`, `--token-offset`, `--token-count`, `--tokenizer`, and per
command `--timeout` (network and streaming), `--confirm-destructive` (destructive),
`--idempotency-key` (non-safe), `--raw-payload` (opt-in), `--no-stream` (streaming), `--live`
(`safe_default`), `--yes` and `--non-interactive` (`interactive=True`), `--limit` and
`--cursor` (list outputs), `--heartbeat-ms` and `--heartbeat-interval` (`heartbeat=True`), `--input-file`
(`stdin_input=True`), `--output` (`output_file=True`), `--headless` and `--token-env-var`
(`auth=`), `--global` (`config_write_scope=`), `--retries` and `--retry-delay` (`retry=`),
`--resume-from` (`resumable=True`), `--rollback-on-failure` (`rollback=`), `--no-cache` and
`--cache-ttl` (`cache=`), `--proxy` and `--no-proxy` (`has_network_io=True`),
`--no-follow-symlinks` and `--max-depth` (`recursive_traversal=True`), and
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
  findings exactly, so adding a rule means updating `failed == 12` in `tests/test_audit.py`
- Apps under test pass `env={}`, so no user config file is found; a project file needs a
  real process with its own `cwd` (see `tests/test_config_layer.py`), since the project
  file is found from `meta.cwd`
- The kit resolves a `command` path containing a slash against the profile's directory;
  `_profile.build_profile` makes a relative `--command` absolute (with `absolute()`, so a
  venv's `bin/python` symlink survives) and keeps the scaffold's `./<name>` launcher,
  found next to the profile, relative
- `run_kit` strips `VIRTUAL_ENV` before calling `uv run --project <spec>`, or uv warns
  about the mismatched environment on stderr
- `treaty init` depends on treaty from PyPI; `--treaty-source <checkout>` pins a local
  checkout to try unreleased changes
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
