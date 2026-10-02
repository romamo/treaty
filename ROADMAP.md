# Roadmap

Ordered by value to an agent using a treaty-built CLI. Requirement IDs refer to the
[CLI Agent Spec](../cli-agent-ergonomics/requirements/index.md).

## Done since the skeleton

- `treaty audit module:app` console script: eleven ordered static rules over the registry,
  each with a generated fix, human and JSON output, `--all` and `--limit`; `treaty rules`
  lists the rule order
- `human=` renderer hook on `@app.command` for commands whose human output should not be
  raw JSON
- `treaty init <name>` scaffolds a project that passes the audit and the kit at all three
  levels; `--treaty-source` pins a local checkout until the PyPI release
- `treaty conformance module:app [--run]` derives probes from examples and danger levels,
  writes the profile, and runs the spec kit; fails with `CONFORMANCE_FAILED` on failing checks
- `treaty audit --strict` exits `AUDIT_FAILED` (79) on any warning or error finding, for
  CI; the envelope keeps the full report as `data` and advice never fails the run
- Human mode renders `data` on failed runs too, so `--strict` and a failing
  `conformance --run` print their report instead of raw JSON
- Handler-raised `ParseError` and `Exit.ARG_ERROR` exit 1 with `VALIDATION_AFTER_START`
  and `phase: execution` (REQ-F-002); cross-field checks in the args `__post_init__` run
  in phase 1 and join `error.errors` (REQ-F-015)
- Newlines, carriage returns, and null bytes in `str` arguments are refused in phase 1
  on every route; `Flag(multiline=True)` opts out; `multiline-flag` audit rule (REQ-F-044)
- `effect` on every mutating and destructive response (REQ-C-003, REQ-C-004) and a
  framework `--idempotency-key` with a per-app record store (REQ-C-007)
- `exec` stdin cap (64 KiB, `STDIN_TOO_LARGE`) with an uncapped `--input-file`
  (REQ-F-054, REQ-O-039)
- Secret flags (REQ-F-034, REQ-F-051): `secret=` on `Flag`, inferred from the name by
  default; their values and any unknown `--name=value` are never echoed in errors
- Secrets only via env var or file (REQ-C-016, REQ-O-022): `--<name>-from-env` and
  `--<name>-from-file` replace the direct flag, `<APP>_<NAME>` is the default source and is
  listed in `secret_env_vars`; positional, boolean, array, and short-flag secrets are
  registration errors
- `Path` fields (REQ-F-045, the `filepath` preset of REQ-C-020): `..` segments,
  percent-encodings, and null bytes are rejected in phase 1 on every input route, with
  `rejected_pattern` and a decoded or absolute suggestion; `path-typed` audit rule
- All validation errors in one run (REQ-F-015): both parse routes collect field errors and
  report them in `error.errors`; every error already carried `phase`
- Response size cap (REQ-F-052): 1 MiB default, `--max-output` and
  `TREATY_MAX_OUTPUT_BYTES`, `meta.truncated` with a hint, and a `FIELD_TRUNCATED` warning
  per cut field (REQ-F-064)
- Custom scalars (cloudfall gap 1): `app.scalar(cls, parse=, base=, pattern=,
  pattern_type=, minimum=, maximum=, serialize=)`; the registry reaches `classify`,
  `schema_for`, and `to_jsonable`; both input routes check the constraint and then call the
  parser; the manifest and `--schema` carry the pattern, preset, and bounds
- Typed resources (cloudfall gap 2): handler parameters after `ctx` name classes with a
  classmethod `acquire(cls, args, ctx, *deps)`; acquired once per run in dependency order
  under the command timeout; `CliExit` and `ParseError` from `acquire` take the normal
  envelope path (a `ParseError` there is `VALIDATION_AFTER_START`); cycles and missing `acquire` fail at registration
- Streaming handlers (cloudfall gap 3, REQ-O-004): `streaming=True` with an `Iterator[T]`
  handler; one envelope per yield with `meta.seq`, a terminal envelope with `end` and
  `total`, failures mark `partial`; the timeout limits the wait for each event (it was a
  whole-stream deadline, with none by default, until plan 07);
  `streaming_default` in the manifest and `--help`; `--no-stream` buffers; works in `exec`
- MCP adapter (cloudfall gap 4, `treaty[mcp]`): `treaty-mcp module:app` serves one tool
  per command in-process over stdio; input schema from the args schema plus framework
  keys, secrets as `_from_env` and `_from_file`; output schema is the envelope; calls go
  through the new `App.call`, so confirmation, idempotency, timeouts, effects, and caps
  apply; streams come back buffered
