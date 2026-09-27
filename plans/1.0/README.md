# Implementation plan: 1.0

This plan takes treaty from 0.1.0 (Level 2 complete, Level 3 at 50%, see
[`COMPLIANCE.md`](../../COMPLIANCE.md)) to a 1.0 release whose public API is frozen. It
covers all 94 open Level 3 requirements plus the release work no requirement names.

1.0 is defined as:

1. Level 2, plus every Level 3 item that changes the public API (Phase A)
2. Two real consumers running on a release candidate
3. A reviewed API snapshot, a changelog, a guide, and a deprecation policy (15)

Additive Level 3 work (Phase B) ships in 1.x minor releases and does not block the tag.
Order is API-changing first: anything that renames, reserves, changes a default, or
changes the shape of `meta`, `error`, `data`, or the manifest must land before the freeze,
because after it the same change needs a major release.

## Workstreams

### Phase A: API-changing (blocks 1.0)

| File | Workstream | Requirements | Size |
|------|------------|--------------|------|
| [01](01-response-meta.md) | Response metadata | F-021, F-022, F-023, F-024, F-025, F-027, F-078, O-013, O-014 | L |
| [02](02-config-layer.md) | Config layer and env namespace | F-028, F-073, F-076, O-015, O-016, O-024, O-036, O-042 | L |
| [03](03-error-contract.md) | Error contract | C-007, C-014, C-028, C-030, F-033, F-037, F-063 | L |
| [04](04-argument-grammar.md) | Argument grammar and command surface | C-020, C-026, C-027, F-049, F-059, F-067, F-075, O-006, O-009 | L |
| [05](05-output-data.md) | Output data contract | F-017, F-020, F-040, F-064, F-072, F-074, O-007 | L |
| [06](06-multi-step.md) | Multi-step commands and lifecycle | C-008, C-009, C-017, O-010, O-011 | L |
| [07](07-output-security.md) | Output security | F-034, F-035, F-058, O-023, O-037 | M |

### Phase B: additive (1.x minors)

| File | Workstream | Requirements | Size |
|------|------------|--------------|------|
| [08](08-declarations.md) | Additional command declarations | C-010, C-011, C-018, C-019, C-024, O-031 | M |
| [09](09-session-hygiene.md) | Session and process hygiene | F-029, F-030, F-032, F-041, F-043, F-050, F-060, F-066, O-017, O-018, O-020 | L |
| [10](10-network-and-fs.md) | Network and filesystem utilities | F-036, F-061, O-019, O-040 | M |
| [11](11-logging.md) | Logging, verbosity, and audit log | F-026, F-038, F-042, O-008, O-025, O-030 | L |
| [12](12-output-selection.md) | Output selection and streaming flags | O-002, O-004, O-005, O-012, O-049, O-050 | L |
| [13](13-built-ins.md) | Built-in commands | O-026, O-027, O-028, O-029, O-034, O-035, O-041 | L |
| [14](14-agent-docs.md) | Agent docs and integration artifacts | O-043, O-044, O-045, O-046 | M |

### Release

| File | Workstream | Size |
|------|------------|------|
| [15](15-release-readiness.md) | API review, snapshot, changelog, guide, deprecation policy, consumers, release candidates | M |

Coverage check: the 94 Partial or Not started rows of `COMPLIANCE.md` each appear in
exactly one workstream table (50 in Phase A, 44 in Phase B). O-050 looks already met
(`exec --format jsonl` works and is tested); 12 only adds criterion-named tests.

## Pulled forward from Phase B

These tasks sit in Phase B files but break existing apps or change a default, so they land
with Phase A:

