# 13: Built-in commands

Size: L

Adds the operational built-ins an agent reaches for before and after real work (`doctor`
checks beyond dependencies, `status`, `changelog`, `generate-skills`, `mcp-validate`),
finishes `cleanup`, and closes the one gap left in `manifest`. Phase B and additive,
except for one rule 1.0 must fix: which command names treaty takes on every app. Builds on
08 (`doctor`, `cleanup`, `SideEffect`), 02 (`--show-config`), 10 (`ctx.http`), and 03
(`network_context`).

| Requirement | Priority | Now | Gap | API change |
|-------------|----------|-----|-----|------------|
| O-026 `doctor` | P1 | Not started | 08 builds `doctor` for O-031 dependencies only; no `checks` array, custom checks, or network checks | Yes: `treaty.Check`, `treaty.endpoint`, `App(checks=)` |
| O-027 `cleanup` | P2 | Not started | 08 builds `cleanup` for `temp` and `cache`; no `--scope`, `--min-age`, `log` paths, or bytes freed | Yes: two flags and output fields on 08's command |
| O-028 `status` | P2 | Not started | No `status`; 08 adds `side-effects`, 02 adds global `--show-config` | Yes: `status` command |
| O-029 `changelog` | P2 | Not started | No structured schema history | Yes: `App(schema_changelog=)`, `changelog`, `treaty changelog-add` |
| O-034 `generate-skills` | P2 | Not started | No skill files | Yes: `generate-skills` command |
| O-035 `mcp-validate` | P2 | Not started | No static MCP schema to compare against | Yes: `mcp-validate`, `treaty-mcp --list-tools` |
| O-041 `manifest` | P1 | Partial | No `--etag` or `meta.not_modified` | Yes: `manifest --etag`, `meta.not_modified` |

## API impact

- **Built-in names (freeze-relevant)**: built-ins are registered in `App._register_builtins`
  before any user command, so a user command of the same name raises `RegistrationError`
  (the `treaty init` scaffold already has `status`). 08 avoids this by registering its
  built-ins only when something is declared, and raising on a clash. 13-D1 proposes one
  rule for every optional built-in; whichever is chosen is what 1.0 freezes
- New exports: `Check`, `endpoint`, `ChangelogEntry`. New `App` keywords: `checks=`,
  `schema_changelog=`. All optional; nothing changes for current apps
- Manifest: up to five more entries per app, so every etag changes once. Envelope:
  `meta.not_modified`, only on a `manifest --etag` hit. Error code: `SCHEMA_DRIFT_DETECTED`

## Design

New built-ins live in a new `_builtins.py` with `register_extended(app)`, called from
`App._register_builtins` after `manifest`, `version`, `check-permissions`, and `job`, and
registered through the same `self.command(...)` path with `danger_level=` and
`exit_codes=` declared. Plain-data MCP tool construction (`ToolEntry`, `tool_entries`,
`input_schema`, `output_schema`) moves from `_mcp.py` to a new `_tools.py` that does not
import `_app`, which breaks the `_app` → `_builtins` → `_mcp` → `_app` cycle.

### Yielding built-ins (13-D1)

`App` keeps `_yielding: set[CommandPath]` for every built-in except `manifest`, `version`,
and `exec`. The duplicate check in `App.command` and `_check_nesting` remove a yielding
built-in (and its entry in the set) instead of raising, so a user `status` command or
`status` group wins. Audit rule `builtin-shadowed` (advice) names each shadowed built-in
and suggests a rename; the scaffold's `status` becomes `show`.

### `manifest --etag` (O-041)

`ManifestArgs(etag: str | None = Flag(...))` replaces `NoArgs`; the value parses as `Etag`
with a `sha256:[0-9a-f]{32}` pattern, so a malformed tag exits 2 in phase 1. On a match
the handler returns a private `NotModified` marker, which `_Run._execute` maps to
`data: null` and `meta.not_modified: true`, so `App.call` and `exec` get it too; a
mismatch returns the full manifest, whose etag is already deterministic.

### `doctor` checks (O-026)

08 builds `doctor` with `data.dependencies` and `DOCTOR_CHECKS_FAILED`; this adds
`data.checks`:

```python
@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    fix: str | None = None        # required when ok is False
    version: str | None = None
    required: str | None = None
    error: str | None = None
```

- `App(checks=[...])` takes callables `(ctx) -> Check`, run under the app timeout, after
  two framework checks: the state dir and the user config parent are writable
- `treaty.endpoint(url, *, fix)` builds a reachability check on 10's `ctx.http`, so proxy
  variables apply (F-036); its `error` carries the proxy in effect and 03's
  `network_context` (F-037)
- A `Check` with `ok=False` and no `fix` fails the run with `INVALID_OUTPUT` rather than
  passing silently. Audit rule `doctor-fix` (advice): a check callable whose failing
  `Check(...)` has no literal `fix=` (AST scan, as `untimed_network_calls` does)

### `status` and `cleanup` (O-028, O-027)