- `--format plain` replaces `--format human` (REQ-O-001), with no alias; `plain=` replaces
  `human=`; commands without a renderer print flat `key: value` lines instead of indented
  JSON; `manifest` and `--schema` stay JSON
- Output hygiene (REQ-F-005 to F-008, F-010, F-016, F-051, C-013): stray `print()` goes to
  stderr with a `THIRD_PARTY_STDOUT` warning, `ctx.log` with redaction, escape and UTF-8
  cleaning in JSON, `ctx.color`, `PAGER=cat` and `NO_COLOR=1` for children under
  `App.main()`, ISO 8601 dates and `Decimal` text, sentence-form error messages, and
  `suggestion=` on exit codes with the `exit-code-suggestion` audit rule
- Required declarations (REQ-C-001, REQ-C-002, breaking): `exit_codes=` and
  `danger_level=` have no default; `exit_codes=()` is the explicit empty declaration
- `treaty.Affects` as `would_affect` on destructive dry runs, checked at registration and
  per run, and quoted by `CONFIRMATION_REQUIRED` (REQ-C-004); `requires_confirmation: true`
  in `--schema` (REQ-O-021)
- `safe_default=True` destructive commands: dry run by default with exit 0, `--live` to
  apply (it is the confirmation), `meta.dry_run` on every response (REQ-O-048)
- `network-timeout` audit rule: network calls without `timeout=` in network commands
  (REQ-C-012)
- Subprocess API (REQ-F-044, F-046, F-055, F-057, F-062, F-065): `ctx.run` and
  `ctx.pipeline` take argument lists only, give children a pager-, color-, and
  editor-free environment and `/dev/null` stdin, raise `SUBPROCESS_FAILED` for any failing
  stage, and stop tracked children on a signal or timeout; `ctx.open_url` with
  `gui_operations=` and `meta.headless`; `no-shell` audit rule
- Prompts (REQ-F-009, F-047, F-055, C-005, C-023): `interactive=True` adds `--yes` and
  `--non-interactive`; `ctx.prompt`, `ctx.confirm`, and `ctx.edit` ask only on a terminal
  and otherwise exit `4` naming the flag that answers; `editor_alternatives=`; a stray
  `input()` off a terminal exits `4` with `INTERACTIVE_BLOCKED`
- Pagination (REQ-F-018, F-019, O-003, F-052): `paginated=True`, `--limit` and
  `--cursor`, `meta.pagination`, `treaty.Page` and `ctx.page`, `INVALID_CURSOR`; a cut
  page's `truncation_hint` is the command for the next page; `<APP>_MAX_OUTPUT_BYTES`;
  `paginated-list` audit rule
- I/O and streams (REQ-F-011, F-014, F-053, F-054, O-001): idle timeouts for streams,
  exit `0` when the reader leaves after a complete envelope, `PYTHONUNBUFFERED` and
  `heartbeat=True`, `stdin_input=True` with `--input-file` and `hint`, `--format jsonl`,
  built-in `tsv` and `treaty.table`, `output_file=True` with `--output PATH`
- Auth and scopes (REQ-C-021, C-029, O-033, O-047): `App(credentials=)` with one
  `active_scopes` method, `requires_auth=True` gated before the handler (exit `8` or `7`,
  `CREDENTIAL_OVER_PRIVILEGED` warning), `check-permissions`, login commands with `auth=`,
  `--headless`, `--token-env-var`, and `ctx.token`; `ctx.warn`; `broad-scope` and
  `auth-declared` audit rules
- Async jobs and config writes (REQ-C-022, C-025, F-070): `async_job=True` returning
  `treaty.Job` with `App(jobs=)`, `job status` and `job cancel`; `config_write_scope=` with
  `--global`, `ctx.config_path`, and `ctx.write_config`; atomic writes for config,
  idempotency records, and `--output`; `async-job` and `config-write-scope` audit rules.
  Level 2 is complete
- 1.0 plan, reserved names: every flag name a 1.0 feature adds is one table in
  `_framework.py`; a field taking one fails registration, and passing one before it lands
  exits 2 `RESERVED_FLAG`
- 1.0 plan, response meta (`plans/1.0/01-response-meta.md`): `command`, `timestamp`,
  `schema_version`, `tool_version`, `cwd`, `trace_id`, `project_root`, and `retries` in
  `meta`; `schema_version=`, `compat=` with `--schema-version`, `--output-schema`,
  `--print-schema`, `project_root=`, `Retry` with `ctx.retry`, `treaty schema-lock`, and
  four audit rules. Breaking: `Envelope` takes a `treaty.Meta`; `App(version=)` is semver
