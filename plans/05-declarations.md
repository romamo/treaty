# 05: Required declarations

Turns the defaults that hide missing contracts into explicit declarations, and fills in the
destructive-command contract.

| Requirement | Level | Now | Gap |
|-------------|-------|-----|-----|
| C-004 Destructive dry run | 1 | Partial | No `would_affect` object |
| C-001 Command declares exit codes | 2 | Partial | Omitting `exit_codes=` is not an error |
| C-002 Command declares danger level | 2 | Partial | `danger_level` defaults to `safe` |
| C-012 Network commands support `--timeout` | 2 | Partial | No static check for network calls without a timeout |
| O-021 `--confirm-destructive` | 2 | Partial | `--schema` lacks `requires_confirmation: true` |
| O-048 Safe-default dry run | 2 | Partial | No `safe_default=`, `--live`, or `meta.dry_run` |

C-004 is in M1; the rest in M2. Follows decision D3 in [README](README.md).

## Design

### `would_affect` (C-004)

Destructive dry-run output must carry `would_affect`, next to the `effect` field that
`effect_problem` already checks:

```python
@dataclass(frozen=True, slots=True)
class Affects:
    summary: str              # "Rolls api back from 1.4.0 to 1.3.9"
    resources: list[str]      # ["service/api"]
    count: int
```

- `treaty.Affects` is exported; the registration check that today requires `effect` on
  mutating output also requires an optional `would_affect: Affects | None` field on
  destructive output types
- `effect_problem` fails a dry run (`would_*` effect) whose `would_affect` is `None`
- The `CONFIRMATION_REQUIRED` error copies `would_affect.summary` into its message, which
  meets O-021's "JSON error listing what would be affected"

### Required `exit_codes=` and `danger_level=` (C-001, C-002, D3)

- Both become required keyword arguments of `App.command` and `Group.command`. A sentinel
  default gives a `RegistrationError` that names the command and shows the fix
  (`exit_codes=()` or `danger_level="safe"`), rather than Python's `TypeError`
- `SUCCESS` stays implicit in the map; C-001's "must include `0`" is met because the
  framework always adds it, so the declaration cannot omit it
- C-001's "undeclared exit triggers a warning in development mode": treaty already turns it
  into `UNDECLARED_EXIT_CODE`, which is stricter; keep it
- Update `examples/`, `_scaffold.py`, the treaty CLI's own commands, `docs/tutorial`, and
  README snippets in the same change. Changelog entry under "Breaking"

### Network timeout check (C-012)

Add the `network-timeout` audit rule: in a command with `has_network_io=True`, AST-scan
the handler for calls to `urllib.request.urlopen`, `http.client.*Connection`,
`socket.create_connection`, `requests.*`, and `httpx.*` without a `timeout=` keyword, and
suggest `timeout=ctx.timeout.seconds`. Add `Timeout.seconds_left()` so handlers can pass
the remaining budget instead of the full value.

### `requires_confirmation` (O-021)

`_manifest.py` adds `requires_confirmation: true` to destructive entries and `--schema`.

### Safe-default commands (O-048)

`safe_default=True` on a destructive command flips the gate: without `--live` it runs the
dry-run path and exits `0` with a `would_*` effect; `--live` executes, still subject to
`--confirm-destructive`. Every response of such a command has `meta.dry_run`. The manifest
shows `safe_default: true` and lists `--live` instead of `--dry-run` as the switch.
Registration requires the `dry_run` field, as for every destructive command. This is
additive: plain destructive commands keep the exit-2 confirmation gate that O-021 requires.

## Tasks

- [x] `Affects`; output-type check; `effect_problem` rule; message in `CONFIRMATION_REQUIRED`
- [x] Required `exit_codes=` and `danger_level=`; migrate every call site
- [x] Audit rule `network-timeout`; `Timeout.seconds_left()`
- [x] `requires_confirmation` in manifest and `--schema`
- [x] `safe_default=`, `--live`, `meta.dry_run`
- [x] Conformance profile: `treaty conformance` derives probes for `--live`

## Deviations as built

- No `Timeout.seconds_left()`: C-012 asks that the configured timeout reach every network
  call, which `ctx.timeout.seconds` does; the audit fix suggests it. A remaining-budget
  helper had no requirement behind it
- `requires_confirmation` is only in `--schema`: `CommandEntry` in `manifest-response.json`
  has `additionalProperties: false`, and O-021 names `--schema`. The manifest already lists
  the `confirm-destructive` flag
- `Affects.resources` is a `tuple[str, ...]`, like every other frozen value in treaty
- `--dry-run` wins over `--live` instead of being a contradiction: the kit previews a
  destructive probe by appending `--dry-run` to its argv, and the probe for a
  `safe_default` command is `argv + --live`, so that preview must exit 0
- **`--live` is the confirmation on a `safe_default` command**: `--live` alone applies, and
  `--confirm-destructive` is not also required (it is accepted and changes nothing). The
  spec's acceptance criterion says `--live` executes, and O-048 describes safe-default as
  replacing the gate with a preview then commit workflow. `requires_confirmation` stays
  true in `--schema`, since applying still takes an explicit flag
- `meta.dry_run` is true on every response that applied nothing and on argument errors;
  `meta.confirmed` appears only on a live run
- The `exit-codes` audit rule stays: an explicit `exit_codes=()` on a non-safe command is
  still worth a warning
- Migration touched `benchmark/cli/treaty/democli.py` too; its filter check moved from a
  handler-raised `Exit.ARG_ERROR` into `__post_init__`, so a bad filter still exits 2

## Tests

- Destructive output type without `would_affect` fails registration; dry run with `None` fails
- `@app.command("x", description="x")` without `exit_codes` raises `RegistrationError`
  naming both missing arguments
- `--schema` of a destructive command has `requires_confirmation: true`
- `safe_default=True`: no flags gives exit 0, `would_*`, `meta.dry_run: true`, sentinel
  untouched; `--live` alone applies
- The audit flags `urlopen(url)` inside a network command and accepts `urlopen(url, timeout=...)`