- `status` (safe) takes `--show-side-effects` (08's `side-effects` listing, plus sizes from
  `os.scandir`), `--show-state-files`, and `--show-config` (02's resolver behind the global
  flag); no flag means all three. State files are the local and global config files, the
  idempotency record directory, and 08's `credential` and `config` declarations, each with
  `purpose`, `exists`, and `bytes`; values are never echoed. With `App(credentials=)` it
  adds `logged_in`. Every path goes through `Path.resolve()`, and a missing path is
  listed with `exists: false`, so it exits 0 whatever state exists
- `cleanup` keeps 08's destructive gate. It gains `--scope all|temp|cache|logs` (default
  `all`, meaning those three; `credential` and `config` are never removed) and
  `--min-age SECONDS` (default 0, against `st_mtime`; younger entries are listed under
  `skipped`). Output adds `bytes_freed` per path and `total_bytes_freed`

### `changelog` (O-029)

`App(schema_changelog=Path)` names a JSON file shipped with the package: entries with
`version`, `date`, `breaking`, `added`, `removed`, `changed`, and the manifest `etag` at
that version. It is loaded and validated when `App` is built; a malformed file is a
`RegistrationError`. `changelog --since 1.0.0` returns newer entries, newest first, with
`--since` parsed by the `semver` preset. The dev command `treaty changelog-add module:app`
diffs a committed `<app>.manifest.json` snapshot against the live manifest into field
paths (`deploy.flags.target`, `deploy.output.url`, `deploy.exit_codes.10`), sets
`breaking` when anything is removed or retyped or a required flag is added, appends the
entry, and rewrites the snapshot. Audit rule `schema-changelog` (warning): the live etag
differs from the latest entry's; fix `uv run treaty changelog-add module:app`. This file
is not the prose `CHANGELOG.md` of 15, which O-029's field lists cannot come from.

### `generate-skills` (O-034)

A new `_skills.py` renders from the manifest: `CONTEXT.md` (description, commands, the
shared exit-code table, non-interactive rules) and one `SKILL-<command>.md` per user
command, with YAML frontmatter (`name`, `description`, `version: <app.version>`, and
`args` as a JSON flow mapping, which is valid YAML and needs no YAML dependency).
Examples: the declared `examples`, a minimal call built from required fields with
placeholders from their type, pattern, or enum, and the `--schema` call, so there are
always at least three. Guardrails come from `danger_level`, `requires_confirmation`, and
`exit_codes`. The command is mutating, takes `--output-dir PATH` (default `./skills`),
writes through `_atomic.write_atomic`, and returns `skills` descriptors plus `effect`.

### `mcp-validate` (O-035)

`treaty-mcp module:app --list-tools` prints `{"cli_version", "tools": [...]}` from
`_tools.tool_entries` and exits, giving a static schema file to commit. `mcp-validate
--mcp-schema-file PATH` (safe) compares it with `tool_entries(app)`: `added` (in the CLI,
not MCP), `removed`, `changed` (`command`, `field`, `cli_type`, `mcp_type`), and
`missing_from_mcp`. Drift raises `CliExit("GENERAL_ERROR", code="SCHEMA_DRIFT_DETECTED",
data=...)`. `--mcp-server-url`: see 13-D3.

## Decisions

| ID | Question | Recommendation |
|----|----------|----------------|
| 13-D1 | One rule for optional built-in names: 08's (register only when declared, raise on a clash) or always-on and yielding to user commands? | Always-on and yielding, applied to 08's built-ins too: the commands are discoverable on every app and no app breaks |
| 13-D2 | With 13-D1, is 08's `side-effects` still needed (08-D1 chose it only to avoid `status`)? | No: fold it into `status --show-side-effects`, as C-011 and O-028 name it |
| 13-D3 | `mcp-validate --mcp-server-url` needs an MCP client in core | Not applicable: `treaty-mcp` serves stdio only and core stays dependency-free; `--list-tools` output is the source of truth |
| 13-D4 | `generate-skills --format markdown\|json` collides with the global `--format` | Keep `--format` as the envelope representation; skill files are always Markdown. Record as a deviation |

## Tasks

- [ ] `_tools.py` split out of `_mcp.py`; yielding built-ins; `builtin-shadowed` rule; scaffold `status` renamed
- [ ] `manifest --etag`, `NotModified`, `meta.not_modified` on the CLI, `App.call`, and `exec`
- [ ] `Check`, `endpoint`, `App(checks=)`, framework checks in `doctor`; `doctor-fix` rule
- [ ] `status` with its three flags; `cleanup --scope` and `--min-age` with bytes freed
- [ ] `App(schema_changelog=)`, `changelog --since`, `treaty changelog-add`, `schema-changelog` rule
- [ ] `_skills.py` and `generate-skills`; a test parses every frontmatter and counts examples
- [ ] `treaty-mcp --list-tools` with `cli_version`; `mcp-validate`
- [ ] Conformance probes for the new built-ins in `examples/deployctl.py` and the scaffold
- [ ] Update COMPLIANCE.md rows (O-026 to O-029, O-034, O-035, O-041), README, HANDOFF, ROADMAP