- 1.0 plan, output data (`plans/1.0/05-output-data.md`): `treaty.Out` and `treaty.Binary`,
  arrays in `data` sorted (`sort_key=`, `ordered=`), absolute output paths, every output
  key required, `Flag(max_bytes=)` with `FIELD_TOO_LARGE`, `ctx.truncated`, LF-only
  streams, and `--stable-output`; audit rules `stable-order`, `binary-output`, and
  `field-limits`. Breaking: array order, relative `Path` output, `list[T] | None` in
  outputs, `Meta.request_id` and `timestamp` optional
- 1.0 plan, config layer (`plans/1.0/02-config-layer.md`): `App(settings=)` read from
  `<APP>_<FIELD>`, `--config` or the project and user TOML files, and defaults;
  `--context`, `--no-config`, `--show-config`, `--instance-id`; `meta.config_sources`,
  `meta.effective_config_hash`, `meta.context`, `meta.instance_id`; `App(init=)` with the
  `init` built-in and `INIT_REQUIRED`; audit rules `settings-declared`, `env-prefix`, and
  `init-isolated`. Breaking: `TREATY_FORMAT`, `TREATY_MAX_OUTPUT_BYTES`,
  `TREATY_MAX_STDIN_BYTES`, `TREATY_STATE_DIR` are now `<APP>_*` (`<APP>_STATE_DIR` names
  the directory itself), `Ctx.config` is private, and every config write is locked
- 1.0 plan, error contract (`plans/1.0/03-error-contract.md`): `retry_strategy` and exit
  code retry defaults, `treaty.already_exists` with `conflict_id`, validated
  `fix_command` with `fix_commands=` and `App(companions=)`, `treaty.Expired` with
  `refreshes_auth=`, `treaty.NetworkContext`, `ctx.lock` with `LOCK_HELD`,
  `<APP>_SESSION` idempotency keys, and `App.redirect` (exit 13, manifest `aliases`);
  audit rules `retry-hint`, `already-exists`, `delete-not-found`, `fix-declared`,
  `refresh-declared`, and `lock-declared`. Breaking: `RATE_LIMITED` without
  `retry_after_ms` and an invalid `fix_command` are `INVALID_EXIT`; a missing login is
  `UNAUTHENTICATED` and a missing scope at run time `PERMISSION_DENIED`; a read-only
  command's `TIMEOUT` is retryable. Help, version, schema, and manifest no longer fail on
  an invalid config file
- 1.0 plan, argument grammar (`plans/1.0/04-argument-grammar.md`): `pattern_type=` on
  `Flag` and `Arg`, `requires=` with `RequiredWhen`, `Excludes`, and `DefaultWhenAbsent`,
  `option_placement="strict"` forwarding the tail of argv, `from_stdin=` for `-`,
  `--validate-only` on every command, JSON5 in `--raw-payload` and `exec` lines with
  `corrected_input`, and `introduced_in=` and `deprecated=Deprecated(...)` with
  `treaty audit --baseline`; audit rules `id-pattern`, `conditional-rules`,
  `option-placement`, and `additive`. Breaking: `async def` handlers, hooks, and acquires
  are a `RegistrationError`; unparseable JSON input is `INVALID_JSON` instead of
  `ARG_ERROR` (or `DISPATCH_PARSE_ERROR` for an `exec` line); `validate_only` is a
  framework key; every manifest entry gains `option_placement`
- 1.0 plan, output security (`plans/1.0/07-output-security.md`): JWT, base64, and
  credential-named strings in `data` masked unless `--unmask`, `external=True` and
  `Out(external=True, high_entropy=...)` with `_source`/`_trusted` tags,
  `--no-injection-protection`, one `SECRET_NAME` rule and `scrub()` for logs and stderr;
  audit rules `external-data` and `high-entropy`. Breaking: masked `data` by default, the
  global names `unmask` and `no-injection-protection`, fields named `_source` or
  `_trusted`, more names inferred secret (`cookie`, a `pass` segment), and truncation
  warnings name `data.x` instead of `$.x`
- 1.0 plan, additional command declarations (`plans/1.0/08-declarations.md`):
  `subprocess=treaty.Subprocess(...)` (derived from `ctx.run` literals otherwise) with
  `SHELL_METACHARACTER` on declared fields, `platform=` and `required_tools=`,
  `filesystem_side_effects=[treaty.SideEffect(...)]`, `background=treaty.Background(...)`
  with `ctx.spawn`, `App(dependencies=[treaty.Dependency(...)])` at the manifest root,
  and the `doctor` and `cleanup` built-ins, which yield to an app command of the same
  name; audit rules `subprocess-declared`, `background-declared`, `fs-side-effects`,
  `declared-commands`, `required-tools`, and `builtin-shadowed`. Breaking: `os.system`,
  `os.popen`, and `shell=True` in a handler fail registration (the `no-shell` rule is
  gone); `gui_operations` requires `headless_behavior=`, and `"emit_in_output"` keeps
  today's behavior; every app lists `doctor` and `cleanup`, so every etag changes once
