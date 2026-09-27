# CLI Agent Spec compliance

Status of treaty against the 159 requirements of the
[CLI Agent Spec](../cli-agent-ergonomics/requirements/index.md), assessed 2026-09-27 at
commit `458bab5` (0.0.6), and updated by hand as the Level 2 plans land (01, output
hygiene; 02, validation phase; 04, subprocess API; 05, declarations).

Each requirement was checked against its acceptance criteria by reading the source and
tests and by probing the example apps. This is stricter than the conformance kit, which
passes 12 of 12 checks at levels 1 to 3 because its checks cover a narrower surface.

Score weights: Done 1, Partial 0.5, Not started 0.

## By level

| Scope | Size | Done | Partial | Not started | Score |
|-------|------|------|---------|-------------|-------|
| Level 1: agent-safe basics | 12 | 11 | 0 | 1 | **92%** |
| Level 2: every P0 (includes Level 1) | 51 | 32 | 10 | 9 | **73%** |
| Level 3: full spec | 159 | 42 | 41 | 76 | **39%** |

## By tier

| Scope | Size | Done | Partial | Not started | Score |
|-------|------|------|---------|-------------|-------|
| Framework-automatic (F) | 79 | 29 | 23 | 27 | **51%** |
| Command contract (C) | 30 | 9 | 8 | 13 | **43%** |
| Opt-in (O) | 50 | 4 | 10 | 36 | **18%** |

## Open mandatory requirements

Level 2 (every P0) is the level the spec calls agent-reliable. These 19 requirements
stand between treaty and a Level 2 claim; the Level 1 ones come first.

| ID | Title | Priority | Level | Status | Notes |
|----|-------|----------|-------|--------|-------|
| [REQ-F-009](../cli-agent-ergonomics/requirements/f-009-non-interactive-mode-auto-detection.md) | Non-Interactive Mode Auto-Detection | P0 | 1 | Not started | No prompt API, exit 4, or INPUT_REQUIRED; `input()` crashes the handler (exit 1) |
| [REQ-F-011](../cli-agent-ergonomics/requirements/f-011-default-timeout-per-command.md) | Default Timeout Per Command | P0 | 2 | Partial | 60 s default deadline; streaming commands default to no timeout |
| [REQ-F-014](../cli-agent-ergonomics/requirements/f-014-sigpipe-handler-installation.md) | SIGPIPE Handler Installation | P0 | 2 | Partial | Exits 141 (OUTPUT_CLOSED); spec requires 0 |
| [REQ-F-018](../cli-agent-ergonomics/requirements/f-018-pagination-metadata-on-list-commands.md) | Pagination Metadata on List Commands | P0 | 2 | Not started | No `meta.pagination`, `--cursor`, or `next_cursor` (ROADMAP 0.2.0) |
| [REQ-F-019](../cli-agent-ergonomics/requirements/f-019-default-output-limit.md) | Default Output Limit | P0 | 2 | Not started | No default item limit or `--limit`; only the 1 MiB byte cap |
| [REQ-F-047](../cli-agent-ergonomics/requirements/f-047-repl-mode-prohibition-in-non-tty-context.md) | REPL Mode Prohibition in Non-TTY Context | P0 | 2 | Partial | No-args run shows help; `exec` refuses a TTY stdin; `input()` is not intercepted |
| [REQ-F-052](../cli-agent-ergonomics/requirements/f-052-response-size-hard-cap-with-truncation-indicator.md) | Response Size Hard Cap with Truncation Indicator | P0 | 2 | Partial | 1 MiB cap, `--max-output`, `meta.truncated`; hint is prose, not a runnable command |
| [REQ-F-053](../cli-agent-ergonomics/requirements/f-053-stdout-unbuffering-in-non-tty-mode.md) | Stdout Unbuffering in Non-TTY Mode | P0 | 2 | Partial | Each envelope flushed; no heartbeat, no `PYTHONUNBUFFERED` |
| [REQ-F-054](../cli-agent-ergonomics/requirements/f-054-stdin-payload-size-cap-with-input-file-fallback.md) | Stdin Payload Size Cap with --input-file Fallback | P0 | 2 | Partial | 64 KiB cap and `--input-file` exist on `exec` only |
| [REQ-F-055](../cli-agent-ergonomics/requirements/f-055-editor-and-visual-no-op-in-non-tty-mode.md) | $EDITOR and $VISUAL No-Op in Non-TTY Mode | P0 | 2 | Partial | Off a terminal, children get `EDITOR`, `VISUAL`, `GIT_EDITOR`=`true`; no in-process editor interception with exit 4 yet |
| [REQ-C-005](../cli-agent-ergonomics/requirements/c-005-interactive-commands-must-support-yes-non-interact.md) | Interactive Commands Must Support --yes / --non-interactive | P0 | 2 | Not started | No `interactive=`, `--yes`, or `--non-interactive` (ROADMAP 0.2.0) |
| [REQ-C-021](../cli-agent-ergonomics/requirements/c-021-auth-commands-declare-headless-mode-support.md) | Auth Commands Declare Headless Mode Support | P0 | 2 | Not started | No headless auth declaration |
| [REQ-C-022](../cli-agent-ergonomics/requirements/c-022-async-commands-declare-job-descriptor-schema.md) | Async Commands Declare Job Descriptor Schema | P0 | 2 | Not started | No async job descriptor |
| [REQ-C-025](../cli-agent-ergonomics/requirements/c-025-config-writing-commands-declare-write-scope.md) | Config-Writing Commands Declare Write Scope | P0 | 2 | Not started | No config write scope |
| [REQ-C-029](../cli-agent-ergonomics/requirements/c-029-command-declares-required-scopes.md) | Command Declares Required Scopes | P0 | 2 | Partial | `required_scopes` in every schema, `[]` by default; never required |
| [REQ-O-001](../cli-agent-ergonomics/requirements/o-001-output-format-flag.md) | --format Output Format Flag | P0 | 2 | Partial | `--format` with json, plain, and registered formats; no `jsonl` |
| [REQ-O-003](../cli-agent-ergonomics/requirements/o-003-limit-and-cursor-pagination-flags.md) | --limit and --cursor Pagination Flags | P0 | 2 | Not started | No `--limit`/`--cursor` (ROADMAP 0.2.0) |
| [REQ-O-033](../cli-agent-ergonomics/requirements/o-033-headless-and-token-env-var-flags-for-auth-commands.md) | --headless and --token-env-var Flags for Auth Commands | P0 | 2 | Not started | No headless auth flags |
| [REQ-O-047](../cli-agent-ergonomics/requirements/o-047-tool-check-permissions-built-in-command.md) | tool check-permissions Built-In Command | P0 | 2 | Partial | `required_scopes` declared; no `check-permissions` or over-privilege warning |

