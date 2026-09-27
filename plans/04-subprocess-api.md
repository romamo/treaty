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

- [x] `_subprocess.py`: `run`, `pipeline`, `Completed`, env builder, group kill
- [x] `ctx.run`, `ctx.pipeline`, `ctx.open_url`, `ctx.headless`; `meta.headless`
- [x] `SUBPROCESS_FAILED` in the framework table; `SHELL_STRING_PROHIBITED` error
- [x] Integration with `Cancellation` and `cleanup=` so signals reach tracked children
- [x] Audit rule `no-shell-string`; `gui_operations=` declaration
- [x] README section "Running programs"

## Deviations as built

- `SUBPROCESS_FAILED` and `SHELL_STRING_PROHIBITED` are error codes on `GENERAL_ERROR`
  (exit 1), not framework table entries: exit 1 is implicit on every command, so nothing
  new to declare
- F-062's "registration time" is a real registration check, not only an audit rule:
  `_scan.ctx_calls` parses the handler source and `build_command` refuses a string literal,
  f-string, or concatenation where `ctx.run`/`ctx.pipeline` take an argument list, and any
  `shell=` keyword (F-044's "registration error if `shell=True`"). A string that only
  exists at run time raises `SHELL_STRING_PROHIBITED` then. The audit rule is `no-shell`
  and covers what the API cannot see: `os.system`, `os.popen`, `shell=True` anywhere
- Environment: `EDITOR`, `VISUAL`, and `GIT_EDITOR` are `true`, as F-055 says, not
  `false`; the F-046 set (`MANPAGER`, `LESS`, `MORE`) is added. Not added: `CI=1`,
  `LC_ALL`, `TERM=dumb`, `GIT_TERMINAL_PROMPT=0`, which no P0 criterion needs (F-050 and
  F-066 are P1 and have their own opt-outs to design). Children start with
  `start_new_session=True`, so a credential prompt on `/dev/tty` fails instead of hanging
- `App.main()` writes the pager and editor settings into `os.environ` too, through the
  same `child_settings`, so programs started without `ctx.run` inherit them
- `Completed` has a `stage` field; for a pipeline `argv`, `returncode`, and `stage` name
  the first failing stage, else the last, and `stderr` joins every stage's. Stage stderr
  goes to anonymous temp files, so no reader threads
- `timeout=` given explicitly is used as is; only the default is the time left on the
  command deadline. A command timeout or signal terminates tracked children from the main
  thread, so the abandoned handler's child never outlives the run
- Headless (F-057) is: stdin or stdout not a terminal, `CI`, or no `DISPLAY` and no
  `WAYLAND_DISPLAY` where a display is needed (not macOS or Windows, or over `SSH_TTY`).
  `meta.headless` is on every envelope of such a run, argument errors and help included
- `ctx.open_url` needs `gui_operations=["browser_open"]` (checked at registration and at
  call time) and an `open_url` field on the output type; the framework fills
  `data.open_url` when the handler leaves it `None`. The manifest shows
  `headless_behavior: "emit_in_output"`, the only behavior; `skip` and `error` (C-024) are
  not built
- Not built: the F-030 session tracking file, F-062's debug-mode argv log (treaty has no
  debug mode; argv is a JSON array in every error context), and F-065's parent-shell
  `pipefail` warning, which a child process cannot observe

## Tests

- `ctx.run(["sh", "-c", 'printf "%s|" "$1"', "_", "; rm -rf /"])` prints the literal text
- `ctx.run("ls")` raises `SHELL_STRING_PROHIBITED`
- `"hello world"` and `"*.json"` arrive as one literal argument each
- A child `sh -c 'echo $PAGER $NO_COLOR $GIT_PAGER'` prints `cat 1 cat`; a grandchild too
- `git log` in a repo with 100 commits and `PAGER=less` in the parent env returns at once
- `false | true` as a pipeline raises `SUBPROCESS_FAILED` with `stage: 0`
- SIGTERM during `ctx.run(["sleep", "30"])` leaves no `sleep` process behind
- `ctx.open_url` without `DISPLAY` returns immediately; `meta.headless` is true