- 1.0 plan, session and process hygiene (`plans/1.0/09-session-hygiene.md`): children of
  `ctx.run` get `CI=1` and the update-notifier variables off a terminal and `LC_ALL=C`
  unless `preserve_locale=True`; `app.suppress_update_notifier`; `App(update_check=)`
  with `meta.update_available`, `--no-update-check`, and `<APP>_NO_UPDATE`; `--cwd`,
  `ctx.cwd`, and `CWD_CHANGED`; `ctx.tmp_dir`, `ctx.temp_file()`, `ctx.output_file()`
  with `data.cleanup`, `children.pids`, and `meta.session_tmp_dir`; descriptor 1 as a pipe
  with `context.text` and `treaty.intercept_stdout()`; `cache=treaty.CachePolicy(...)`
  with `ctx.cache`, `--no-cache`, and `--cache-ttl`; audit rules `preserve-locale`,
  `no-chdir`, and `cache-declared`. Behavior changes: children see `LC_ALL=C` and, off a
  terminal, `CI=1`; the scaffold's console script starts in `entry.py`
- 1.0 plan, network and filesystem utilities (`plans/1.0/10-network-and-fs.md`):
  `ctx.http` with proxy and CA bundle variables, `--proxy`, `--no-proxy`,
  `error.network_context`, HTTP 401/403/429/5xx mapping, and retries in `meta.retries`;
  `recursive_traversal=True` with `ctx.walk`, `SYMLINK_LOOP`, `DEPTH_EXCEEDED`,
  `--no-follow-symlinks`, and `--max-depth`; exports `HttpResponse` and `WalkEntry`; audit
  rules `http-client` and `recursive-traversal`. Behavior change: network commands list
  exit 12 among their implicit codes, and a field named `proxy`, `no-proxy`, `max-depth`,
  or `no-follow-symlinks` on an opting-in command was already refused