## Framework-automatic

| ID | Title | Priority | Level | Status | Notes |
|----|-------|----------|-------|--------|-------|
| [REQ-F-001](../cli-agent-ergonomics/requirements/f-001-standard-exit-code-table.md) | Standard Exit Code Table | P0 | 1 | Done | FrameworkCode table 0-13 plus signal codes; undeclared exits become UNDECLARED_EXIT_CODE |
| [REQ-F-002](../cli-agent-ergonomics/requirements/f-002-exit-code-2-reserved-for-validation-failures.md) | Exit Code 2 Reserved for Validation Failures | P0 | 1 | Done | Exit 2 only from phase 1; a handler's `ParseError` or `Exit.ARG_ERROR` is `VALIDATION_AFTER_START` (exit 1); the confirmation preview runs the handler in dry-run mode only |
| [REQ-F-003](../cli-agent-ergonomics/requirements/f-003-json-output-mode-auto-activation.md) | JSON Output Mode Auto-Activation | P0 | 1 | Done | Non-TTY stdout or `CI` selects JSON |
| [REQ-F-004](../cli-agent-ergonomics/requirements/f-004-consistent-json-response-envelope.md) | Consistent JSON Response Envelope | P0 | 1 | Done | Envelope with ok, data, error, warnings, meta on every exit |
| [REQ-F-005](../cli-agent-ergonomics/requirements/f-005-locale-invariant-serialization.md) | Locale-Invariant Serialization | P0 | 2 | Done | ISO 8601 `datetime`/`date`/`time` (`Z` for UTC), `Decimal` as text; naive datetimes refused; byte-identical across locales |
| [REQ-F-006](../cli-agent-ergonomics/requirements/f-006-stdout-stderr-stream-enforcement.md) | Stdout/Stderr Stream Enforcement | P0 | 1 | Done | Stray `print()` goes to stderr with a `THIRD_PARTY_STDOUT` warning; `ctx.log` writes stderr; tracebacks only on stderr |
| [REQ-F-007](../cli-agent-ergonomics/requirements/f-007-ansi-color-code-suppression.md) | ANSI/Color Code Suppression | P0 | 1 | Done | Every JSON string loses ANSI escapes and carriage returns, error messages included |
| [REQ-F-008](../cli-agent-ergonomics/requirements/f-008-no-color-and-ci-environment-detection.md) | NO_COLOR and CI Environment Detection | P0 | 1 | Done | `NO_COLOR` (even empty), `CI`, `GITHUB_ACTIONS`, `JENKINS_URL`, `TERM=dumb` turn `ctx.color` off; `App.main()` sets `NO_COLOR=1` for children |
| [REQ-F-009](../cli-agent-ergonomics/requirements/f-009-non-interactive-mode-auto-detection.md) | Non-Interactive Mode Auto-Detection | P0 | 1 | Not started | No prompt API, exit 4, or INPUT_REQUIRED; `input()` crashes the handler (exit 1) |
| [REQ-F-010](../cli-agent-ergonomics/requirements/f-010-pager-suppression.md) | Pager Suppression | P0 | 1 | Done | `App.main()` sets `PAGER=cat` and `GIT_PAGER=cat` for every child; treaty never pages |
| [REQ-F-011](../cli-agent-ergonomics/requirements/f-011-default-timeout-per-command.md) | Default Timeout Per Command | P0 | 2 | Partial | 60 s default deadline; streaming commands default to no timeout |
| [REQ-F-012](../cli-agent-ergonomics/requirements/f-012-timeout-exit-code-and-json-error.md) | Timeout Exit Code and JSON Error | P0 | 2 | Done | TIMEOUT, exit 10, `duration_ms` |
| [REQ-F-013](../cli-agent-ergonomics/requirements/f-013-sigterm-handler-installation.md) | SIGTERM Handler Installation | P0 | 2 | Done | SIGTERM gives a CANCELLED envelope, exit 143, cleanup runs |
| [REQ-F-014](../cli-agent-ergonomics/requirements/f-014-sigpipe-handler-installation.md) | SIGPIPE Handler Installation | P0 | 2 | Partial | Exits 141 (OUTPUT_CLOSED); spec requires 0 |
| [REQ-F-015](../cli-agent-ergonomics/requirements/f-015-validate-before-execute-phase-order.md) | Validate-Before-Execute Phase Order | P0 | 2 | Done | Field errors and the args `__post_init__` (cross-field) errors collected in one run before the handler; no hooks to misorder |
| [REQ-F-016](../cli-agent-ergonomics/requirements/f-016-utf-8-sanitization-before-serialization.md) | UTF-8 Sanitization Before Serialization | P1 | 3 | Done | JSON strings: null bytes and lone surrogates become U+FFFD; bytes output is refused as INVALID_OUTPUT |
| [REQ-F-017](../cli-agent-ergonomics/requirements/f-017-binary-field-base64-encoding.md) | Binary Field Base64 Encoding | P1 | 3 | Not started | Returning `bytes` gives INVALID_OUTPUT; no base64 wrapper with `size_bytes` |
| [REQ-F-018](../cli-agent-ergonomics/requirements/f-018-pagination-metadata-on-list-commands.md) | Pagination Metadata on List Commands | P0 | 2 | Not started | No `meta.pagination`, `--cursor`, or `next_cursor` (ROADMAP 0.2.0) |
| [REQ-F-019](../cli-agent-ergonomics/requirements/f-019-default-output-limit.md) | Default Output Limit | P0 | 2 | Not started | No default item limit or `--limit`; only the 1 MiB byte cap |
| [REQ-F-020](../cli-agent-ergonomics/requirements/f-020-stable-array-sorting-in-json-output.md) | Stable Array Sorting in JSON Output | P2 | 3 | Partial | Object keys sorted; arrays are not |
| [REQ-F-021](../cli-agent-ergonomics/requirements/f-021-data-meta-separation-in-response-envelope.md) | Data/Meta Separation in Response Envelope | P1 | 3 | Partial | Volatile fields live only in meta; no warning for timestamps in data |
| [REQ-F-022](../cli-agent-ergonomics/requirements/f-022-schema-version-in-every-response.md) | Schema Version in Every Response | P1 | 3 | Not started | `schema_version` only in the manifest, not in response meta (ROADMAP 0.1.0) |
| [REQ-F-023](../cli-agent-ergonomics/requirements/f-023-tool-version-in-every-response.md) | Tool Version in Every Response | P1 | 3 | Not started | No `meta.tool_version` |
| [REQ-F-024](../cli-agent-ergonomics/requirements/f-024-request-id-and-trace-id-in-every-response.md) | Request ID and Trace ID in Every Response | P2 | 3 | Partial | `meta.request_id` present; no `trace_id`, `meta.command`, or `meta.timestamp` |
| [REQ-F-025](../cli-agent-ergonomics/requirements/f-025-tool-trace-id-environment-variable-propagation.md) | TOOL_TRACE_ID Environment Variable Propagation | P2 | 3 | Not started | `TOOL_TRACE_ID` not propagated |
| [REQ-F-026](../cli-agent-ergonomics/requirements/f-026-append-only-audit-log.md) | Append-Only Audit Log | P2 | 3 | Not started | No append-only audit log (`treaty audit` is a static linter) |
| [REQ-F-027](../cli-agent-ergonomics/requirements/f-027-cwd-in-response-meta.md) | CWD in Response Meta | P2 | 3 | Not started | No `meta.cwd` or `project_root` |
| [REQ-F-028](../cli-agent-ergonomics/requirements/f-028-config-source-tracking-in-response-meta.md) | Config Source Tracking in Response Meta | P1 | 3 | Not started | No config layer, so no `meta.config_sources` |
| [REQ-F-029](../cli-agent-ergonomics/requirements/f-029-auto-update-suppression-in-non-interactive-mode.md) | Auto-Update Suppression in Non-Interactive Mode | P1 | 3 | Partial | Holds only because treaty never checks for updates; no suppression hook for app authors |
| [REQ-F-030](../cli-agent-ergonomics/requirements/f-030-child-process-session-tracking.md) | Child Process Session Tracking | P2 | 3 | Partial | `ctx.run` children are tracked and stopped on signal or timeout; no session tracking file |
| [REQ-F-031](../cli-agent-ergonomics/requirements/f-031-sigterm-forwarding-to-tracked-children.md) | SIGTERM Forwarding to Tracked Children | P2 | 3 | Done | SIGTERM or SIGINT sends SIGTERM to each tracked child's process group, SIGKILL after 2 s, before the `CANCELLED` envelope |
| [REQ-F-032](../cli-agent-ergonomics/requirements/f-032-session-scoped-temp-directory.md) | Session-Scoped Temp Directory | P2 | 3 | Not started | No session-scoped temp directory |
| [REQ-F-033](../cli-agent-ergonomics/requirements/f-033-lock-acquisition-with-timeout-and-retry-after-ms.md) | Lock Acquisition with Timeout and retry_after_ms | P2 | 3 | Not started | No public lock API with `retry_after_ms` (ROADMAP 0.3.0) |
| [REQ-F-034](../cli-agent-ergonomics/requirements/f-034-secret-field-auto-redaction-in-logs.md) | Secret Field Auto-Redaction in Logs | P1 | 3 | Partial | Secret args redacted in errors and tracebacks; response fields are not redacted by name |
| [REQ-F-035](../cli-agent-ergonomics/requirements/f-035-external-data-trust-tagging.md) | External Data Trust Tagging | P1 | 3 | Not started | No `_trusted` or `_source` tagging |
| [REQ-F-036](../cli-agent-ergonomics/requirements/f-036-http-client-proxy-environment-variable-compliance.md) | HTTP Client Proxy Environment Variable Compliance | P1 | 3 | Not started | No framework HTTP client, so no proxy or CA handling |
| [REQ-F-037](../cli-agent-ergonomics/requirements/f-037-network-error-context-block.md) | Network Error Context Block | P1 | 3 | Not started | No `error.network_context` |
| [REQ-F-038](../cli-agent-ergonomics/requirements/f-038-verbosity-auto-quiet-in-non-tty-context.md) | Verbosity Auto-Quiet in Non-TTY Context | P2 | 3 | Not started | No `progress()`/`log()` API or verbosity flags |
| [REQ-F-039](../cli-agent-ergonomics/requirements/f-039-duration-tracking-in-response-meta.md) | Duration Tracking in Response Meta | P1 | 3 | Done | `duration_ms` on every envelope, including timeout and cancel |
| [REQ-F-040](../cli-agent-ergonomics/requirements/f-040-absolute-path-output-enforcement.md) | Absolute Path Output Enforcement | P2 | 3 | Not started | Output paths are not made absolute |
| [REQ-F-041](../cli-agent-ergonomics/requirements/f-041-process-cwd-immutability.md) | Process CWD Immutability | P2 | 3 | Not started | Handler `os.chdir` is neither restored nor flagged |
| [REQ-F-042](../cli-agent-ergonomics/requirements/f-042-log-rotation-in-framework-logger.md) | Log Rotation in Framework Logger | P3 | 3 | Not started | No framework logger |
| [REQ-F-043](../cli-agent-ergonomics/requirements/f-043-temp-file-session-scoped-auto-cleanup.md) | Temp File Session-Scoped Auto-Cleanup | P2 | 3 | Not started | No session temp cleanup |
| [REQ-F-044](../cli-agent-ergonomics/requirements/f-044-shell-argument-escaping-enforcement.md) | Shell Argument Escaping Enforcement | P0 | 2 | Done | Newlines, CR, and NUL in `str` args refused in phase 1; `ctx.run`/`ctx.pipeline` take argument lists only, so metacharacters arrive literally; a shell string or `shell=` in handler source is a `RegistrationError` |
| [REQ-F-045](../cli-agent-ergonomics/requirements/f-045-agent-hallucination-input-pattern-rejection.md) | Agent Hallucination Input Pattern Rejection | P0 | 2 | Done | `Path` and pattern-typed scalars reject `..`, `%XX`, and NUL on every input route; plain `str` is unchecked |
| [REQ-F-046](../cli-agent-ergonomics/requirements/f-046-pager-environment-variable-suppression.md) | Pager Environment Variable Suppression | P0 | 2 | Done | `ctx.run` children get `PAGER`, `GIT_PAGER`, `MANPAGER`=`cat`, `LESS=-F -X -R`, empty `MORE`, inherited by grandchildren; `App.main()` sets them process-wide |
| [REQ-F-047](../cli-agent-ergonomics/requirements/f-047-repl-mode-prohibition-in-non-tty-context.md) | REPL Mode Prohibition in Non-TTY Context | P0 | 2 | Partial | No-args run shows help; `exec` refuses a TTY stdin; `input()` is not intercepted |
| [REQ-F-048](../cli-agent-ergonomics/requirements/f-048-help-output-routing-to-stderr-in-non-tty-mode.md) | Help Output Routing to Stderr in Non-TTY Mode | P0 | 1 | Done | Help text to stderr, JSON envelope with `meta.help` on stdout |
| [REQ-F-049](../cli-agent-ergonomics/requirements/f-049-async-command-handler-enforcement.md) | Async Command Handler Enforcement | P1 | 3 | Partial | `async def` handlers register and fail only at run time |
| [REQ-F-050](../cli-agent-ergonomics/requirements/f-050-update-notifier-side-channel-suppression.md) | Update Notifier Side-Channel Suppression | P1 | 3 | Not started | No `CI=1`/`NO_UPDATE_NOTIFIER` for children |
| [REQ-F-051](../cli-agent-ergonomics/requirements/f-051-debug-and-trace-mode-secret-redaction.md) | Debug and Trace Mode Secret Redaction | P0 | 2 | Done | Secrets redacted in errors, tracebacks, and `ctx.log`, including credential-named fields, env dumps, and headers; no audit log exists yet |
| [REQ-F-052](../cli-agent-ergonomics/requirements/f-052-response-size-hard-cap-with-truncation-indicator.md) | Response Size Hard Cap with Truncation Indicator | P0 | 2 | Partial | 1 MiB cap, `--max-output`, `meta.truncated`; hint is prose, not a runnable command |
| [REQ-F-053](../cli-agent-ergonomics/requirements/f-053-stdout-unbuffering-in-non-tty-mode.md) | Stdout Unbuffering in Non-TTY Mode | P0 | 2 | Partial | Each envelope flushed; no heartbeat, no `PYTHONUNBUFFERED` |
| [REQ-F-054](../cli-agent-ergonomics/requirements/f-054-stdin-payload-size-cap-with-input-file-fallback.md) | Stdin Payload Size Cap with --input-file Fallback | P0 | 2 | Partial | 64 KiB cap and `--input-file` exist on `exec` only |
| [REQ-F-055](../cli-agent-ergonomics/requirements/f-055-editor-and-visual-no-op-in-non-tty-mode.md) | $EDITOR and $VISUAL No-Op in Non-TTY Mode | P0 | 2 | Partial | Off a terminal, children get `EDITOR`, `VISUAL`, `GIT_EDITOR`=`true`; no in-process editor interception with exit 4 yet |
| [REQ-F-056](../cli-agent-ergonomics/requirements/f-056-terminal-width-wrapping-disabled-in-json-mode.md) | Terminal Width Wrapping Disabled in JSON Mode | P0 | 2 | Done | Compact JSON; never wraps to terminal width |
| [REQ-F-057](../cli-agent-ergonomics/requirements/f-057-headless-environment-detection-and-gui-suppression.md) | Headless Environment Detection and GUI Suppression | P0 | 2 | Done | Headless when stdin or stdout is not a TTY, `CI`, or no `DISPLAY`/`WAYLAND_DISPLAY` on Linux or over SSH; `meta.headless` on every envelope; `ctx.open_url` needs `gui_operations` and fills `data.open_url` |
| [REQ-F-058](../cli-agent-ergonomics/requirements/f-058-high-entropy-field-masking.md) | High-Entropy Field Masking | P1 | 3 | Not started | No high-entropy masking or `--unmask` |
| [REQ-F-059](../cli-agent-ergonomics/requirements/f-059-json5-input-normalization.md) | JSON5 Input Normalization | P1 | 3 | Not started | Strict JSON only; no JSON5 normalization |
| [REQ-F-060](../cli-agent-ergonomics/requirements/f-060-third-party-stdout-interception.md) | Third-Party Stdout Interception | P1 | 3 | Partial | `sys.stdout` is swapped to stderr during a run with a warning; writes to fd 1 and import-time prints are not caught |
| [REQ-F-061](../cli-agent-ergonomics/requirements/f-061-symlink-loop-detection-in-traversal-utilities.md) | Symlink Loop Detection in Traversal Utilities | P1 | 3 | Not started | No traversal utilities |
| [REQ-F-062](../cli-agent-ergonomics/requirements/f-062-glob-expansion-and-word-splitting-prevention.md) | Glob Expansion and Word-Splitting Prevention | P0 | 2 | Done | Argument lists only; string argv is `SHELL_STRING_PROHIBITED` at registration (source scan) or run time. Debug-mode argv logging not applicable: treaty has no debug mode; argv is a JSON array in error context |
| [REQ-F-063](../cli-agent-ergonomics/requirements/f-063-credential-expiry-structured-error.md) | Credential Expiry Structured Error | P1 | 3 | Partial | PERMISSION_DENIED and AUTH_REQUIRED codes exist; no `refresh_command`/`required_permission` |
| [REQ-F-064](../cli-agent-ergonomics/requirements/f-064-output-truncation-detection-and-warning.md) | Output Truncation Detection and Warning | P1 | 3 | Partial | FIELD_TRUNCATED warnings on cap; no `max_bytes` field declaration |
| [REQ-F-065](../cli-agent-ergonomics/requirements/f-065-pipeline-exit-code-propagation.md) | Pipeline Exit Code Propagation | P0 | 2 | Done | `ctx.pipeline` checks every stage; the first failing stage raises `SUBPROCESS_FAILED` with `stage`. The parent-shell `pipefail` warning is not applicable: a child cannot observe its parent shell's options |
| [REQ-F-066](../cli-agent-ergonomics/requirements/f-066-subprocess-locale-normalization.md) | Subprocess Locale Normalization | P1 | 3 | Not started | No `LC_ALL` injection |
| [REQ-F-067](../cli-agent-ergonomics/requirements/f-067-interspersed-option-parsing.md) | Interspersed Option Parsing | P1 | 3 | Partial | Globals accepted anywhere before `--`; no `option_placement: strict` |
| [REQ-F-068](../cli-agent-ergonomics/requirements/f-068-help-and-version-flag-purity.md) | Help and Version Flag Purity | P0 | 2 | Done | Help, schema, and version resolve before parsing and resources |
| [REQ-F-069](../cli-agent-ergonomics/requirements/f-069-sigint-handler-installation.md) | SIGINT Handler Installation | P0 | 2 | Done | SIGINT exits 130; second signal exits immediately |
| [REQ-F-070](../cli-agent-ergonomics/requirements/f-070-atomic-write-via-rename.md) | Atomic Write via Rename | P1 | 3 | Partial | Atomic write used internally for idempotency records; no public helper |
| [REQ-F-071](../cli-agent-ergonomics/requirements/f-071-file-descriptor-leak-prevention.md) | File Descriptor Leak Prevention | P1 | 3 | Done | File descriptors are non-inheritable (PEP 446); no children spawned |
| [REQ-F-072](../cli-agent-ergonomics/requirements/f-072-lf-line-ending-enforcement.md) | LF Line Ending Enforcement | P1 | 3 | Partial | Writes `\n`; stdout not forced to `\n` on Windows |
| [REQ-F-073](../cli-agent-ergonomics/requirements/f-073-env-var-namespace-prefix.md) | Environment Variable Namespace Prefix | P1 | 3 | Partial | Env vars prefixed `TREATY_`, not per tool; unprefixed `CI` is read |
| [REQ-F-074](../cli-agent-ergonomics/requirements/f-074-json-null-absent-empty-convention.md) | JSON Null/Absent/Empty Convention | P1 | 3 | Partial | Dataclass outputs emit every key; no `[]` vs `null` enforcement |
| [REQ-F-075](../cli-agent-ergonomics/requirements/f-075-subcommand-additive-stability.md) | Subcommand Additive Stability | P1 | 3 | Not started | No deprecation metadata |
| [REQ-F-076](../cli-agent-ergonomics/requirements/f-076-first-run-init-isolation.md) | First-Run Init Isolation | P1 | 3 | Partial | No first-run work; no `init` built-in or INIT_REQUIRED helper |
| [REQ-F-077](../cli-agent-ergonomics/requirements/f-077-telemetry-non-blocking.md) | Telemetry Non-Blocking | P2 | 3 | Done | No network code or telemetry |
| [REQ-F-078](../cli-agent-ergonomics/requirements/f-078-retry-count-in-response-meta.md) | Retry Count in Response Meta | P2 | 3 | Not started | No retry machinery |
| [REQ-F-079](../cli-agent-ergonomics/requirements/f-079-global-option-scope.md) | Global Option Scope | P1 | 3 | Done | Root `flags` map; colliding command flags fail at registration |

