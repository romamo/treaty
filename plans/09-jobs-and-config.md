# 09: Async jobs and config writes

Two domain contracts treaty exposes as declarations plus one built-in each (decision D4).

| Requirement | Level | Now | Gap |
|-------------|-------|-----|-----|
| C-022 Async commands declare a job descriptor | 2 | Not started | No async concept |
| C-025 Config-writing commands declare write scope | 2 | Not started | No config concept |

## Async jobs (C-022)

### Declaration

```python
@app.command("deploy.start", description="...", exit_codes=(), danger_level="mutating",
             async_job=True)
def start(args: StartArgs, ctx: Ctx) -> Job:
    job_id = backend.submit(args)
    return Job(id=job_id, status="pending", effect="created")
```

- `async_job=True` requires the output type to be `treaty.Job` or a dataclass extending it
  (registration error otherwise). `Job` fields follow the spec's job descriptor schema:
  `id`, `status` (`pending`, `running`, `complete`, `failed`), `created_at`,
  `status_command`, `poll_interval_ms`. The framework fills `status_command` with
  `<app> job status <id>`
- The manifest and `--schema` show `async: true` and the job descriptor schema

### `job status` built-in

Registered when any command declares `async_job=True`. The app supplies
`App(jobs=JobStore)`, a protocol with `status(job_id) -> Job | None`. Exit codes per the
spec: `0` complete, `3` still running, `4` failed, `5` not found. In treaty's table 3 is
`PARTIAL_FAILURE` and 4 is `PRECONDITION`, so the built-in declares its own entries on
those numbers (`JOB_RUNNING` retryable with `retry_after_ms` from `poll_interval_ms`,
`JOB_FAILED` not retryable) and reuses `NOT_FOUND` for 5.

## Config writes (C-025)

### Declaration

```python
@app.command("config.set", description="...", exit_codes=(), danger_level="mutating",
             config_write_scope="project")
def set_(args: SetArgs, ctx: Ctx) -> Written:
    ctx.config_path  # ./.deployctl.toml, or ~/.config/deployctl/config.toml with --global
```

- `config_write_scope="project" | "user"` adds `--global` to project-scoped commands and
  sets `ctx.config_path`: project scope is `./.<app>.toml`, user scope is
  `$XDG_CONFIG_HOME/<app>/config.toml`
- A write to the user path adds a `GLOBAL_CONFIG_MODIFIED` warning
- `ctx.write_config(text)` writes through a same-directory temp file and `os.replace`,
  reusing the atomic write in `_idempotency.py` (promote it to `_atomic.py`; F-070 comes
  free)
- The `config-write-scope` audit rule warns on commands named `config.*` or `*.set`
  without the declaration; the spec asks for a warning, not an error

## Tasks

- [x] `Job`, `async_job=`, `JobStore` protocol, `job status` built-in
- [x] `config_write_scope=`, `--global`, `ctx.config_path`, `ctx.write_config`
- [x] `_atomic.py` shared by idempotency and config writes
- [x] Audit rules `async-job` and `config-write-scope`
- [x] Extend `examples/deployctl.py` with `deploy.start` and `config.set` for the kit

## Tests

- `async_job=True` with a non-`Job` output fails registration
- `job status` returns exit 0, 3, 4, 5 for the four states
- `config.set` writes `./.deployctl.toml`; with `--global` writes the user path and warns
- A write killed between temp file and rename leaves the old config intact

## Deviations as built

- **The descriptor follows the spec, not this plan.** `Job(job_id, status,
  poll_interval_ms=5000, timeout_ms=600000, effect=None)` with `status` one of `running`,
  `complete`, `failed`, `cancelled`; the framework adds `terminal`, `status_command`, and
  `cancel_command`. No `pending` or `created_at`. `effect` lets a mutating async command
  meet REQ-C-003
- **`job cancel` too.** The spec's descriptor carries `cancel_command`, so `JobStore` has
  `status(job_id, ctx)` and `cancel(job_id, ctx)`; `job cancel` is mutating and fills
  `effect: updated` when the store leaves it empty. Both built-ins are registered by
  `App(jobs=)`, and `async_job=True` without it is a registration error
- **`job status` reuses framework entries.** The registry holds one entry per number, so 3
  is `PARTIAL_FAILURE` with code `JOB_RUNNING`, 4 is `PRECONDITION` with `JOB_FAILED` or
  `JOB_CANCELLED`, 5 is `NOT_FOUND` with `JOB_NOT_FOUND`, each with the job as `data`.
  `PARTIAL_FAILURE` is not retryable, so a running job carries `poll_interval_ms` in its
  context and suggestion instead of `retry_after_ms`
- **Scopes use the spec's names.** `config_write_scope="local"` (project file, `--global`
  for the user file) and `"global"` (user file only, exit 2 without `--global`); `session`
  has no file to name and is refused. The project file is `./.<app>.toml` in the working
  directory, not the nearest one up the tree. A config command must be mutating
- **Only a global write locks**, on `<file>.lock` next to it, left in place like the
  idempotency locks; a project file is not shared between sessions the same way
- **The registration warning is the `config-write-scope` audit rule** (a warning), like
  treaty's other name heuristics; treaty cannot see file writes, only names. Calling
  `ctx.write_config` without the declaration is stronger: a registration error, found by
  scanning the handler. `async-job` is advice for `start`, `submit`, `enqueue`, `launch`,
  and `trigger` commands
- **`_atomic.write_atomic` uses `mkstemp`**, so concurrent writers never share a temp file,
  fsyncs before the rename, keeps an existing file's mode, and removes the temp file on
  failure. `--output` goes through it too, which completes F-070
- **The interrupted-write test fails the write with an unencodable character** after the
  temp file is open, rather than killing a process between write and rename
- **The example store is a stub**: `deployctl`'s `Deployments` reports every `deploy-*`
  job complete; `tests/test_jobs_config.py` uses a store with all four states
