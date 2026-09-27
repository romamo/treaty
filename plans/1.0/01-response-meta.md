# 01: Response metadata

Size: L

Freezes the envelope's `meta`: every field an agent correlates, caches, or branches on
gets its final name, type, and producer before 1.0, and each command's contract version
can be read and pinned.

| Requirement | Priority | Now | Gap | API change |
|-------------|----------|-----|-----|------------|
| F-021 Data/meta separation | P1 | Partial | No warning for timestamps in `data`; not documented | Additive: `volatile-data` audit rule |
| F-022 Schema version in every response | P1 | Not started | Only the manifest has a version | Yes: `meta.schema_version`, `schema_version=` keyword |
| F-023 Tool version in every response | P1 | Not started | No `meta.tool_version` | Yes: `meta.tool_version`; `App(version=)` must be semver |
| F-024 Request ID and trace ID | P2 | Partial | No `trace_id`, `command`, `timestamp` | Yes: three `meta` fields |
| F-025 `TOOL_TRACE_ID` propagation | P2 | Not started | Not read, not in log lines | Yes: env var name and log line format |
| F-027 CWD in meta | P2 | Not started | No `meta.cwd` or `project_root` | Yes: `meta.cwd`, `project_root=` keyword, `ctx.project_root` |
| F-078 Retry count in meta | P2 | Not started | No retry machinery | Yes: `retry=` keyword, `ctx.retry`, two flags, `meta.retries` |
| O-013 `--output-schema` | P1 | Partial | No `--output-schema`, no `--print-schema` | Yes: two reserved flag names |
| O-014 `--schema-version` | P2 | Not started | No negotiation | Yes: `compat=` keyword, `--schema-version`, two codes |

## API impact

- **`Envelope` constructor** (exported): `duration_ms` and `request_id` move into a new
  frozen `treaty.Meta`; `Envelope(exit_code, data, error, meta, warnings, extra_meta)`.
  Breaking for code that builds envelopes by hand (see 01-D1)
- **Always-present `meta` keys**: `command`, `timestamp`, `schema_version`,
  `tool_version`, `cwd`, joining `exit_code`, `duration_ms`, `request_id`. Conditional:
  `trace_id`, `project_root`, `retries`. Additive for readers; each envelope grows about
  200 bytes, which the 1 MiB cap absorbs
- **`App(version=)`** is validated as semver at construction (`RegistrationError`), since
  `meta.tool_version` has a semver pattern in `response-envelope.json`. Breaking for apps
  with versions such as `"1.0"`
- **`App.command` keywords**: `schema_version="1.0"` (default, `MAJOR.MINOR`),
  `compat={}`, `project_root=()`, `retry=None`. All defaulted, so additive
- **New exports**: `Meta`, `Retry`, `SchemaVersion`. **`Ctx`**: `project_root`,
  `retry()`, `trace_id`
- **Reserved flag names**: `--output-schema` and `--schema-version` on every command,
  `--print-schema` at the root, `--retries` and `--retry-delay` on `retry=` commands. A
  field already named so fails registration through `framework_collisions`
- **Codes**: warning `SCHEMA_DEPRECATED`, error `SCHEMA_VERSION_UNSUPPORTED` and
  `TRACE_ID_INVALID` (exit 2), `ErrorDetail.retries_exhausted` (coordinate with 03).
  **Env**: `TOOL_TRACE_ID`
- **`--schema`** gains `schema_version` and `min_schema_version` per command. Neither is a
  `CommandEntry` key (`additionalProperties: false`), so the manifest is unchanged

## Design

### `Meta` and the run's identity (F-023, F-024, F-027)

`_Run.__init__` in `_app.py` captures `timestamp` (UTC, milliseconds, `Z`), `cwd`, and
`trace_id` once per process; exec lines share them and `request_id`, told apart by
`_line`. `_Run._envelope`, the one choke point, builds `Meta` from those plus the command
being answered: `_Run` gains `current: Command | None`, set by `_route` after resolution
and per line by `_exec_lines`. `Meta.to_json()` omits `None` fields, so `trace_id`,
`project_root`, and `retries` are absent rather than null (F-074).

