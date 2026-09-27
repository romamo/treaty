# 01: Output hygiene

Makes stdout carry only the envelope, whatever the handler or its dependencies do.

| Requirement | Level | Now | Gap |
|-------------|-------|-----|-----|
| F-006 Stdout/stderr enforcement | 1 | Partial | Handler `print()` lands on stdout ahead of the envelope; no `log()` API |
| F-007 ANSI suppression | 1 | Partial | Escape sequences inside data strings reach the JSON |
| F-008 NO_COLOR and CI detection | 1 | Partial | `NO_COLOR` and `TERM=dumb` not read; children do not inherit `NO_COLOR=1` |
| F-010 Pager suppression | 1 | Partial | Children do not see `PAGER=cat` |
| C-013 Error code and message | 1 | Partial | Messages are fragments; `suggestion` not required for recoverable errors |
| F-005 Locale-invariant serialization | 2 | Partial | `datetime` is rejected as INVALID_OUTPUT instead of written as ISO 8601 |
| F-051 Debug and trace redaction | 2 | Partial | No `log()` path to redact; env dumps unredacted |

Also closes at no extra cost: F-060 (third-party stdout, P1), F-016 (UTF-8 sanitization, P1).

## Design

### Stdout guard (F-006, F-060)

`_Run` already keeps the real stdout as `self.out`. In `App.run`, after routing and before
the first handler call, replace `sys.stdout` with a `_StrayStdout` writer that forwards to
`self.err` and records the byte count; restore it in `finally`. Because the handler runs on
a daemon thread (`call_with_timeout`), the swap must be process-wide, not a context-local
`redirect_stdout`. That is safe: one handler runs at a time, and an abandoned handler keeps
writing to stderr, which is the correct place.

When the guard saw bytes, add a `THIRD_PARTY_STDOUT` warning to the envelope via
`_warned`, with `context.bytes`. File-descriptor-level writes (C extensions, children
spawned with plain `subprocess`) are out of reach of this guard; 04 covers children.

### `ctx.log()` (F-006, F-051)

`Ctx.log(message: str, **fields: object) -> None` writes one line to stderr: plain text in
`plain` mode, a JSON object with `level`, `message`, `fields` in `json` mode. Every value
passes through the run's existing `_redactor`, so declared secrets never appear (F-051).
`Ctx` is frozen; add a `_log: Callable[[str, Mapping[str, object]], None]` field set by
`_Run` and keep it out of the public constructor.

F-051 also asks for redacted env dumps: `ctx.log` redacts any field value whose key matches
the secret-name heuristic already used by `Flag` inference (`token`, `secret`, `password`,
`key`, `credential`, `auth`).

### ANSI and text sanitization (F-007, F-016)

In JSON mode, `to_jsonable` strips CSI and OSC sequences (`\x1b\[[0-?]*[ -/]*[@-~]` and
`\x1b\][^\x07\x1b]*(\x07|\x1b\\)`) from every `str` and replaces lone surrogates with
U+FFFD. Plain mode keeps the text as returned, because a renderer may color on purpose.
When a string was changed, add one `OUTPUT_SANITIZED` warning naming the JSON paths.

### Color and environment detection (F-008, F-010)

Add `Ctx.color: bool`, false when any of: JSON mode, `NO_COLOR` is set (even empty),
`TERM=dumb`, `CI` is set, stdout is not a TTY. Plain renderers read it; the framework
itself still emits no color.

In `App.main()` only (not `App.run`, which embedders and tests call with their own `env`),
when stdin or stdout is not a TTY, set `NO_COLOR=1`, `PAGER=cat`, `GIT_PAGER=cat`, and
`TERM=dumb` in `os.environ` if the caller did not set them. This reaches every child and
grandchild, including ones spawned with plain `subprocess`, which is what the F-010
acceptance test (`echo $PAGER` in a child shell) checks. 04 applies the same set
explicitly through its `env=`.

### Error messages (C-013)

- Rewrite every framework `ErrorDetail.message` as a sentence: capital first letter, final
  period. `ARG_ERROR`, `TIMEOUT`, `CONFIRMATION_REQUIRED`, `STDIN_TOO_LARGE`, and the
  `ParseError` texts in `_parse.py` are the bulk
