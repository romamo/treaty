# 02: Config layer and env namespace

Size: L

Treaty writes config (`_config.py`, C-025) but never reads it, and its own environment
variables carry the `TREATY_` prefix instead of the tool's. This workstream adds a typed,
layered config read with source tracking, the flags that isolate one agent session from
another, and the per-tool env namespace. Phase A: the env var rename is breaking, and every
flag name, meta field, and file location here is frozen at 1.0.

| Requirement | Priority | Now | Gap | API change |
|-------------|----------|-----|-----|------------|
| F-073 Env var namespace prefix | P1 | Partial | `TREATY_FORMAT`, `TREATY_MAX_OUTPUT_BYTES`, `TREATY_MAX_STDIN_BYTES`, `TREATY_STATE_DIR`; no manifest list | Yes: breaking rename to `<APP>_*` |
| O-042 Format env var default | P2 | Partial | `TREATY_FORMAT` honored; not tool-scoped or in help | Yes: `<APP>_FORMAT` replaces it |
| F-028 Config source tracking | P1 | Not started | No config read, no `meta.config_sources` | Yes: `App(settings=)`, two meta fields on every response |
| O-015 `--show-config` | P1 | Not started | No flag | Yes: new global flag |
| O-016 `--no-config` | P1 | Not started | No flag | Yes: new global flag |
| O-024 `--config` / `--context` | P1 | Not started | No flags | Yes: new global flags, `meta.context` |
| O-036 `--instance-id` | P1 | Not started | No namespacing; local config writes are unlocked | Yes: new global flag, state paths move under an instance |
| F-076 First-run init isolation | P1 | Partial | No `init` built-in, no `INIT_REQUIRED` | Yes: `App(init=)`, `INIT_REQUIRED` and `INIT_FAILED` codes |

## API impact

- **Breaking, env vars**: `TREATY_FORMAT`, `TREATY_MAX_OUTPUT_BYTES`,
  `TREATY_MAX_STDIN_BYTES`, and `TREATY_STATE_DIR` stop being read; each has an `<APP>_`
  twin (`DEPLOYCTL_FORMAT`, ...), derived by `_secrets.default_env_var`. The treaty CLI's
  own `TREATY_SPEC_DIR` already follows the rule, since its app name is `treaty`
- **New env vars**: `<APP>_CONFIG`, `<APP>_CONTEXT`, `<APP>_INSTANCE_ID`, `<APP>_<FIELD>`
- **New `App` keywords**: `settings=` (a frozen dataclass type) and `init=`; new exports
  `Init`, `InitResult`
- **New global flags** (parsed by `split_globals` anywhere in argv, listed in the manifest
  `flags`): `--config PATH`, `--context NAME`, `--no-config`, `--show-config`,
  `--instance-id ID`. They become reserved names: an app field named `config` or `context`
  now fails registration through `framework_collisions`, which is breaking for such apps
- **Envelope**: `meta.config_sources` and `meta.effective_config_hash` on every response;
  `meta.context` and `meta.instance_id` when set (`ResponseMeta` allows extra keys)
- **Rename before freeze**: the `Ctx.config` field (the write handle) becomes private
  `_config_file`; `ctx.config_path` and `ctx.write_config` stay. Reading goes through the
  settings parameter, so "config" means one thing in the public surface

## Design

### Env namespace (F-073, O-042)

One helper, `_env.app_var(app_name, key)`, replaces the four `TREATY_*` constants in
`_mode.py`, `_cap.py`, and `_idempotency.py`. `resolve_mode` reads `<APP>_FORMAT` and, on
a bad value, raises the same `ParseError` as `--format` with `context.source` naming the
variable, so the shape and exit 2 match. Unprefixed reads stay only for the spec's
exception list (`CI`, `NO_COLOR`, `TERM`, `HOME`, `XDG_*`, proxies) plus `GITHUB_ACTIONS`
and `JENKINS_URL`, which are CI detection in the same spirit as `CI`.

- An `_env.KNOWN` registry collects every variable treaty reads for an app, with a
  description; the manifest lists it (see 02-D1) and `--help` names `<APP>_FORMAT` next to
  `--format`