- `tool_version` is `app.version`, the same value `version` returns, so it matches
  `--version` by construction. A `ToolVersion` value object in `_values.py` checks semver
- `cwd` is `PWD` when it names the same directory as `os.getcwd()`
  (`os.path.samefile`), else `os.getcwd()`: `pwd` prints the logical path, and on macOS
  `/tmp` would otherwise read `/private/tmp` (01-D4)
- `update_available` is never set, so "absent when no update" holds (01-D3)

### Schema version per command (F-022)

`schema_version: str = "1.0"` on `App.command`, parsed into `SchemaVersion(major, minor)`
(`^\d+\.\d+$`). `meta.schema_version` is the command's, or `ENVELOPE_SCHEMA_VERSION =
"1.0"` in `_envelope.py` when no command resolved (01-D2). `--schema` shows it.

The increment rule needs a baseline: `treaty schema-lock module:app` writes
`treaty-schema.lock` (version and output schema per command). Audit rule `schema-version`
diffs against it: a removed property, changed type, or newly required key without a major
bump is an error, an added property without a minor bump a warning; the fix names the
next version.

### Trace ID (F-024, F-025)

`TOOL_TRACE_ID` is read from the run's `env` (never `os.environ`, so `App.run` and MCP
callers control it); over 256 characters or with control characters, it exits 2 with
`TRACE_ID_INVALID` in the validation phase. `Processes` puts it in the base env next to
`child_settings`, so `ctx.run` passes it even when the caller's `env=` omits it.
`_Run._log_sink` adds `"trace_id"` to JSON records and `trace=<id>` to plain lines, as do
the crash traceback header and `hint:` lines. Audit log: not applicable until F-026.

### CWD and project root (F-027)

`project_root=(".git", "pyproject.toml")` on `App.command` names marker files. The
framework walks up from `meta.cwd` before the handler runs; the first directory holding a
marker becomes `meta.project_root` and `ctx.project_root: Path | None`. No marker: both
absent/`None`, exit 0. Audit rule `project-root`: an AST scan (`_scan.py`) for handlers
that call `Path.cwd()` or `os.getcwd()` and loop over `.parent`, fix
`project_root=("<marker>",)`.

### Retries (F-078)

`Retry(attempts=3, delay_ms=500, on=(ConnectionError, TimeoutError),
exhausted="UNAVAILABLE")` is a frozen dataclass; `exhausted` must be a declared exit code.
`retry=Retry(...)` adds `--retries N` and `--retry-delay DURATION` (`500ms`, `2s`) as
`FrameworkFlag` rows in `_framework.py`, overriding `attempts` and `delay_ms`.
`ctx.retry(fn)` calls `fn`, retries on `on`, and never sleeps past the command deadline
(the timeout bounds all attempts). Each retry increments a counter on `_Run`;
`meta.retries` appears when it is above 0. Exhaustion raises the `exhausted` code with
`retryable: false` and `retries_exhausted: N`. `ctx.http` (plan 10) uses `ctx.retry` when
the command declares `retry=`. Audit rule `retry-declared`: a handler with a
`time.sleep` inside a `try` in a loop, fix `retry=Retry(...)` and `ctx.retry`.

### `--output-schema` and `--print-schema` (O-013)

`--output-schema` joins `_FIXED_GLOBAL_FLAGS`; `_route` handles it next to `--schema`
and returns `command.output_schema` as `data` with `meta.schema_version`. `--print-schema`
is a root-only alias of `--schema`. "Stability tier per field" has no acceptance
criterion or schema field: not applicable.

### Version negotiation (O-014)

`compat={"1.4": to_v1}` maps an old version to a typed shim, `Callable[[Out], OldOut]`.
Registration checks that the parameter annotation is the output type, that each key's
major is below the current, and builds `OldOut`'s schema. `min_schema_version` is the
lowest key, or the current version. `--schema-version MAJOR` (global, every command;
JSON key `schema_version`) selects a shim, which runs on the handler's result before
`to_jsonable`; streams run it per event, list commands per page. Effects:

- An old major: `meta.schema_version` is the shim's key and `SCHEMA_DEPRECATED` is
  warned with `current_version` and `requested_version`
- Below the minimum or above the current: exit 2, `SCHEMA_VERSION_UNSUPPORTED`, context
  `min_schema_version` and `schema_version`; `--output-schema` prints `OldOut`'s schema

### Data carries no volatile fields (F-021)

Framework values are already in `meta`. Audit rule `volatile-data` flags output fields
typed `datetime` or named like `fetched_at`, `generated_at`, `request_id`, `duration`;
fix: drop it (`meta.timestamp` has the time) or mark it `Out(volatile=True)` (plan 05).
An audit rule, not a registration warning, because registration has no warning channel.
The README states that `data` is safe to cache and diff and `meta` is not.

## Decisions

| ID | Question | Recommendation |
|----|----------|----------------|
| 01-D1 | Break the exported `Envelope` constructor for a `Meta` value | Yes: hand-built envelopes are rare, and a frozen `Meta` is the only way to freeze the shape |
| 01-D2 | `meta.command` and `schema_version` when no command resolved (unknown command, parse error before routing) | `command` is the app name; `schema_version` is `ENVELOPE_SCHEMA_VERSION` |
| 01-D3 | Add an update-check hook for `update_available` | No: no release channel exists; absence meets F-023 and F-029, and a hook stays additive |
| 01-D4 | `meta.cwd` logical (`PWD`) or physical path | Logical when it names the same directory, since the criterion is "matches `pwd`" |
| 01-D5 | Literal `TOOL_TRACE_ID` or `<APP>_TRACE_ID` | Literal: a trace crosses tools, so the name cannot depend on one of them |

## Tasks

- [x] `Meta`, `ToolVersion`; `command`, `timestamp`, `tool_version`, `cwd` on every envelope.
  `meta.command` is the manifest key (`deploy.rollback`)
- [x] `schema_version=` and `meta.schema_version`; `--schema` shows it
- [x] `treaty schema-lock` and the `schema-version` audit rule. A key that became required
  is additive for a reader of output, not breaking; one that became optional breaks it.
  No lock file, no findings
- [x] `TOOL_TRACE_ID`: `meta.trace_id`, validation, children, log lines. Children inherit
  it through the run's env, which already seeds `Processes`
- [x] `project_root=`, `ctx.project_root`, `meta.project_root`, `project-root` rule
- [x] `Retry`, `ctx.retry`, `--retries`, `--retry-delay`, `meta.retries`, `retry-declared`
  rule. `Retry(retries=3)` counts retries after the first attempt, matching `--retries`,
  instead of `attempts`; a retry whose delay would pass the deadline is not made. With
  `--retries 0` the error keeps the code's own `retryable`
- [x] `--output-schema` and `--print-schema`. `--print-schema` is an alias on every path,
  not only the root: the name is reserved, so it shadows nothing
- [x] `compat=`, `--schema-version`, `SCHEMA_DEPRECATED`, `SCHEMA_VERSION_UNSUPPORTED`.
  `--schema-version` is a root flag only (the manifest forbids repeating it per command);
  JSON callers pass `schema_version` on any command
- [x] `volatile-data` audit rule; README section on `data` versus `meta`. The fix says to
  drop the field; `Out(volatile=True)` is left to 05
- [x] MCP input schema and exec keys for the new flags. No new conformance profile probes:
  the kit has no check for these fields
- [x] Update COMPLIANCE.md rows, README, HANDOFF, ROADMAP. No `CHANGELOG.md` exists yet;
  the breaking changes are listed in ROADMAP for 15 to collect