## Command contract

| ID | Title | Priority | Level | Status | Notes |
|----|-------|----------|-------|--------|-------|
| [REQ-C-001](../cli-agent-ergonomics/requirements/c-001-command-declares-exit-codes.md) | Command Declares Exit Codes | P0 | 2 | Done | `exit_codes=` is required (`()` is explicit); `SUCCESS` always in the map; retryable entries must have `side_effects: none`; undeclared exits become `UNDECLARED_EXIT_CODE` |
| [REQ-C-002](../cli-agent-ergonomics/requirements/c-002-command-declares-danger-level.md) | Command Declares Danger Level | P0 | 2 | Done | `danger_level=` is required; in every `--schema`; destructive forces `dry_run`, safe gets no `--idempotency-key` |
| [REQ-C-003](../cli-agent-ergonomics/requirements/c-003-mutating-commands-declare-effect-field.md) | Mutating Commands Declare effect Field | P0 | 2 | Done | Mutating output types must carry `effect`; checked at registration and run time |
| [REQ-C-004](../cli-agent-ergonomics/requirements/c-004-destructive-commands-must-support-dry-run.md) | Destructive Commands Must Support --dry-run | P0 | 1 | Done | Destructive commands require `dry_run` and a `would_affect` field; dry runs must return `would_*` and `treaty.Affects`, else `INVALID_EFFECT` |
| [REQ-C-005](../cli-agent-ergonomics/requirements/c-005-interactive-commands-must-support-yes-non-interact.md) | Interactive Commands Must Support --yes / --non-interactive | P0 | 2 | Not started | No `interactive=`, `--yes`, or `--non-interactive` (ROADMAP 0.2.0) |
| [REQ-C-006](../cli-agent-ergonomics/requirements/c-006-all-args-validated-in-phase-1.md) | All Args Validated in Phase 1 | P0 | 2 | Done | All phase-1 errors in `error.errors` with field and value |
| [REQ-C-007](../cli-agent-ergonomics/requirements/c-007-mutating-commands-accept-idempotency-key.md) | Mutating Commands Accept --idempotency-key | P1 | 3 | Partial | `--idempotency-key` with replay as `noop`; no auto-generated key |
| [REQ-C-008](../cli-agent-ergonomics/requirements/c-008-multi-step-commands-emit-step-manifest.md) | Multi-Step Commands Emit Step Manifest | P1 | 3 | Not started | No step manifest (ROADMAP 0.3.0) |
| [REQ-C-009](../cli-agent-ergonomics/requirements/c-009-multi-step-commands-report-completed-failed-skippe.md) | Multi-Step Commands Report completed/failed/skipped | P1 | 3 | Not started | No batch summary contract |
| [REQ-C-010](../cli-agent-ergonomics/requirements/c-010-background-process-commands-declare-metadata.md) | Background-Process Commands Declare Metadata | P2 | 3 | Not started | No background-process metadata |
| [REQ-C-011](../cli-agent-ergonomics/requirements/c-011-commands-declare-filesystem-side-effects.md) | Commands Declare Filesystem Side Effects | P3 | 3 | Not started | No filesystem side-effect declaration |
| [REQ-C-012](../cli-agent-ergonomics/requirements/c-012-commands-with-network-i-o-support-timeout.md) | Commands with Network I/O Support --timeout | P0 | 2 | Done | `has_network_io` adds `--timeout` (`0` disables it); `ctx.timeout` for handlers; `network-timeout` audit rule flags calls without `timeout=` |
| [REQ-C-013](../cli-agent-ergonomics/requirements/c-013-error-responses-include-code-and-message.md) | Error Responses Include Code and Message | P0 | 1 | Done | Every `error.message` is normalized to a sentence; recoverable errors always carry `suggestion`; `exit-code-suggestion` audit rule |
| [REQ-C-014](../cli-agent-ergonomics/requirements/c-014-error-responses-include-retryable-and-retry-after-.md) | Error Responses Include retryable and retry_after_ms | P1 | 3 | Partial | `retryable` always present; RATE_LIMITED allowed without `retry_after_ms` |
| [REQ-C-015](../cli-agent-ergonomics/requirements/c-015-commands-declare-input-and-output-schema.md) | Commands Declare Input and Output Schema | P1 | 3 | Done | `--schema` has parameters and output schema, derived from the dataclass |
| [REQ-C-016](../cli-agent-ergonomics/requirements/c-016-secrets-accepted-only-via-env-var-or-file.md) | Secrets Accepted Only via Env Var or File | P1 | 3 | Done | Secrets only via `--x-from-env`, `--x-from-file`, or `<APP>_<X>` |
| [REQ-C-017](../cli-agent-ergonomics/requirements/c-017-commands-register-cleanup-hook.md) | Commands Register cleanup() Hook | P1 | 3 | Partial | `cleanup=` runs on signals only, not on normal exit or timeout; no resource `release` |
| [REQ-C-018](../cli-agent-ergonomics/requirements/c-018-commands-declare-platform-requirements.md) | Commands Declare Platform Requirements | P3 | 3 | Not started | No platform or required-tools declaration |
| [REQ-C-019](../cli-agent-ergonomics/requirements/c-019-subprocess-invoking-commands-declare-argument-sche.md) | Subprocess-Invoking Commands Declare Argument Schema | P1 | 3 | Not started | No subprocess API |
| [REQ-C-020](../cli-agent-ergonomics/requirements/c-020-resource-id-fields-declare-validation-pattern.md) | Resource ID Fields Declare Validation Pattern | P1 | 3 | Partial | Presets and `pattern=` checked in phase 1; no warning for ID fields without a pattern |
| [REQ-C-021](../cli-agent-ergonomics/requirements/c-021-auth-commands-declare-headless-mode-support.md) | Auth Commands Declare Headless Mode Support | P0 | 2 | Not started | No headless auth declaration |
| [REQ-C-022](../cli-agent-ergonomics/requirements/c-022-async-commands-declare-job-descriptor-schema.md) | Async Commands Declare Job Descriptor Schema | P0 | 2 | Not started | No async job descriptor |
| [REQ-C-023](../cli-agent-ergonomics/requirements/c-023-editor-requiring-commands-declare-non-interactive-.md) | Editor-Requiring Commands Declare Non-Interactive Alternative | P1 | 3 | Not started | No `requires_editor` |
| [REQ-C-024](../cli-agent-ergonomics/requirements/c-024-gui-launching-commands-declare-headless-behavior.md) | GUI-Launching Commands Declare Headless Behavior | P1 | 3 | Partial | `gui_operations=["browser_open"]` with `headless_behavior: emit_in_output` in the manifest; `skip` and `error` behaviors not offered |
| [REQ-C-025](../cli-agent-ergonomics/requirements/c-025-config-writing-commands-declare-write-scope.md) | Config-Writing Commands Declare Write Scope | P0 | 2 | Not started | No config write scope |
| [REQ-C-026](../cli-agent-ergonomics/requirements/c-026-commands-declare-conditional-argument-dependencies.md) | Commands Declare Conditional Argument Dependencies | P1 | 3 | Not started | No conditional argument rules (ROADMAP 0.3.0) |
| [REQ-C-027](../cli-agent-ergonomics/requirements/c-027-commands-declare-option-placement.md) | Commands Declare Option Placement Convention | P1 | 3 | Not started | No `option_placement`; tokens after `--` are rejected, not forwarded |
| [REQ-C-028](../cli-agent-ergonomics/requirements/c-028-already-exists-response-pattern.md) | ALREADY_EXISTS Response Pattern | P1 | 3 | Partial | `Exit.CONFLICT(code="ALREADY_EXISTS", data=...)` works; not a prescribed pattern |
| [REQ-C-029](../cli-agent-ergonomics/requirements/c-029-command-declares-required-scopes.md) | Command Declares Required Scopes | P0 | 2 | Partial | `required_scopes` in every schema, `[]` by default; never required |
| [REQ-C-030](../cli-agent-ergonomics/requirements/c-030-error-responses-include-fix-command.md) | Error Responses Include Executable fix_command | P1 | 3 | Partial | `fix_command` emitted but not validated |

