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

- [ ] `Job`, `async_job=`, `JobStore` protocol, `job status` built-in
- [ ] `config_write_scope=`, `--global`, `ctx.config_path`, `ctx.write_config`
- [ ] `_atomic.py` shared by idempotency and config writes
- [ ] Audit rules `async-job` and `config-write-scope`
- [ ] Extend `examples/deployctl.py` with `deploy.start` and `config.set` for the kit

## Tests

- `async_job=True` with a non-`Job` output fails registration
- `job status` returns exit 0, 3, 4, 5 for the four states
- `config.set` writes `./.deployctl.toml`; with `--global` writes the user path and warns
- A write killed between temp file and rename leaves the old config intact
