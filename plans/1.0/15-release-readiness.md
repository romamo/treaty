# 15: Release readiness

Size: M

The work 1.0 needs that no spec requirement covers: a reviewed and frozen public API, a
deprecation policy for after the freeze, release documentation, and evidence from real
consumers. Runs after the Phase A workstreams (01 to 07) land, and gates the 1.0 tag.

| Item | Now | Gap | API change |
|------|-----|-----|------------|
| Public API review | 36 names in `treaty.__all__` | Never reviewed as a whole; some may be internal (`ExecArgs`, `ExitCodeName`, `SchemaError`) | Yes: removals and renames happen here or never |
| Wire contract version | `schema_version` in the manifest only | No version for the envelope and manifest shapes that 1.0 freezes | Yes: `meta.schema_version` becomes `1` (see 01) |
| Deprecation policy | None; D3 broke every app with no window | Nothing tells adopters what 1.x may change | No: policy and tooling only |
| `CHANGELOG.md` | Missing | Releases 0.0.1 to 0.1.0 are only in git history | No |
| `docs/guide.md` | Missing (ROADMAP 0.1.x) | The judgement calls the audit cannot make | No |
| Spec pin | CI pins `SPEC_REF` to a commit | 1.0 should name the spec release it conforms to | No |
| Windows | CI runs tests and scaffold on Windows; 4 signal tests skip | Skipped behavior is not documented per platform | No |
| Consumers | One (cloudfall), adoption in progress | 1.0 needs evidence the frozen API fits more than one app | No |
| Classifier | `Development Status :: 2 - Pre-Alpha` | Should move with each milestone | No |

## API impact

This workstream decides the frozen surface. Everything exported from `treaty` at 1.0, every
keyword of `App`, `App.command`, `Group.command`, `Flag`, `Arg`, and `Ctx`, every envelope
and manifest field, every framework exit code and error code, and every environment
variable name is covered by semantic versioning from 1.0 on. Private modules (`_*.py`) stay
private; the review moves any name adopters need out of them into `__init__`.

## Design

### Public API review

- Generate an inventory with a script in `tmp/`: exported names, the signature of each
  public callable, `App.command` keywords, envelope and manifest keys (from the spec
  schemas and `_manifest.py`), `FrameworkCode` members, and every env var the source reads
- For each item decide: keep, rename, make private, or remove. Record the table in
  `docs/api.md`, which becomes the reference page for 1.0
- Snapshot test: `tests/test_public_api.py` asserts the inventory, so any later change to
  the frozen surface fails CI until the snapshot is updated deliberately
- Settle the naming questions the workstreams deferred here: the output-field marker
  (07-D4: `Field` in 07 and `Out` in 05 should be one type), the built-in name rule
  (13-D1), `side-effects` versus `status --show-side-effects` (08-D1, 13-D2), and whether
  `Ctx.config` becomes private (02)
- Fix `build_manifest`: `framework_version` carries `app.version`, not treaty's version

### Wire contract version

Workstream 01 adds `meta.schema_version`. Set the envelope and manifest contract to
version `1` at the tag, and document that additive fields keep the version while removals
or meaning changes bump it.

Deviation: `meta.schema_version` stays the per-command `data` contract that 01 made it,
and the manifest's `schema_version` is the spec's `"3.0"`. The envelope and manifest are
versioned by the spec schemas they validate against; `docs/api.md` states the rule.

### Deprecation policy (after 1.0)

- Removals only in a major release. A deprecated keyword or name works for at least one
  minor release and emits a `DEPRECATED_USAGE` registration warning that `treaty audit`
  reports with a generated fix
- Deprecated commands of apps built on treaty use the F-075 metadata from workstream 04,
  so treaty and its apps share one mechanism
- The policy lives in `README.md` under "Stability"

### Documentation

- `CHANGELOG.md`: Keep a Changelog format, reconstructed from tags `v0.0.1` to `v0.1.0`,
  with "Breaking" sections; apps get a separate structured schema changelog for the `changelog` built-in (workstream 13)
- `docs/guide.md`: naming paths, what belongs in `error.context`, when a failure deserves
  its own exit code, when to split a command. Short, since the audit covers mechanics
- `docs/tutorial/` (already drafted): check it against the frozen API before the tag
- `COMPLIANCE.md` names the spec release and commit it was assessed against

### Consumers

- Finish the cloudfall port and record what changed in the API because of it
- Port a second, differently shaped CLI (a read-heavy tool with pagination and streaming,
  since cloudfall is mutation-heavy). Gaps found feed back into Phase A before the tag
- Run `benchmark/` against the release candidate and publish the numbers in the release notes

### Release candidates

`1.0.0rc1` after Phase A and the API review; at least two weeks with both consumers on it;
fixes go into `rc2` and later. Tag `1.0.0` when a release candidate passes with no API
changes. Move the classifier to `4 - Beta` at rc1 and `5 - Production/Stable` at 1.0.

## Decisions

| ID | Question | Recommendation |
|----|----------|----------------|
| 15-D1 | Does Phase B (08 to 14) block 1.0? | No: it is additive and ships in 1.x minors. Only Phase A and this file block the tag |
| 15-D2 | Which second consumer? | A read-heavy CLI the author already maintains, so the port is real and not a demo |
| 15-D3 | Does 1.0 claim Level 3 conformance? | No: claim Level 2, publish the Level 3 score from `COMPLIANCE.md` |

## Tasks

- [x] Inventory script and `docs/api.md` with a keep, rename, private, or remove decision per item
  (the inventory lives in `tests/test_public_api.py`; running it as a script prints the
  snapshot, so `tmp/` holds only a wrapper)
- [x] Settle 07-D4, 13-D1, 08-D1 with 13-D2, and `Ctx.config`; fix `framework_version`
  (all four had landed with their workstreams: `Out`, yielding built-ins,
  `status --show-side-effects`, `Ctx._config_file`. `framework_version` is treaty's
  version; `treaty audit --baseline` reads the released app version from the saved
  response's `meta.tool_version` instead)
- [x] Apply the review's renames and removals; migrate examples, scaffold, tutorial
  (no renames; `ExecArgs` unexported, nine `Ctx` plumbing fields and seven `App` helpers
  made private. None was used by examples, scaffold, tutorial, or README, so nothing
  migrated. `ExitCodeName` and `SchemaError` stay public: `CliExit` carries the one and
  registration raises the other)
- [x] `tests/test_public_api.py` snapshot of the frozen surface
- [x] `CHANGELOG.md` from tag history
- [x] `docs/guide.md`
- [x] Stability and deprecation policy in `README.md`; `DEPRECATED_USAGE` warning and audit rule
  (policy only: treaty has no registration-warning channel and nothing is deprecated, so
  the warning and rule land with the first deprecation after 1.0, on `treaty.Deprecated`)
- [x] Document per-platform behavior (signals on Windows)
  (README "Platforms")
- [ ] Finish cloudfall port; port a second consumer; feed gaps back into Phase A
  (needs the user: a real-consumer soak)
- [x] `1.0.0rc1`, classifier `4 - Beta` (tagged 2026-09-28, ahead of the consumer ports)
- [ ] Soak the release candidate (rc18, 2026-10-02) with both consumers for at least two weeks
- [ ] Tag `1.0.0`, classifier `5 - Production/Stable`; update `COMPLIANCE.md`, `README.md`, `HANDOFF.md`, `ROADMAP.md`
  (after the soak)
