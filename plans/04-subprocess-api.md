# 04: Subprocess API

One way for handlers to run other programs: argument lists only, a safe environment, and
failures that surface as envelopes. Five P0 requirements depend on it.

| Requirement | Level | Now | Gap |
|-------------|-------|-----|-----|
| F-044 Shell argument escaping | 2 | Not started | No subprocess API; newline half in 02 |
| F-046 Pager env suppression | 2 | Not started | No env injection for children |
| F-062 Glob and word-splitting prevention | 2 | Not started | No argv-only API, no `SHELL_STRING_PROHIBITED` |
| F-065 Pipeline exit code propagation | 2 | Not started | No pipeline API |
| F-055 `$EDITOR` no-op (child half) | 2 | Not started | Children can open an editor |
| F-057 Headless detection | 2 | Not started | No `DISPLAY` detection, no `meta.headless` |
| F-008, F-010 (child half) | 1 | Partial | Env defaults set process-wide in 01; applied explicitly here |

Also closes: F-030 and F-031 (child tracking and SIGTERM forwarding, P2), F-050 (`CI=1`
for children, P1), F-066 (`LC_ALL=C`, P1), C-019 (subprocess argument schema, P1).

## API

```python
result = ctx.run(["git", "log", "-1", "--format=%H"], timeout=ctx.timeout)
result.stdout  # str
result.returncode  # int, always 0 here: non-zero raises
```

- `ctx.run(argv: Sequence[str], *, input: str | None = None, cwd: Path | None = None,
  env: Mapping[str, str] | None = None, timeout: Timeout | None = None,
  check: bool = True) -> Completed`
- `ctx.pipeline([[...], [...]], ...) -> Completed` for `a | b | c`, each stage an argv list,
  connected with OS pipes, never a shell
- `Completed` is a frozen dataclass: `argv`, `returncode`, `stdout`, `stderr`, `duration_ms`

## Rules

- **No shell** (F-044, F-062): `argv` must be a list or tuple of `str`/`Path`. A `str`
  raises `SHELL_STRING_PROHIBITED`; there is no `shell=` parameter at all. Since handlers
  are plain functions, the "registration time" wording in F-062 is met by the
  `no-shell-string` audit rule, which flags `ctx.run("...")`, `subprocess.*(shell=True)`,
  and `os.system` in handler modules by AST scan
- **Environment** (F-008, F-010, F-046, F-050, F-055, F-066): the child env is the process
  env plus `NO_COLOR=1`, `PAGER=cat`, `GIT_PAGER=cat`, `CI=1`, `LC_ALL=C.UTF-8`,
  `TERM=dumb`, `GIT_TERMINAL_PROMPT=0`, and, when stdin is not a TTY, `EDITOR` and `VISUAL`
  set to `false` so a child that opens an editor fails at once instead of hanging.
  Grandchildren inherit it. The caller's `env=` overrides individual keys
- **Stdin**: `input=None` gives the child `/dev/null`, never the parent's stdin
- **Timeout**: defaults to the time left on the command deadline; expiry kills the child's
  process group and raises the framework `TIMEOUT` with `context.argv`
- **Failure** (F-065): a non-zero exit in any stage raises `CliExit` with code
  `SUBPROCESS_FAILED` (exit 1), `context` holding `argv`, `returncode`, `stage`, and the last
  4 KiB of stderr. `check=False` returns instead. In a pipeline the first failing stage wins,
  even when the last stage exits 0
- **Tracking** (F-030, F-031): children start in a new process group; the run keeps their
  PIDs; `Cancelled` and cleanup send SIGTERM, then SIGKILL after 2 s
- **Headless** (F-057): `Ctx.headless` is true when there is no `DISPLAY` and no
  `WAYLAND_DISPLAY` on Linux, or stdin is not a TTY; every envelope gets `meta.headless`
  then. `ctx.open_url(url)` launches the browser only when not headless; otherwise it
  returns without blocking and the command puts the URL in `data.open_url`. Commands that
  call it declare `gui_operations=["open_url"]` (C-024, P1)

F-065's "warn when `pipefail` is not set in the parent shell" is not observable from a
child process; record it as not applicable in `COMPLIANCE.md` with that reason.

## Tasks

- [ ] `_subprocess.py`: `run`, `pipeline`, `Completed`, env builder, group kill
- [ ] `ctx.run`, `ctx.pipeline`, `ctx.open_url`, `ctx.headless`; `meta.headless`
- [ ] `SUBPROCESS_FAILED` in the framework table; `SHELL_STRING_PROHIBITED` error
- [ ] Integration with `Cancellation` and `cleanup=` so signals reach tracked children
- [ ] Audit rule `no-shell-string`; `gui_operations=` declaration
- [ ] README section "Running programs"

## Tests

- `ctx.run(["sh", "-c", 'printf "%s|" "$1"', "_", "; rm -rf /"])` prints the literal text
- `ctx.run("ls")` raises `SHELL_STRING_PROHIBITED`
- `"hello world"` and `"*.json"` arrive as one literal argument each
- A child `sh -c 'echo $PAGER $NO_COLOR $GIT_PAGER'` prints `cat 1 cat`; a grandchild too
- `git log` in a repo with 100 commits and `PAGER=less` in the parent env returns at once
- `false | true` as a pipeline raises `SUBPROCESS_FAILED` with `stage: 0`
- SIGTERM during `ctx.run(["sleep", "30"])` leaves no `sleep` process behind
- `ctx.open_url` without `DISPLAY` returns immediately; `meta.headless` is true
