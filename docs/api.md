# Public API

What treaty 1.0 freezes. Everything on this page is covered by semantic versioning from
1.0 on (see "Stability" in the [README](../README.md#stability)); anything not on it,
including every `treaty._*` module, may change in any release.

`tests/test_public_api.py` holds the same inventory as a snapshot
(`tests/data/public_api.json`): a change to any item fails CI until the snapshot is
regenerated on purpose with `uv run python tests/test_public_api.py > tests/data/public_api.json`
and the change has a `CHANGELOG.md` entry.

Decision key: **keep** (frozen as is), **private** (moved out of the public surface in the
review), **rename**, **remove**.

## Exports from `treaty`

### Building an app

| Name | Kind | Decision | Note |
|------|------|----------|------|
| `App` | class | keep | Keywords and public methods below |
| `Group` | class | keep | `command(name, **App.command keywords)`, `group` |
| `Ctx` | class | keep | Handler context; fields and methods below |
| `Flag`, `Arg` | field markers | keep | Keywords below |
| `Out` | output field marker | keep | Settles 07-D4 and X1: one marker, `Out`, not `Field`, so it cannot be confused with `dataclasses.field` |
| `NoArgs` | class | keep | Arguments type of a command with none |
| `Format` | enum | keep | `PLAIN`, `JSON`, `JSONL`, `CSV`, `TSV`, `YAML`, `MARKDOWN`, `ID` |
| `DangerLevel` | enum | keep | `SAFE`, `MUTATING`, `DESTRUCTIVE` |
| `Example` | class | keep | Also accepted as `(description, command)` tuples |
| `Renderer` | type alias | keep | `Callable[[Any], str]` |
| `table` | function | keep | Delimited renderer factory |
| `ScalarSpec` | class | keep | Returned by `app.scalar` |
| `ExecArgs` | class | private | Arguments of the `exec` built-in; no app constructs it |

### Declarations

| Name | Decision | Note |
|------|----------|------|
| `Affects` | keep | `would_affect` of a dry run |
| `Background`, `Subprocess`, `SideEffect` | keep | `background=`, `subprocess=`, `filesystem_side_effects=` |
| `CachePolicy` | keep | `cache=` |
| `Check`, `Dependency`, `endpoint` | keep | `App(checks=)`, `App(dependencies=)` |
| `Deprecated` | keep | `deprecated=` on a command or `Flag` |
| `DefaultWhenAbsent`, `Excludes`, `RequiredWhen` | keep | `requires=` |
| `Retry`, `RetryStrategy` | keep | `retry=`, exit code retry hints |
| `Rollback`, `StepName` | keep | `rollback=`, `steps=` |
| `Timeout` | keep | `ctx.timeout` |
| `AuditLog` | keep | `App(audit_log=)` |
| `UpdateCheck` | keep | `App(update_check=)` base class |
| `Init` | keep | `App(init=)` base class |
| `Credentials`, `Expired` | keep | `App(credentials=)`, `refreshes_auth=` |
| `Job`, `JobStore` | keep | `async_job=True`, `App(jobs=)` |

### Results and values

| Name | Decision | Note |
|------|----------|------|
| `Batch`, `Item`, `ItemError` | keep | Per-item results with exit 3 |
| `Binary` | keep | Base64 output with `content_type` |
| `Page`, `PageRequest` | keep | `paginated=True` |
| `Completed`, `Spawned` | keep | Returned by `ctx.run`, `ctx.pipeline`, `ctx.spawn` |
| `HttpResponse` | keep | Returned by `ctx.http` |
| `WalkEntry` | keep | Yielded by `ctx.walk` |
| `CommandPath`, `ExitCode`, `ExitCodeName`, `SchemaVersion`, `Scope` | keep | Value objects in public signatures (`App.commands` keys, `CliExit.name`); the plan asked whether `ExitCodeName` is internal: it is what `CliExit` carries, so it stays |
| `intercept_stdout` | keep | Captures descriptor 1 around a third-party call |
| `already_exists` | keep | Create-or-get `CliExit` |

### Errors and exits

| Name | Decision | Note |
|------|----------|------|
| `Exit` | keep | `Exit.NOT_FOUND(...)` factory for `CliExit` |
| `CliExit` | keep | Raised by handlers |
| `ParseError` | keep | Raised by argument validation |
| `TreatyError`, `RegistrationError`, `SchemaError` | keep | `SchemaError` is raised to app authors at registration (an unserializable output type), so it stays public |
| `FrameworkCode` | keep | Exit codes 0 to 13, below |
| `ExitCodeEntry`, `SideEffects` | keep | `app.exit_code` and the manifest's exit code table |

### Wire types

| Name | Decision | Note |
|------|----------|------|
| `Envelope`, `Meta`, `ErrorDetail`, `WarningDetail`, `NetworkContext`, `Redirect` | keep | The response shape as dataclasses; fields below |

## `App`

Keywords: `name`, `version` (semver), `description`, `state`, `default_timeout`,
`max_output_bytes`, `max_stdin_bytes`, `state_dir`, `enable_exec`, `credentials`, `jobs`,
`settings`, `init`, `companions`, `dependencies`, `checks`, `update_check`, `audit_log`,
`schema_changelog`. All keep.

Public methods (keep): `command`, `group`, `redirect`, `exit_code`, `scalar`, `format`,
`tokenizer`, `suppress_update_notifier`, `run`, `main`, `call`, `manifest`,
`environment`, and the read-only properties `commands`, `builtins`, `formats`,
`shadowed_builtins`. The `Command` objects `commands` maps to are opaque: their
attributes are not covered.

Made private in the review: `renderer`, `moved`, `check_fixes`, `fix_problem`,
`named_commands`, `effective_timeout`, `silence_notifiers`. They were run-path helpers
that no app or example called.

`App.command` keywords (keep): `path`, `description`, `danger_level`, `required_scopes`,
`exit_codes`, `examples`, `has_network_io`, `timeout`, `supports_raw_payload`, `cleanup`,
`renderers`, `streaming`, `safe_default`, `gui_operations`, `headless_behavior`,
`interactive`, `editor_alternatives`, `paginated`, `default_limit`, `cursor_check`,
`heartbeat`, `stdin_input`, `output_file`, `requires_auth`, `auth`, `token_env_vars`,
`async_job`, `config_write_scope`, `schema_version`, `compat`, `project_root`, `retry`,
`sort_key`, `ordered`, `fix_commands`, `refreshes_auth`, `requires`, `option_placement`,
`introduced_in`, `deprecated`, `steps`, `resumable`, `rollback`, `external`,
`subprocess`, `platform`, `required_tools`, `filesystem_side_effects`, `background`,
`preserve_locale`, `cache`, `recursive_traversal`, `id_field`. `Group.command` takes the
same keywords.

`Flag`: `description`, `default`, `short`, `pattern`, `secret`, `multiline`, `max_bytes`,
`pattern_type`, `from_stdin`, `deprecated`. `Arg`: `description`, `pattern`, `secret`,
`pattern_type`, `from_stdin`. `Out`: `default`, `default_factory`, `sort_key`, `ordered`,
`volatile`, `high_entropy`, `external`. All keep.

## `Ctx`

Fields (keep): `app_name`, `version`, `mode`, `request_id`, `env`, `state`, `timeout`,
`color`, `headless`, `cwd`, `idempotency_key`, `stdin_text`, `page`, `token`,
`trace_id`, `project_root`.

Methods and properties (keep): `log`, `warn`, `debug`, `progress`, `log_error`, `run`,
`pipeline`, `spawn`, `open_url`, `prompt`, `confirm`, `edit`, `retry`, `lock`, `step`,
`http`, `walk`, `cache`, `tmp_dir`, `temp_file`, `output_file`, `truncated`,
`config_path`, `write_config`.

Made private in the review: `log_sink`, `warn_sink`, `processes`, `prompter`, `retrier`,
`locks`, `teardown`, `steps`, `session` (the run's plumbing, reached through the methods
above). `Ctx.config` was already private (`_config_file`, workstream 02).

## Wire contract

- Envelope keys: `ok`, `data`, `error`, `warnings`, `meta`
- `meta`: `duration_ms`, `request_id`, `command`, `timestamp`, `schema_version`,
  `tool_version`, `cwd`, `trace_id`, `project_root`, `retries`, plus the conditional keys
  the README lists (`pagination`, `config_sources`, `update_available`, `not_modified`,
  and the like); optional keys are absent, never null
- `error`: `code`, `message`, `retryable`, `detail`, `cause`, `context`, `suggestion`,
  `fix_command`, `retry_after_ms`, `fix_required`, `phase`, `errors`, `alternatives`,
  `hint`, `auth_methods`, `retries_exhausted`, `retry_strategy`, `conflict_id`,
  `refresh_command`, `expires_at`, `required_permission`, `network_context`, `redirect`,
  `corrected_input`
- `warnings[]`: `code`, `message`, `context`
- Manifest root: `schema_version` (`"3.0"`, the spec's `ManifestResponse`),
  `framework_version`, `etag`, `flags`, `exit_codes`, `commands`, and `dependencies` when
  declared
- `framework_version` is treaty's version; the app's version is `meta.tool_version`. Before
  the review it carried the app's version

Versioning: the envelope and manifest follow the spec schemas they validate against. A new
optional key keeps the contract; removing a key or changing what one means is a major
release. `meta.schema_version` stays what workstream 01 made it, the version of one
command's `data` contract, rather than a second envelope-wide number (a deviation from
the plan's "`meta.schema_version` becomes `1`").

## Exit codes

`FrameworkCode`: `SUCCESS` 0, `GENERAL_ERROR` 1, `ARG_ERROR` 2, `PARTIAL_FAILURE` 3,
`PRECONDITION` 4, `NOT_FOUND` 5, `CONFLICT` 6, `PERMISSION_DENIED` 7, `AUTH_REQUIRED` 8,
`PAYMENT_REQUIRED` 9, `TIMEOUT` 10, `RATE_LIMITED` 11, `UNAVAILABLE` 12, `REDIRECTED` 13;
signals 130, 141, 143. Apps declare their own codes in 79 to 125 with `app.exit_code`.

Framework `error.code` and warning codes are frozen by name: an agent branches on them.
New codes may be added in a minor release.

## Environment variables

Prefixed (`<APP>_<KEY>`): `FORMAT`, `MAX_OUTPUT_BYTES`, `MAX_STDIN_BYTES`, `STATE_DIR`,
`CONFIG`, `CONTEXT`, `INSTANCE_ID`, `SESSION`, `NO_UPDATE`, `AUDIT_LOG`, plus one per
`App(settings=)` field and secret flag.

Unprefixed conventions: `CI`, `NO_COLOR`, `TERM`, `HOME`, `USER`, `PATH`, `SHELL`, `PWD`,
`XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `XDG_STATE_HOME`, the proxy
variables in both cases, `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE`, `GITHUB_ACTIONS`,
`JENKINS_URL`, and `TOOL_TRACE_ID`. The `treaty` CLI itself also reads `TREATY_SPEC_DIR`.

## Naming questions settled here

| ID | Question | Decision |
|----|----------|----------|
| 07-D4, X1 | Output field marker: `Field` or `Out` | `Out` (landed with 05; 07 extended it) |
| 13-D1 | Built-in name rule | Always registered and yielding: an app command of the same name wins, and the `builtin-shadowed` audit rule reports it (landed with 08 and 13) |
| 08-D1, 13-D2 | `side-effects` built-in or `status --show-side-effects` | `status --show-side-effects`, as C-011 and O-028 name it (landed with 13) |
| 02 | `Ctx.config` private | Yes, `Ctx._config_file` (landed with 02) |
| 15 | `framework_version` | treaty's version, fixed in the review |