## Opt-in

| ID | Title | Priority | Level | Status | Notes |
|----|-------|----------|-------|--------|-------|
| [REQ-O-001](../cli-agent-ergonomics/requirements/o-001-output-format-flag.md) | --format Output Format Flag | P0 | 2 | Partial | `--format` with json, plain, and registered formats; no `jsonl` |
| [REQ-O-002](../cli-agent-ergonomics/requirements/o-002-fields-selector.md) | --fields Selector | P2 | 3 | Not started | No `--fields` |
| [REQ-O-003](../cli-agent-ergonomics/requirements/o-003-limit-and-cursor-pagination-flags.md) | --limit and --cursor Pagination Flags | P0 | 2 | Not started | No `--limit`/`--cursor` (ROADMAP 0.2.0) |
| [REQ-O-004](../cli-agent-ergonomics/requirements/o-004-output-jsonl-stream-flag.md) | --format jsonl / --stream Flag | P2 | 3 | Partial | Streaming handlers with `--no-stream`; no `--stream` opt-in |
| [REQ-O-005](../cli-agent-ergonomics/requirements/o-005-output-id-extraction-mode.md) | --format id Extraction Mode | P3 | 3 | Not started | No `--format id` |
| [REQ-O-006](../cli-agent-ergonomics/requirements/o-006-stdin-as-id-source.md) | Stdin as ID Source (-) | P3 | 3 | Not started | No `-` for stdin arguments |
| [REQ-O-007](../cli-agent-ergonomics/requirements/o-007-stable-output-flag.md) | --stable-output Flag | P3 | 3 | Not started | No `--stable-output` |
| [REQ-O-008](../cli-agent-ergonomics/requirements/o-008-quiet-verbose-debug-verbosity-flags.md) | --quiet / --verbose / --debug Verbosity Flags | P1 | 3 | Not started | No verbosity flags |
| [REQ-O-009](../cli-agent-ergonomics/requirements/o-009-validate-only-flag.md) | --validate-only Flag | P1 | 3 | Not started | No `--validate-only` (ROADMAP 0.3.0) |
| [REQ-O-010](../cli-agent-ergonomics/requirements/o-010-resume-from-flag-for-multi-step-commands.md) | --resume-from Flag for Multi-Step Commands | P2 | 3 | Not started | No `--resume-from` |
| [REQ-O-011](../cli-agent-ergonomics/requirements/o-011-rollback-on-failure-flag.md) | --rollback-on-failure Flag | P2 | 3 | Not started | No rollback hook |
| [REQ-O-012](../cli-agent-ergonomics/requirements/o-012-heartbeat-interval-flag.md) | --heartbeat-interval Flag | P2 | 3 | Not started | No `--heartbeat-interval` |
| [REQ-O-013](../cli-agent-ergonomics/requirements/o-013-schema-output-schema-flag.md) | --schema / --output-schema Flag | P1 | 3 | Partial | `--schema` per command; no `--print-schema` alias or `--output-schema` |
| [REQ-O-014](../cli-agent-ergonomics/requirements/o-014-schema-version-compatibility-flag.md) | --schema-version Compatibility Flag | P2 | 3 | Not started | No schema version negotiation |
| [REQ-O-015](../cli-agent-ergonomics/requirements/o-015-show-config-flag.md) | --show-config Flag | P1 | 3 | Not started | No `--show-config` |
| [REQ-O-016](../cli-agent-ergonomics/requirements/o-016-no-config-flag.md) | --no-config Flag | P1 | 3 | Not started | No `--no-config` |
| [REQ-O-017](../cli-agent-ergonomics/requirements/o-017-cwd-root-flag.md) | --cwd / --root Flag | P2 | 3 | Not started | No `--cwd` |
| [REQ-O-018](../cli-agent-ergonomics/requirements/o-018-no-cache-and-cache-ttl-flags.md) | --no-cache and --cache-ttl Flags | P3 | 3 | Not started | No cache flags |
| [REQ-O-019](../cli-agent-ergonomics/requirements/o-019-proxy-and-no-proxy-flags.md) | --proxy and --no-proxy Flags | P2 | 3 | Not started | No `--proxy`/`--no-proxy` |
| [REQ-O-020](../cli-agent-ergonomics/requirements/o-020-no-update-check-flag.md) | --no-update-check Flag | P1 | 3 | Not started | No `--no-update-check` |
| [REQ-O-021](../cli-agent-ergonomics/requirements/o-021-confirm-destructive-flag.md) | --confirm-destructive Flag | P0 | 2 | Done | Unconfirmed destructive runs exit 2 with the preview and the `would_affect` summary; `--schema` has `requires_confirmation: true` |
| [REQ-O-022](../cli-agent-ergonomics/requirements/o-022-secret-from-env-secret-from-file-flags.md) | --secret-from-env / --secret-from-file Flags | P1 | 3 | Done | `--x-from-env` and `--x-from-file` |
| [REQ-O-023](../cli-agent-ergonomics/requirements/o-023-no-injection-protection-flag.md) | --no-injection-protection Flag | P3 | 3 | Not started | No trust tagging flag |
| [REQ-O-024](../cli-agent-ergonomics/requirements/o-024-context-config-override-flag.md) | --context / --config Override Flag | P1 | 3 | Not started | No `--config`/`--context` |
| [REQ-O-025](../cli-agent-ergonomics/requirements/o-025-warnings-as-errors-flag.md) | --warnings-as-errors Flag | P3 | 3 | Not started | No warn API, so no `--warnings-as-errors` |
| [REQ-O-026](../cli-agent-ergonomics/requirements/o-026-tool-doctor-built-in-command.md) | tool doctor Built-In Command | P1 | 3 | Not started | No `doctor` built-in |
| [REQ-O-027](../cli-agent-ergonomics/requirements/o-027-tool-cleanup-built-in-command.md) | tool cleanup Built-In Command | P2 | 3 | Not started | No `cleanup` built-in |
| [REQ-O-028](../cli-agent-ergonomics/requirements/o-028-tool-status-built-in-command.md) | tool status Built-In Command | P2 | 3 | Not started | No `status` built-in |
| [REQ-O-029](../cli-agent-ergonomics/requirements/o-029-tool-changelog-built-in-command.md) | tool changelog Built-In Command | P2 | 3 | Not started | No `changelog` built-in |
| [REQ-O-030](../cli-agent-ergonomics/requirements/o-030-tool-audit-log-built-in-command.md) | tool audit-log Built-In Command | P2 | 3 | Not started | No `audit-log` built-in |
| [REQ-O-031](../cli-agent-ergonomics/requirements/o-031-dependency-version-matrix-declaration.md) | Dependency Version Matrix Declaration | P1 | 3 | Not started | No dependency declarations (ROADMAP 0.3.0) |
| [REQ-O-032](../cli-agent-ergonomics/requirements/o-032-raw-payload-flag-for-mutating-commands.md) | --raw-payload Flag for Mutating Commands | P1 | 3 | Done | `--raw-payload` with schema and equivalence tests |
| [REQ-O-033](../cli-agent-ergonomics/requirements/o-033-headless-and-token-env-var-flags-for-auth-commands.md) | --headless and --token-env-var Flags for Auth Commands | P0 | 2 | Not started | No headless auth flags |
| [REQ-O-034](../cli-agent-ergonomics/requirements/o-034-tool-generate-skills-built-in-command.md) | tool generate-skills Built-In Command | P2 | 3 | Not started | No skill generation |
| [REQ-O-035](../cli-agent-ergonomics/requirements/o-035-tool-mcp-validate-built-in-command.md) | tool mcp-validate Built-In Command | P2 | 3 | Not started | No MCP drift check |
| [REQ-O-036](../cli-agent-ergonomics/requirements/o-036-instance-id-flag-for-agent-state-namespacing.md) | --instance-id Flag for Agent State Namespacing | P1 | 3 | Not started | No `--instance-id` |
| [REQ-O-037](../cli-agent-ergonomics/requirements/o-037-unmask-flag-for-high-entropy-fields.md) | --unmask Flag for High-Entropy Fields | P2 | 3 | Not started | No entropy masking |
| [REQ-O-038](../cli-agent-ergonomics/requirements/o-038-heartbeat-ms-flag-for-long-running-commands.md) | --heartbeat-ms Flag for Long-Running Commands | P1 | 3 | Not started | No `--heartbeat-ms` |
| [REQ-O-039](../cli-agent-ergonomics/requirements/o-039-input-file-flag-for-stdin-commands.md) | --input-file Flag for Stdin Commands | P1 | 3 | Partial | `--input-file` on `exec` only |
| [REQ-O-040](../cli-agent-ergonomics/requirements/o-040-no-follow-symlinks-flag-for-traversal-commands.md) | --no-follow-symlinks Flag for Traversal Commands | P1 | 3 | Not started | No `--no-follow-symlinks`/`--max-depth` |
| [REQ-O-041](../cli-agent-ergonomics/requirements/o-041-tool-manifest-built-in-command.md) | tool manifest Built-In Command | P1 | 3 | Partial | `manifest` built-in with etag; no `manifest --etag` or `meta.not_modified` |
| [REQ-O-042](../cli-agent-ergonomics/requirements/o-042-output-format-env-var-default.md) | Output Format Environment Variable Default | P2 | 3 | Partial | `TREATY_FORMAT` honored; not tool-prefixed or listed in help |
| [REQ-O-043](../cli-agent-ergonomics/requirements/o-043-agents-md-content-spec.md) | AGENTS.md Required Content | P1 | 3 | Not started | AGENTS.md lacks required sections; `treaty init` generates none |
| [REQ-O-044](../cli-agent-ergonomics/requirements/o-044-noninteractive-install-command.md) | Non-Interactive Install Command Documentation | P1 | 3 | Partial | AGENTS.md has an install section; heading and app scaffold do not match the spec |
| [REQ-O-045](../cli-agent-ergonomics/requirements/o-045-integration-artifact-version-declaration.md) | Integration Artifact Version Declaration | P1 | 3 | Partial | MCP adapter generated in-process; no version in static artifacts |
| [REQ-O-046](../cli-agent-ergonomics/requirements/o-046-agents-md-ci-validation.md) | AGENTS.md CI Validation | P2 | 3 | Not started | No AGENTS.md check in CI |
| [REQ-O-047](../cli-agent-ergonomics/requirements/o-047-tool-check-permissions-built-in-command.md) | tool check-permissions Built-In Command | P0 | 2 | Partial | `required_scopes` declared; no `check-permissions` or over-privilege warning |
| [REQ-O-048](../cli-agent-ergonomics/requirements/o-048-destructive-commands-default-dry-run.md) | Destructive Commands Default to Dry-Run Mode | P0 | 2 | Done | `safe_default=True`: dry run and exit 0 without `--live`; `--live --confirm-destructive` applies; `meta.dry_run` on every response, `meta.confirmed` when applied; `safe_default` in the manifest |
| [REQ-O-049](../cli-agent-ergonomics/requirements/o-049-llm-token-budget-flags.md) | LLM Token Budget Flags | P2 | 3 | Not started | No token budget flags (ROADMAP 0.3.0) |
| [REQ-O-050](../cli-agent-ergonomics/requirements/o-050-tool-exec-built-in-command.md) | tool exec Built-In Command | P2 | 3 | Partial | `exec` built-in with per-line envelopes; no `jsonl` format |