- A test walks every code path that builds an `ErrorDetail` (grep in the test for
  `message=`) and asserts the sentence shape, so new messages cannot regress
- `CliExit` construction normalizes an author message to a sentence (capitalize, append a
  period when missing); no rejection, because author text is data
- Recoverable means `retryable: true` or a non-null `fix_required`. `_exit_envelope` fills
  `suggestion` from the exit code entry's new `suggestion=` field when the author gave
  none; `app.exit_code(..., suggestion=...)` and the framework table provide defaults. The
  `exit-code-suggestion` audit rule flags retryable entries without one

### Datetime and decimal output (F-005)

`to_jsonable` writes `datetime` as ISO 8601 (`isoformat()`, with `Z` for UTC and a refusal
of naive datetimes as INVALID_OUTPUT with a fix naming `tzinfo`), `date` and `time` as ISO
8601, and `Decimal` as a JSON string. `schema_for` maps them to `string` with `format:
date-time`, `date`, `time`, and `pattern` for decimals.

## Tasks

- [x] `_StrayStdout` and the swap in `App.run`; `THIRD_PARTY_STDOUT` warning
- [x] `Ctx.log` with redaction; document it in README next to `ctx.timeout`
- [x] Sanitizer in `to_jsonable` for JSON mode; `OUTPUT_SANITIZED` warning
- [x] `Ctx.color`; environment defaults in `App.main()`
- [x] Sentence-form framework messages; `suggestion=` on `ExitCodeEntry`; audit rule
- [x] `datetime`, `date`, `time`, `Decimal` in `to_jsonable` and `schema_for`

## Deviations as built

- Messages: `ErrorDetail.__post_init__` normalizes every `message` (and `errors[].message`)
  to a sentence instead of rewriting each framework string; messages that began with a
  command path now begin with `Command <path>`, and a few `_cli.py` and `_mode.py` ones were
  reworded so capitalizing never changes an identifier. The test asserts `^[^a-z].*[.!?]$`,
  because many messages open with a quoted flag name (`'replicas' expects an integer.`)
- `suggestion`: `ErrorDetail` fills it from `fix_required`, else a generic retry step, so
  every recoverable error has one; no per-code defaults in the framework table. The
  `exit-code-suggestion` rule is advice, since the fallback already meets the spec
- No `OUTPUT_SANITIZED` warning: the spec asks only for stripping. Cleaning happens in
  `Envelope.to_json`, not `to_jsonable`, so error messages, context, and meta are covered
  too, and it also removes carriage returns and null bytes (REQ-F-007, REQ-F-016)
- `ctx.log` redaction also matches `pass`, `cookie`, and `API_*` names (REQ-F-051 lists
  them) and walks nested mappings, so header and env dicts are covered
- `App.main()` overrides `PAGER` and `GIT_PAGER` always, as REQ-F-010 says, and sets
  `NO_COLOR=1` when color is off; it does not set `TERM=dumb`, which no criterion needs
- `App.call` (MCP) gets no stdout swap: MCP calls run concurrently on threads, and a
  process-wide swap is not safe there

## Tests

- `print("x")` in a handler: stdout parses as one envelope, `x` is on stderr, warning present
- `2>/dev/null` still yields the envelope; `1>/dev/null` loses no stderr log line
- A handler returning `"\x1b[31mred\x1b[0m"` gives `"red"` in JSON and the raw text in plain
- `NO_COLOR=` (empty), `TERM=dumb`, and `CI=1` each set `ctx.color` false
- Under `main()` with piped stdout, a handler running `sh -c 'echo $PAGER $NO_COLOR'` sees `cat 1`
- Every framework error message matches `^[A-Z].*[.!?]$`
- The same run under `LC_ALL=de_DE.UTF-8` and `LC_ALL=C` is byte-identical after removing
  `request_id` and `duration_ms`; a UTC `datetime` renders as `2026-09-27T10:00:00Z`
- `ctx.log("x", token="abc")` prints `[REDACTED]` for `token`
