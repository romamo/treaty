# 11: Logging, verbosity, and audit log

Size: L

Makes stderr quiet by default for agents and controllable by one set of global flags, turns
warnings into a failure on request, and records every invocation in a bounded, redacted
audit log that the app can query. Phase B: mostly additive, but it reserves four global
flag names and one built-in name, which is what the 1.0 freeze checks.

| Requirement | Priority | Now | Gap | API change |
|-------------|----------|-----|-----|------------|
| O-008 `--quiet`/`--verbose`/`--debug` | P1 | Not started | No verbosity flags; `ctx.log` always writes | Yes: three reserved global flags |
| F-038 Auto-quiet off a TTY | P2 | Not started | No levels; no `progress()` | Yes: `ctx.log` silent off a TTY by default; new `ctx.debug`, `ctx.log_error`, `ctx.progress` |
| O-025 `--warnings-as-errors` | P3 | Not started | `ctx.warn` exists; no flag | Additive: global flag, `WARNINGS_AS_ERRORS` code |
| F-026 Append-only audit log | P2 | Not started | No log (`treaty audit` is the static linter) | Yes: `App(audit_log=)`, `treaty.AuditLog`, `meta.audit_log_path`, `<APP>_AUDIT_LOG` |
| F-042 Log rotation | P3 | Not started | No framework log file | Additive: retention fields on `AuditLog` |
| O-030 `audit-log` built-in | P2 | Not started | No built-in | Yes: reserves `audit-log` (yielding, per 13) |

## API impact

- **Reserved global flags**: `--quiet`, `--verbose`, `--debug`, `--warnings-as-errors`, taken
  by `split_globals` anywhere on every path, like `--format`. A field named `quiet`,
  `verbose`, `debug`, or `warnings_as_errors` now fails registration; breaking for such apps,
  so it lands before 1.0
- **Default behavior change**: off a terminal or under `CI`, `ctx.log` writes nothing, and
  neither does stray `print()` output redirected to stderr. Logs were never part of the
  contract, but pipelines that scraped them see less; changelog entry under "Breaking"
- New `Ctx` methods: `debug`, `log_error`, `progress`. `ctx.log` keeps its signature and is
  INFO; `ctx.warn` stays the warn API (envelope `warnings`)
- New `App` keyword `audit_log: AuditLog | None = AuditLog()`: on by default, as F-026 is
  framework-automatic. New export `AuditLog`. `<APP>_AUDIT_LOG` (a path, or `off`)
- Envelope: `meta.audit_log_path` on every response while the log is active; error code
  `WARNINGS_AS_ERRORS`; warning code `AUDIT_LOG_UNAVAILABLE`
