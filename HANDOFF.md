# Handoff

Status as of 2026-09-25. Read this before touching the code.

## What this is

`treaty` is a zero-dependency Python CLI framework that implements the
[CLI Agent Spec](../cli-agent-ergonomics). The spec repo is the source of truth for
every schema and requirement cited here; this repo is one implementation of it.
`agentyper` (sibling directory) is a separate, older library on pydantic and rich.
The two do not share code.

## State

| Check | Result |
|-------|--------|
| `uv run pytest` | 163 passed |
| `uv run mypy src` (strict) | clean |
| `uv run ruff check src tests examples` | clean |
| Spec conformance kit against `examples/deployctl.py` | 11 of 11, levels 1 to 3 |
| Git | initial commit on `main`; no remote, nothing pushed |
| PyPI | `treaty` is free; nothing published |

## Decisions already made

- **Zero runtime dependencies in core.** Extras `[rich]` and `[pydantic]` are reserved in
  `pyproject.toml` but empty; adapters are postponed (see `ROADMAP.md`)
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
- **Positional arguments are also flags.** Every field appears in the manifest `flags` map
  and can be passed as `--name=value`, because `FlagEntry` has no positional marker
- **In-house parser.** `argparse` is not used anywhere; every parse failure is a
  `ParseError` with structured context and becomes exit `2`
- **Unconfirmed destructive commands exit 2**, running the handler in dry-run mode and
  returning the preview as `data` with error code `CONFIRMATION_REQUIRED` (REQ-O-021)
- **Timeouts use a daemon thread**, not `SIGALRM`, so they work on Windows, off the main
  thread, and inside blocking C calls. A timed-out handler is abandoned, not killed
- **Commands may set `human=`**, a renderer for human mode; JSON mode ignores it. It also
  renders `data` on failed runs, so a `CliExit` must carry `data` of the handler's return
  type (the error line goes to stderr)
- **`treaty audit --strict` fails on warnings and errors, never advice.** It raises
  `AUDIT_FAILED` (79) with the full report as `data`; without `--strict` the audit always
  exits 0 on a completed run
- **Uncaught handler exceptions propagate.** Only `CliExit`, `ParseError` (a handler
  validating its own input, exit 2), timeout, and cancellation become envelopes; anything
  else is a bug and surfaces as a traceback

## Layout

```
src/treaty/
  __init__.py    public API; everything else is private
  _app.py        App, Group, run(), _Run (envelope construction, exec loop, --schema)
  _audit.py      ordered static rules over a registry; RULES tuple is the audit order
  _cli.py        the `treaty` console script (audit, rules, init, conformance)
  _profile.py    probes from examples and danger levels, profile writer, kit runner
  _scaffold.py   file templates for `treaty init`; generated projects pass the audit
  _cap.py        OutputCap, cap_envelope(): byte cap with per-field truncation; StdinCap
  _command.py    Command record, build_command(), handler signature inspection
  _context.py    Ctx handed to handlers (mode, request_id, env, state, timeout, idempotency_key)
  _dispatch.py   DispatchRequest line parser for exec
  _effect.py     effect contract: registration check and per-run validation
  _envelope.py   Envelope, ErrorDetail, WarningDetail, write_envelope()
  _errors.py     TreatyError family (registration), ParseError, CliExit, Exit factory
  _exit.py       ExitCodeEntry, FrameworkCode, ExitCodeRegistry, signal entries
  _flags.py      Arg/Flag markers, FieldInfo, inspect_fields(), token coercion
  _help.py       human-mode help renderer (root, group, command)
  _idempotency.py  IdempotencyKey VO, per-key locked record store, state dir lookup
  _manifest.py   build_manifest(), command_entry(), command_schema(), etag
  _mode.py       OutputMode resolution (--format, TREATY_FORMAT, CI, tty)
  _parse.py      globals, path routing, per-command parsing, mapping builder, raw payload
  _schema.py     annotation to draft-07 schema, to_jsonable()
  _signals.py    SIGINT/SIGTERM handlers, re-entrancy guard
  _timeout.py    Timeout VO, call_with_timeout()
  _types.py      annotation classification shared by _flags and _schema
  _values.py     CommandPath, ExitCodeName, ExitCode, Scope, Etag
examples/        deployctl.py (destructive, raw payload), slowctl.py (timeout, cleanup)
conformance/     deployctl.json profile and launcher for the spec kit
tests/           one file per feature; conftest.py holds the shared app fixture
```

## Execution path

1. `split_globals` strips `--format`, `--help`, `--schema`
2. `resolve_mode` picks human or JSON
3. `resolve_path` consumes tokens by longest known prefix
4. `parse_command_args` (argv) or `build_from_mapping` (exec, raw payload) yields an
   `Invocation`: args dataclass plus `timeout` and `confirmed`
5. `_Run.execute` applies the destructive preview rule, runs the handler under
   `call_with_timeout` inside `cancellation_handlers`, and returns an `Envelope`
6. `_Run.emit` writes JSON or human output and returns the exit code

`exec` loops steps 4 to 6 per stdin line with `_cmd` and `_line` added to `meta`.

## Spec coverage

Implemented: REQ-F-001, F-002, F-003, F-004, F-006, F-007, F-008, F-009, F-011, F-012,
F-013, F-045 (paths), F-048, F-069, C-001, C-002, C-004, C-012, C-015, C-020 (`filepath`
only), O-021, O-032, O-041, O-050.

Framework flags the parser knows: `--format`, `--help`, `--schema`, and per command
`--timeout` (network), `--confirm-destructive` (destructive), `--raw-payload` (opt-in).

## Gotchas

- `tests/conftest.py` locates the spec checkout at `../cli-agent-ergonomics`; override with
  `TREATY_SPEC_DIR`. Schema and kit tests skip when it is absent
- `conformance/deployctl` runs the example through `.venv/bin/python`, so `uv sync` first
- Signal tests spawn real subprocesses and sleep; they add about three seconds
- `ruff --fix` once rewrote a deliberate `getattr` into attribute access and broke mypy;
  check the diff after autofix
- `tests/fixture_audit_app.py` is a deliberately flawed app; the audit tests count its
  findings exactly, so adding a rule means updating `failed == 7` there
- The kit resolves a `command` path containing a slash against the profile's directory;
  `_profile.build_profile` absolutizes relative launcher paths for that reason
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
