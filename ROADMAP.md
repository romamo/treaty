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

## 0.1.x: after the first minor release

0.1.0 shipped Level 1 and Level 2 of the spec (see `COMPLIANCE.md`). Still open:

- `CHANGELOG.md`
- `docs/guide.md`: the judgement calls the audit cannot make (naming paths, what belongs in
  `error.context`, when a failure deserves its own exit code); short, because every
  mechanical step is now an audit rule

## 0.1.1: cloudfall adoption

Gaps found on 2026-09-25 by reading the first real consumer, `romamo/cloudfall`
(commit `b9677a6`): its argparse CLI, agent toolset, and MCP server declare every
operation three times, and none of them could be ported until these land. All four
landed the same day; what remains under each is follow-up work:

- **Custom scalars**: done, see above, including phase-1 checks for the `alphanumeric_id`,
  `uuid`, `semver`, and `url` presets
- **Typed resources**: done, see above. Still open from it: a `release` counterpart to
  `acquire` for resources that hold a lock or a connection, run after the handler and on
  cancellation alongside `cleanup=`
- **Streaming handlers**: done, see above. Still open from it: streaming for
  mutating commands once the effect contract can name the event that carries `effect`
- **MCP adapter**: done, see above, as the `treaty-mcp` script rather than a `treaty`
  subcommand because a stdio server owns stdout. Still open from it: the manifest as an
  MCP resource, and progress notifications for streaming commands instead of buffering

## 0.2.0: level 2 coverage

Every remaining P0 requirement the kit cannot yet check. Each new declaration gets a
matching audit rule so adoption never requires reading the spec.

- `ALREADY_EXISTS` returning the existing resource in `data` (REQ-C-028)
- `REDIRECTED` exit `13` with `error.redirect` for renamed commands and `aliases` in the
  manifest

## 0.3.0: richer contracts

- The resource-id patterns of REQ-F-045 (`?`, `#`, encoded metacharacters) beyond the
  REQ-C-020 presets, which are all done
- Conditional argument rules (REQ-C-026) and `option_placement: strict` for commands that
  forward trailing arguments (REQ-C-027)
- Multi-step commands with a step manifest and `completed_steps` on timeout and
  cancellation (REQ-C-008)
- Framework-managed locks with `retry_after_ms` (REQ-F-033)
- `--validate-only` (REQ-O-009) and safe-default dry run with `--live` (REQ-O-048)
- Dependency declarations and a `doctor` built-in (REQ-O-031)
- Token budget flags `--max-tokens` and `--fields` (REQ-O-049)

## Later

- **pydantic adapter** (`treaty[pydantic]`): a protocol seam in `_flags.py` and
  `_schema.py` so a `BaseModel` can serve as args or output type. First adapter to build
  when the extras are revisited
- **rich adapter** (`treaty[rich]`): terminal rendering only, never a `--format` value. Low value
- Shell completion generated from the manifest
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