- Built-in `audit-log` on every app with an active log, yielding to a user command of that
  name (13's rule). The treaty CLI passes `audit_log=None`, so `treaty audit-log` never
  appears beside `treaty audit`; the new module is `_journal.py` to keep `_audit.py` the
  linter's

## Design

### Verbosity levels (O-008, F-038)

A new `_verbosity.py` next to `_mode.py`:

```python
class Verbosity(IntEnum):
    QUIET = 0     # --quiet: nothing on stderr
    AUTO = 1      # off a TTY or CI: errors only
    NORMAL = 2    # a TTY: info and progress
    VERBOSE = 3   # --verbose: info and progress anywhere
    DEBUG = 4     # --debug: plus debug and framework internals
```

- `resolve_verbosity(options, env, tty)`: an explicit flag wins, so `--verbose` with
  `CI=true` shows progress (criterion); else `AUTO` when stdout is not a terminal or `CI`
  is set (the test `color_allowed` uses), else `NORMAL`. Two of the three flags: exit 2,
  `ARG_ERROR` with `context.flags`, since the levels are mutually exclusive
- `_Stderr.write(text, level)` drops lines above the run's level. Every framework write in
  `_app.py` gets a level: handler tracebacks are ERROR, stray stdout text INFO. `--quiet`
  drops tracebacks too, since the envelope carries the error. `Prompter` is exempt: it only
  writes on an interactive terminal, where a person asked for the prompt
- `_Run._log_sink` takes a level; JSON-mode records already have `"level"`.
  `ctx.progress(message, *, done=None, total=None)` writes a `progress` record at NORMAL
  and above; `ctx.log_error` is emitted at every level but `QUIET` (criterion 3)
- `--quiet` gives zero bytes from Python code. Not applicable: writes by C extensions to
  descriptor 2; `main()` already points descriptor 1 at stderr, and `ctx.run` captures
  children's stderr, so treaty adds no bytes there

### Debug trace (O-008)

At `DEBUG`, `_Run.trace(event, **fields)` logs framework internals through the redacting
sink: resolved format and verbosity, the config file `_config_file` loads, the state
directory and idempotency claim or replay in `_keyed`, each resource acquired and released
(`Resolver`, 06's `Teardown`), each child's redacted argv, exit, and duration in
`Processes`, the effective timeout, and the audit log path. HTTP requests: `ctx.http` from
10 logs each request at DEBUG; for third-party clients, `guard_streams` attaches a
`logging.Handler` to the root logger for the run that routes records through the same sink
and redactor (urllib3 and httpx log requests at DEBUG), and removes it afterwards.
`meta.debug` is optional in the spec and not added.

### `--warnings-as-errors` (O-025)

A new `_Run.finish(command, args, envelope)` is the single place a terminal envelope is
settled before it is written: `App._route` before `emit`, `App._call`, and each line in
`_exec_invocation`. With the flag, an exit-0 envelope with any warning becomes exit 1
(`GENERAL_ERROR`, implicit on every command), error `WARNINGS_AS_ERRORS` with
`context.count`, warnings kept. Framework warnings (`THIRD_PARTY_STDOUT`,
`GLOBAL_CONFIG_MODIFIED`, `CLEANUP_FAILED`) count as well. A stream applies it to its final
envelope. Exec lines inherit the process's flag.

### Audit log and rotation (F-026, F-042)

`_journal.py` defines the settings and the entry:

```python
@dataclass(frozen=True, slots=True)
class AuditLog:
    path: Path | None = None        # None: <APP>_AUDIT_LOG, then the XDG data home
    max_bytes: int = 100 * 2**20
    keep: int = 5
    max_age_days: int = 30          # __post_init__ rejects values below 1
```

- Path: `AuditLog.path`, then `<APP>_AUDIT_LOG` (absolute, or `off`), then
  `$XDG_DATA_HOME/<app>/audit.jsonl`, then `~/.local/share/<app>/audit.jsonl`. With none
  resolvable the log is inactive and `meta.audit_log_path` is absent
- `_Run.finish` appends one entry after `--warnings-as-errors` is applied and before the
  envelope is written, then adds `meta.audit_log_path`. Fields: `timestamp` (UTC, ms),
  `command`, `parameters`, `exit_code`, `duration_ms`, `trace_id` (from the trace-id
  workstream, null until it lands), `request_id`, `operator` (`<APP>_SESSION` from 03,
  else null). Arg errors are logged with `parameters: {}`, never raw argv; `--help` and
  `--schema` are not invocations and are not logged
- Redaction: `parameters` come from the parsed args via `to_jsonable`; secret fields and
  `ctx.token` become `[REDACTED]`, and `_scrub` redacts credential-named keys at any depth
- Append-only: `os.open(O_WRONLY | O_APPEND | O_CREAT, 0o600)`, one `os.write` per line,
  directory `0o700`; string values over 1 KiB are truncated so lines stay short and whole
- An `OSError` never fails the command: warning `AUDIT_LOG_UNAVAILABLE` with the path
- Rotation: when the next line would pass `max_bytes`, `exclusive(<path>.lock)` from
  `_atomic.py` guards a re-stat (another process may have rotated) and the shift
  `audit.jsonl` to `audit.1.jsonl` up to `audit.<keep>.jsonl`, dropping the oldest. On the
  first append per process, rotated files older than `max_age_days` are deleted. Disk use
  stays under `(keep + 1) × max_bytes`
- Scope: the audit log is treaty's only log file (idempotency records prune themselves), so
  F-042's "framework logger" is this file

### `audit-log` built-in (O-030)

Registered in `_register_builtins` when the log is active: `danger_level="safe"`,
`exit_codes=()`, `streaming=True`, so every entry is one event line and `--format jsonl`
gives one entry per line. Args: `--since` (`30m`, `1h`, `2d`, or ISO 8601; parsed in
`__post_init__`, so a bad value exits 2), `--command`, `--trace-id`, and its own
`--limit` (streams are never paginated). It reads rotated files oldest first, then the
live file, filters, and stops at the limit. Entries are redacted at write time; the reader
applies `_scrub` again for lines written before a secret field was declared.

## Decisions

| ID | Question | Recommendation |
|----|----------|----------------|
| 11-D1 | Audit log on by default (F-026 is automatic) or opt-in (a file in every user's home) | On by default with `<APP>_AUDIT_LOG=off` and `audit_log=None`; the test suite sets `XDG_DATA_HOME` to a temp dir |
| 11-D2 | Entry key `args` (F-026) or `parameters` (O-030) | `parameters`, the name the query command's contract uses; note F-026's name in COMPLIANCE |
| 11-D3 | `WARNINGS_AS_ERRORS` with `data: null` (spec wire) or `data` kept | Keep `data`: on a mutating command it carries the `effect` that already happened |
| 11-D4 | `audit-log` as a stream (one entry per line) or a paginated list | Stream, to meet "one per line"; `--cursor` is not needed for a local file |
| 11-D5 | Drop stray `print()` text off a TTY | Yes, at INFO; `THIRD_PARTY_STDOUT` still counts the bytes in the envelope |

## Tasks

- [ ] `Verbosity`, `resolve_verbosity`, global flags in `split_globals` and `global_flag_entries`; collision check
- [ ] Levels on `_Stderr` and every framework write; `ctx.debug`, `ctx.log_error`, `ctx.progress`
- [ ] `_Run.trace` at the framework's decision points; run-scoped `logging` handler
- [ ] `_Run.finish`; `--warnings-as-errors` and `WARNINGS_AS_ERRORS`
- [ ] `AuditLog`, path resolution, redacted append, `meta.audit_log_path`, `AUDIT_LOG_UNAVAILABLE`
- [ ] Size rotation under the lock; age pruning on first append
- [ ] `audit-log` built-in; `audit_log=None` for the treaty CLI; test fixture for `XDG_DATA_HOME`
- [ ] Audit rule `log-not-print`: handlers calling `print()` or `sys.stderr.write`, fix `ctx.log(...)`
- [ ] Update COMPLIANCE rows F-026, F-038, F-042, O-008, O-025, O-030; README, HANDOFF, ROADMAP