- Treaty has no plugins, so F-073's plugin check becomes audit rule `env-prefix`: it
  AST-scans handlers and `acquire` for literal names in `ctx.env`, `os.environ`, and
  `os.getenv`, warns on any outside the prefix and exception list, and suggests
  `DEPLOYCTL_DEBUG` for `DEBUG`. `token_env_vars` names are exempt (other systems' tokens)

### Settings and layers (F-028, O-016, O-024)

An app declares its config as a frozen dataclass; the handler asks for it by type, through
the resource injection in `_resources.py` (no `acquire` needed for the settings class):

```python
@dataclass(frozen=True, slots=True)
class Settings:
    region: str = "us-east-1"
    retries: int = 3

app = App("deployctl", version="1.0.0", settings=Settings)

@app.command("deploy", description="...", exit_codes=(), danger_level="mutating")
def deploy(args: DeployArgs, ctx: Ctx, settings: Settings) -> Deployed: ...
```

A new `_settings.py` resolves it once per run, in `_Run.execute` before the idempotency
claim, highest precedence first: env `<APP>_<FIELD>`, then `--config PATH` alone, or else
the project file `./.<app>.toml` (found from `ctx.cwd`, see 09) and the user file
`user_config()`, then field defaults. Files are TOML via stdlib `tomllib`; `--config`
also accepts `.json` by suffix. Values are coerced with the same `classify` and scalar
registry as args, so the settings schema is the args schema.

- A file may hold `[contexts.<name>]` tables that overlay its top level, and a
  `current_context` key. `--context` (or `<APP>_CONTEXT`) overrides `current_context`; an
  unknown name exits 2 with `context.available`
- Unknown keys, wrong types, bad TOML, or a missing `--config` file exit 2 with code
  `CONFIG_INVALID`, `phase: validation`, and `context.path` and `context.key` (02-D3)
- `--no-config` skips every file and env vars still apply, so `meta.config_sources` is `[]`
- `meta.config_sources` lists absolute paths of files read, highest first;
  `meta.effective_config_hash` is 12 hex of sha256 over `canonical_json` of the merged
  settings. Without `settings=`: `[]` and the hash of `{}`
- Reads create nothing and share nothing between runs; a `config_write_scope` command
  under `--config` writes that file. `App.call` and MCP read `<APP>_CONFIG`,
  `<APP>_CONTEXT`, and `<APP>_INSTANCE_ID` instead of flags

### `--show-config` (O-015)

A global switch handled in `App._route` next to `--schema`: it resolves settings as a run
would and returns `data.effective_config`, `data.sources` (per key `env:DEPLOYCTL_REGION`,
`file:/abs/path`, or `default`), and `data.precedence_order` (`env-vars`, each file path,
`defaults`). Fields the secret inference in `_secrets.py` marks as secrets show
`[REDACTED]`. Needs no command, so `tool --show-config` works at the root.

### Instance namespacing (O-036)

`--instance-id ID` or `<APP>_INSTANCE_ID`; the ID must match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`
or the run exits 2. With it, `user_config()` returns `<config home>/<app>/instances/<id>/
config.toml`, `state_dir()` returns `<state>/instances/<id>`, and 09's session temp root
gains the same segment. `meta.instance_id` echoes it. Treaty keeps XDG locations instead
of the spec's `~/.tool/instances/`; the criterion's point (disjoint paths) holds. Without
an ID, `ConfigFile.write` takes the `exclusive` lock for local writes too, not only
global ones, so concurrent writes succeed one after another.

### First-run init (F-076)

Treaty's own first-run work is already isolated: config reads create nothing, and the
state dir is made only by a keyed run, failing with a structured `STATE_*` error. For apps
with setup, `App(init=MyInit())` takes a protocol:

```python
class Init(Protocol):
    def initialized(self, ctx: Ctx) -> bool: ...
    def run(self, ctx: Ctx) -> InitResult: ...
```

It registers an `init` built-in (danger `mutating`, `idempotent=True`) that returns
`{"already_initialized": true}` when `initialized()` holds. Every other non-built-in
command checks `initialized()` first and exits 4 with `INIT_REQUIRED` and
`fix_command: "<app> init"` (the spec's `next_steps` maps to treaty's `fix_command`). A
failing `run` exits 1 with `INIT_FAILED` and `context.reason` from the exception:
`PermissionError` is `permissions`, `OSError` with `ENOSPC` or `EDQUOT` is `disk`,
`ConnectionError`, `TimeoutError`, and `socket.gaierror` are `network`. Audit rule
`init-isolated` warns when a handler calls `mkdir`, `write_config`, or network APIs guarded
by an existence check (`if not path.exists()`), with the fix "move it into `App(init=)`".

## Decisions

| ID | Question | Recommendation |
|----|----------|----------------|
| 02-D1 | F-073 wants env vars in the manifest, but `manifest-response.json` has `additionalProperties: false` and no `environment` key | Add `environment` (name, description, required) to the spec schema first, then emit it; until then, list them in `--help` |
| 02-D2 | Keep reading `TREATY_*` for one release with a deprecation warning | No: reading them is what F-073 forbids; hard break as in D3, changelog under Breaking |
| 02-D3 | Invalid config: exit 2 or 4 | 2 with `CONFIG_INVALID`: the caller fixes input before anything runs |
| 02-D4 | A system layer (`/etc/<app>/config.toml`) | No: absent on Windows and rare for agent CLIs; O-016 names it only as an example |
| 02-D5 | May a config file set framework options (`format`, `timeout`) | No: a file must not flip an agent's JSON; env vars already cover them |
| 02-D6 | Should `--instance-id` also namespace the project file | No: it belongs to the project and is locked; instances isolate user-level state |

## Tasks

- [ ] `_env.py`: `app_var`, `KNOWN`; replace the four `TREATY_*` reads; `<APP>_FORMAT` error parity; `--help` names it
- [ ] Audit rule `env-prefix` with generated rename fix
- [ ] `App(settings=)`, `_settings.py` layers and coercion, settings injection in `_resources.py`, `CONFIG_INVALID`
- [ ] `meta.config_sources` and `meta.effective_config_hash` on every envelope
- [ ] Global flags `--config`, `--context`, `--no-config` in `split_globals` and manifest `flags`; `meta.context`; `--config` as the write target
- [ ] `--show-config` with sources, precedence, and redaction
- [ ] `--instance-id`, `<APP>_INSTANCE_ID`, namespaced `user_config` and `state_dir`, `meta.instance_id`; lock local config writes
- [ ] Make `Ctx.config` private
- [ ] `Init` protocol, `init` built-in, `INIT_REQUIRED` and `INIT_FAILED`, audit rule `init-isolated`
- [ ] Spec PR for manifest `environment` (02-D1), then emit it
- [ ] Update examples, scaffold, tutorial, COMPLIANCE rows, README, HANDOFF, ROADMAP; changelog under Breaking