- 1.0 plan, logging, verbosity, and audit log (`plans/1.0/11-logging.md`): `--quiet`,
  `--verbose`, `--debug` (the framework's trace and every `logging` record, redacted),
  `ctx.progress`, `ctx.debug`, `ctx.log_error`, `--warnings-as-errors` with
  `WARNINGS_AS_ERRORS`, a rotated and redacted `audit.jsonl` (`treaty.AuditLog`,
  `<APP>_AUDIT_LOG`, `meta.audit_log_path`), the `audit-log` built-in, and the audit rule
  `log-not-print`. Behavior changes: off a terminal or under `CI`, `ctx.log` and stray
  `print()` text no longer reach stderr; every app writes an audit log under
  `XDG_DATA_HOME` unless `<APP>_AUDIT_LOG=off` or `App(audit_log=None)`. Since superseded:
  the audit log is opt-in (REQ-O-030), under `XDG_STATE_HOME`, 10 MiB × 5 rotated files
  (#71)
- 1.0 plan, output selection and streaming flags (`plans/1.0/12-output-selection.md`):
  `--fields`, `--stream` (a `STREAMING_NOT_SUPPORTED` warning on commands that cannot
  stream; `meta.pagination` on a stream's summary line), `--format id` with
  `id_field=`, `--heartbeat-interval` with `ctx.progress()` status lines on stderr,
  `--token-limit`, `--token-offset`, `--token-count`, `--tokenizer`, `app.tokenizer()`,
  the `treaty[tiktoken]` extra, and the audit rule `id-field`. Behavior change: an app
  with an output `id` field offers `--format id`, listed in the manifest's `format` enum
- 1.0 plan, built-in commands (`plans/1.0/13-built-ins.md`): `manifest --etag` with
  `meta.not_modified`; `doctor` checks for the state and config directories and
  `App(checks=[...])` with `treaty.Check` and `treaty.endpoint`; `status`; `cleanup
  --scope` and `--min-age` with bytes freed; `App(schema_changelog=)`, `changelog`, and
  `treaty changelog-add`; `generate-skills`; `mcp-validate` and `treaty-mcp --list-tools`;
  audit rules `doctor-fix` and `schema-changelog`. Behavior changes: every app has
  `status`, `generate-skills`, and `mcp-validate` (yielding to an app command), `cleanup`
  output has `cleaned` instead of `removed` and also removes declared `log` paths, and the
  scaffold's `status` command is `show`
- 1.0 plan, agent docs (`plans/1.0/14-agent-docs.md`): `treaty agents-md` and `treaty
  check-docs` (exit 81 `DOCS_OUT_OF_DATE`), a `cli-version` comment on AGENTS.md and
  `CONTEXT.md`, AGENTS.md and its check test from `treaty init`, and the check plus an
  install-twice step in CI. Behavior change: `--version --format plain` prints the bare
  version
- 1.0 plan, release readiness (`plans/1.0/15-release-readiness.md`), in-repo part:
  `docs/api.md` with a decision per public item, the `tests/test_public_api.py` snapshot,
  `CHANGELOG.md`, `docs/guide.md`, "Stability" and "Platforms" in the README. Breaking:
  `ExecArgs` unexported, `Ctx` run plumbing and seven `App` helpers private, and
  `framework_version` is treaty's version. The latest release is `1.0.0rc22`, tagged 2026-10-02. Open: the consumer ports,
  the rc soak, and the `1.0.0` tag, which need real consumers

- Shell completion: the `completion` built-in prints a static bash or zsh script
  generated from the manifest. Open: fish, and PowerShell for Windows

## 0.1.x: after the first minor release

0.1.0 shipped Level 1 and Level 2 of the spec (see `COMPLIANCE.md`). Still open:

- `CHANGELOG.md` and `docs/guide.md`: done in the 1.0 plan (15)

## 0.1.1: cloudfall adoption

Gaps found on 2026-09-25 by reading the first real consumer, `romamo/cloudfall`
(commit `b9677a6`): its argparse CLI, agent toolset, and MCP server declare every
operation three times, and none of them could be ported until these land. All four
landed the same day; what remains under each is follow-up work:

- **Custom scalars**: done, see above, including phase-1 checks for the `alphanumeric_id`,
  `uuid`, `semver`, and `url` presets
- **Typed resources**: done, see above, and `release(self)` on a resource class is called
  when the run ends by any exit, before `cleanup=` (1.0 plan 06)
- **Streaming handlers**: done, see above. Still open from it: streaming for
  mutating commands once the effect contract can name the event that carries `effect`
- **MCP adapter**: done, see above, as the `treaty-mcp` script rather than a `treaty`
  subcommand because a stdio server owns stdout. Still open from it: the manifest as an
  MCP resource, and progress notifications for streaming commands instead of buffering

## 0.2.0: level 2 coverage

Every remaining P0 requirement the kit cannot yet check. Each new declaration gets a
matching audit rule so adoption never requires reading the spec.

- Done in the 1.0 error contract: `ALREADY_EXISTS` returning the existing resource in
  `data` (REQ-C-028), and `REDIRECTED` exit `13` with `error.redirect` and manifest `aliases`

## 0.3.0: richer contracts

- The resource-id patterns of REQ-F-045 (`?`, `#`, encoded metacharacters) beyond the
  REQ-C-020 presets, which are all done
- Multi-step commands with a step manifest and `completed_steps` on timeout and
  cancellation (REQ-C-008): done in 1.0 plan 06, with `--resume-from`,
  `--rollback-on-failure`, and `treaty.Batch`
- Framework-managed locks with `retry_after_ms` (REQ-F-033): done, `ctx.lock`
- Dependency declarations and a `doctor` built-in (REQ-O-031): done, with custom and
  network checks in 1.0 plan 13
- Token budget flags and `--fields` (REQ-O-049, REQ-O-002): done in 1.0 plan 12 as
  `--token-limit`, `--token-offset`, `--token-count`, and `--tokenizer`, the spec's names

## Later

- **pydantic adapter** (`treaty[pydantic]`): a protocol seam in `_flags.py` and
  `_schema.py` so a `BaseModel` can serve as args or output type. First adapter to build
  when the extras are revisited
- **rich adapter** (`treaty[rich]`): terminal rendering only, never a `--format` value. Low value
- Windows CI: signals are POSIX-only in the tests; the daemon-thread timeout already works
  there
- Benchmark: `benchmark/README.md` compares argparse, click, and treaty builds of the same
  CLI on the spec harness (done 2026-09-24; group help scoped to its subtree and shared
  exit codes hoisted to a root table the same day; S6 to S8 added for hangs, lost
  responses, and lossy text; `ARG_ERROR.context.available` now lists invocations scoped
  to the group after S8 showed agents typing registry keys literally, which halved the
  S8 token cost)

## Non-goals

- Runtime dependencies in core
- Python below 3.14
- Exposing `argparse` or any parser objects in the public API
- Mounting `App` instances as sub-apps; the registry stays flat