| Task | From | Why it cannot wait |
|------|------|--------------------|
| Reserve global names (see below) | 01, 02, 04, 05, 07, 09, 11, 12 | A reserved name collides with app fields (REQ-F-079); adding one after 1.0 breaks apps |
| `os.system`, `os.popen`, `shell=True` in a handler is a `RegistrationError` (landed) | 08 (C-019) | Refuses registration of apps that pass today |
| `gui_operations` requires `headless_behavior=` (landed) | 08 (C-024) | New required keyword |
| `ctx.log` and stray `print()` silent off a TTY or under `CI` | 11 (F-038) | Default behavior change |
| Built-in name rule: new built-ins yield to a same-named app command (landed with 08's `doctor` and `cleanup`) | 13 (13-D1) | Decides which names apps may use |
| `LC_ALL=C` for children unless `preserve_locale=True` (landed with 09) | 09 (F-066) | Changes child output that apps may parse |

### Reserved names (one commit, first in Phase A)

Landed: `RESERVED_GLOBAL`, `RESERVED_OPT_IN`, and `UNIMPLEMENTED` in `_framework.py`.
An opt-in name whose keyword does not exist yet had a predicate that is always false; its
workstream swapped in the real one and moved the name to `IMPLEMENTED`. Every opt-in name
is implemented now (`recursive_traversal=` landed with 10).

Each name joins `GLOBAL_FLAGS` or the per-command framework flags. Until its feature lands,
passing it exits 2 naming it as reserved.

- Every command: `--output-schema`, `--print-schema`, `--schema-version`, `--config`,
  `--context`, `--no-config`, `--show-config`, `--instance-id`, `--validate-only`,
  `--stable-output`, `--unmask`, `--no-injection-protection`, `--cwd`,
  `--no-update-check`, `--quiet`, `--verbose`, `--debug`, `--warnings-as-errors`,
  `--fields`, `--stream`, `--token-limit`, `--token-offset`, `--token-count`, `--tokenizer`
- Only on commands that opt in: `--retries`, `--retry-delay` (`retry=`), `--resume-from`
  (`resumable=`), `--rollback-on-failure` (`rollback=`), `--proxy`, `--no-proxy`
  (`has_network_io=True`), `--no-follow-symlinks`, `--max-depth` (`recursive_traversal=`)

## Order

```
Phase A   R ──┬── 01 ── 05 ── 07 ──────────┐
              ├── 02                       │
              ├── 03 ── 04                 ├── 15 API review ── 1.0.0rc1 ── soak ── 1.0.0
              ├── 06                       │
              └── pulled-forward tasks ────┘
Phase B   08 ── 13      09      10      11      12 (after 07)      14 (after 02)
```

- **R** (reserved names) first: one small commit that fixes the collision surface
- **01 before 05**: 05 makes paths absolute against `meta.cwd` from 01; 05's `Out` type
  lands before 01's `volatile-data` audit rule, which suggests it
- **03 before 04**: 04 uses `App.redirect` and exit 13 from 03 for removed commands
- **06** provides the run-scoped `Teardown` that 03's `ctx.lock` and 09's session temp
  directory register with; land its teardown task early. Landed: 09 calls
  `ctx.teardown.add(name, fn)`; `ctx.lock` kept its `with` block, which a resource's
  `release` can close
- **07 before 12**: both extend the one output step `_Run._present` (mask, trust tags,
  `--fields`, token budget, byte cap, in that order). Landed: `_present` runs where
  `execute` and `stream` make each envelope, before any sink; the byte cap stays in
  `_write`
- **02 before 14**: 14's env var inventory reads 02's registry of known variables
- **08 before 13**: 13 extends 08's `doctor` and `cleanup`

## Breaking changes at a glance

Pre-1.0, so no deprecation window (as with D3 in the Level 2 plan). Each needs a
"Breaking" changelog entry and a migration of `examples/`, `_scaffold.py`, and the tutorial.

- `TREATY_FORMAT`, `TREATY_MAX_OUTPUT_BYTES`, `TREATY_MAX_STDIN_BYTES`, `TREATY_STATE_DIR`
  become `<APP>_*` (02, F-073); every new env var uses the app prefix
- `Envelope` takes a `treaty.Meta` instead of `duration_ms` and `request_id`;
  `App(version=)` must be semver (01)
- `RATE_LIMITED` without `retry_after_ms` is `INVALID_EXIT`; invalid `fix_command` fails;
  `AUTH_REQUIRED` and run-time `INSUFFICIENT_SCOPES` codes are renamed (03)
- `async def` handlers refused at registration; malformed JSON input reports `INVALID_JSON` (04)
- Arrays in `data` sorted by default; `Path` values absolute; nullable collections in
  output types refused (05)
- `cleanup=` runs after every handler run, not only on signals (06)
- High-entropy strings and secret-named fields in `data` masked unless `--unmask` (07)
- The pulled-forward tasks above

## Cross-workstream decisions

Workstream-local questions are in each file's Decisions table. These span files:

| ID | Question | Recommendation |
|----|----------|----------------|
| X1 | Output-field marker: 05 proposes `treaty.Out`, 07 proposes `treaty.Field` | One type, settled in 15's API review; `Out` avoids confusion with `dataclasses.field` |
| X2 | F-037 `NetworkContext` is shaped in 03 but filled by `ctx.http` in 10 | 03 freezes the type; F-037 moves to Done when 10 ships |
| X3 | F-034's log criteria need the audit log (F-026, in 11) | 07 adds the `scrub` entry point; F-034 moves to Done when 11 ships |
| X4 | Audit log on by default writes a file in every user's home (11-D1) | Accept, with `<APP>_AUDIT_LOG=off`; decide before 1.0 since it is a default |
| X5 | F-078 `retries_exhausted` sits in 01 but is an `error` field | Agree the field in 03's frozen `ErrorDetail`; `ctx.http` in 10 calls `ctx.retry` |
| X6 | Spec gaps block some criteria: `introduced_in` and `deprecated_in` not in `CommandEntry` (F-075), no `environment` key in the manifest (F-073), O-009 exit 2 or 3, F-061 and O-040 disagree on `--max-depth`, O-019 and F-037 disagree on where network context goes | Emit the fields in `--schema` now and open spec PRs; follow acceptance criteria over schema text where they conflict |

X1 landed with 05: `treaty.Out` (`_out.OutSpec`: `sort_key`, `ordered`, `volatile`); 07
adds its keywords to the same spec.

X2 and X5 landed with 10: `ctx.http` fills `error.network_context`, and its retries go
through the command's `retry=` budget into `meta.retries` and `error.retries_exhausted`.

## Definition of done, per workstream

- Every acceptance criterion of every listed requirement has a test named after it
- `uv run pytest`, `uv run mypy src`, and `uv run ruff check src tests examples` are clean
- The conformance kit passes for `examples/deployctl.py` and a fresh `treaty init` project
- New declarations have an audit rule with a generated fix, listed by `treaty rules`
- Breaking changes have a changelog entry and migrated examples, scaffold, and tutorial
- `README.md`, `HANDOFF.md`, and `ROADMAP.md` are updated; the rows in `COMPLIANCE.md`
  move to Done
