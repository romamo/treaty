# 09: Session and process hygiene

Size: L

Keeps one run from leaking into the next or into the agent's session: no update chatter,
English and dot-decimal output from children, a private temp directory removed on exit, an
unchanged working directory, and nothing but envelopes on descriptor 1. Phase B: every
item is additive, but the global flag names below are reserved at 1.0 (see 09-D1).

| Requirement | Priority | Now | Gap | API change |
|-------------|----------|-----|-----|------------|
| F-029 Auto-update suppression | P1 | Partial | Holds only because treaty never checks; no hook for apps | Additive: `App(update_check=)`, `meta.update_available` |
| O-020 `--no-update-check` | P1 | Not started | No flag, no `<APP>_NO_UPDATE` | Additive: global flag |
| F-050 Update notifier side channel | P1 | Not started | Children get no `CI=1` or `NO_UPDATE_NOTIFIER` | Additive: `app.suppress_update_notifier()`; children's env changes |
| F-066 Subprocess locale | P1 | Not started | No `LC_ALL` for children | Additive: `preserve_locale=`; children's env changes |
| F-060 Third-party stdout | P1 | Partial | fd 1 goes to stderr uncounted; warning has no `text`; import-time prints escape | Additive: `context.text`, `treaty.intercept_stdout()` |
| F-032 Session temp dir | P2 | Not started | None | Additive: `ctx.tmp_dir`, `meta.session_tmp_dir` |
| F-043 Temp auto-cleanup | P2 | Not started | None | Additive: `ctx.output_file()`, `data.cleanup` |
| F-030 Child session tracking | P2 | Partial | Children tracked in memory only | Additive: `meta.session_pid_file` |
| F-041 CWD immutability | P2 | Not started | `os.chdir` in a handler persists | Additive: `CWD_CHANGED` warning, audit rule |
| O-017 `--cwd` | P2 | Not started | No flag, no `meta.cwd` | Additive: global flag, `ctx.cwd`, `meta.cwd` |
| O-018 `--no-cache`, `--cache-ttl` | P3 | Not started | No cache declaration (C-011 not started) | Additive: `cache=`, `ctx.cache`, `meta.cache_used` |

## API impact

- **New `App` surface**: `update_check=`, `app.suppress_update_notifier(fn)`; exports
  `UpdateCheck`, `CachePolicy`, `intercept_stdout`
- **New `App.command` keywords**: `preserve_locale=False`, `cache=None`
- **New `Ctx` members**: `cwd`, `tmp_dir`, `temp_file()`, `output_file()`, `cache`
- **Global flags**: `--cwd PATH`, `--no-update-check`; on `cache=` commands only:
  `--no-cache`, `--cache-ttl SECONDS`. Env var `<APP>_NO_UPDATE`
- **Envelope**: `meta.cwd` on every response; `meta.session_tmp_dir`,
  `meta.session_pid_file`, `meta.update_available`, `meta.cache_used` when they apply;
  `data.cleanup` beside `data.open_url`; `THIRD_PARTY_STDOUT` gains `context.text`; new
  warning `CWD_CHANGED`
- **Behavior change, not API**: children of `ctx.run` get `LC_ALL=C`, `LC_NUMERIC=C`,
  `TMPDIR` set to the session dir, and off a terminal `CI=1` and the notifier variables;
  a child that relied on the user's locale needs `preserve_locale=True`

## Design

### Update checks and notifiers (F-029, O-020, F-050)

Treaty ships no update checker (no network in core); an app may pass one:

```python
class UpdateCheck(Protocol):
    def latest(self, current: str, timeout: Timeout) -> str | None: ...
```

It runs only when stdin and stdout are terminals, `CI` is unset, `<APP>_NO_UPDATE` is
unset, and `--no-update-check` is absent. It never delays a run: a daemon thread refreshes
`<state dir>/update.json` at most daily, and the run reads only the cached answer, setting
`meta.update_available` when newer and printing one stderr line in plain mode. The flag
and variable exist on every app, checker or not, so agents can pass them blindly. Treaty
never replaces a binary, so "never replaced while running" is not applicable.

`_mode.child_settings` gains, when not interactive or under `CI`: `CI=1`,
`NO_UPDATE_NOTIFIER=1`, `NPM_CONFIG_UPDATE_NOTIFIER=false`, `HOMEBREW_NO_AUTO_UPDATE=1`,
`PIP_DISABLE_PIP_VERSION_CHECK=1`, `GH_NO_UPDATE_NOTIFIER=1`. `App.main()` already writes
these into `os.environ` through `quiet_children` before dispatch; `_Run._ctx` adds them to
`Processes`. `app.suppress_update_notifier(fn)` registers
`fn: Callable[[MutableMapping[str, str]], None]`, called in both places, for libraries
with their own switch. On a terminal nothing is set, so notices show as usual.

### Child locale (F-066)

`_Run._ctx` adds `LC_ALL=C` and `LC_NUMERIC=C` to the `Processes` env unless the command
has `preserve_locale=True`; `quiet_children` does not, so the parent's environment and
locale are untouched. Python children still get UTF-8 through PEP 540's C-locale UTF-8
mode. Audit rule `preserve-locale` (advice) lists opted-out commands with the fix "remove
`preserve_locale`, or say in `description` why child output is localized".

### fd-level stdout interception (F-060)

`App.main()` already points descriptor 1 at stderr and writes envelopes to a saved copy.
Replace the `dup2(stderr, 1)` with a pipe: a reader thread tees everything to stderr and
keeps the first 4 KiB and a byte count. Before `_Run._write` builds the envelope, it
writes a unique marker to fd 1 and waits for the reader to see it, so nothing written
earlier is missed. `_StrayStdout` and the pipe feed one `THIRD_PARTY_STDOUT` warning with
`context.text` (cut to 4 KiB) and `context.bytes`. A write that parses as JSON goes to
stderr without a warning; the spec's "discard" becomes "not on stdout", which loses nothing.

Import-time prints happen before `main()`. `treaty.intercept_stdout()` installs the same
pipe early and returns the handle `main()` then adopts; the scaffold's entry module calls it
before importing the app (09-D3). `--debug` attribution is not applicable: treaty has no
`--debug`, and the text already reaches stderr.

### Session temp dir and cleanup (F-032, F-043, F-030)

New `_session.py`. The root is `<tempfile.gettempdir()>/<app>-<uid>/` (plus
`instances/<id>/` under 02's `--instance-id`), created `0700`, its owner checked, and
refused if it is a symlink. The session dir is `<root>/<request_id>/`, made lazily by the
first `ctx.tmp_dir`, `ctx.temp_file()`, or child spawn, with `os.mkdir(mode=0o700)`
followed by an explicit `chmod`, so umask never widens it. `temp_file()` uses
`os.open(..., O_CREAT | O_EXCL, 0o600)`. Children get `TMPDIR`, `TEMP`, and `TMP` pointing
at it. `meta.session_tmp_dir` is set once it exists. `_Run` removes it after the last
envelope on every path: success, error, timeout, and the `_cancelled` signal path.

- `ctx.output_file(name, keep_seconds=300)` returns a `0600` path under `<root>/out/
  <request_id>/` that survives the run; the framework adds `data.cleanup` with `command`
  (`rm -f '<path>'`, quoted by `shlex.quote`) and `auto_cleanup_after_seconds`, following
  the `_with_open_url` rule for object data
- No daemon: each run starts with one `os.scandir` of `<root>` and removes `out/` entries
  past their expiry and session dirs older than 24 hours
- F-030: `Processes` rewrites `<session dir>/children.pids` on each spawn and reap, and an
  `atexit` hook calls `terminate` for abnormal exits (SIGTERM forwarding is done);
  `meta.session_pid_file` appears only when children are still tracked when the envelope
  is built (09-D2). Stale PID files are deleted, never signaled: PIDs get reused

### Working directory (F-041, O-017)

`--cwd PATH` is a global flag, validated in `split_globals`' caller before any side
effect: missing or not a directory exits 2 (`ARG_ERROR`, `phase: validation`). `ctx.cwd`
is it, absolute, or the process CWD at start; `meta.cwd` reports it. Relative `Path`
fields are joined to `ctx.cwd` after `check_path` when `--cwd` is given; `ctx.run` and
`ctx.pipeline` default `cwd=` to it; 02's project config file is found from it. The
process CWD never changes.

`_Run._execute` records `os.getcwd()` before resources and handler; if it differs after,
it restores it and adds `CWD_CHANGED` with `context.from` and `context.to`. Audit rule
`no-chdir` flags `os.chdir`, `contextlib.chdir`, and `os.fchdir` in handlers and
`acquire`, with the fix "build paths from `ctx.cwd`, or pass `cwd=` to `ctx.run`".

### Cache flags (O-018)

`cache=CachePolicy(ttl_seconds=3600)` gives the command `ctx.cache` with `get(key) ->
bytes | None` and `put(key, data)`, stored under `$XDG_CACHE_HOME/<app>/<command>/` by
sha256 of the key, and the flags `--no-cache` and `--cache-ttl`. `--no-cache` and
`--cache-ttl 0` make `get` miss and `put` a no-op; an entry older than the TTL misses.
`meta.cache_used` is true when a `get` hit. Commands without `cache=` have neither flag.
Audit rule `cache-declared` warns when a handler writes under `XDG_CACHE_HOME` or
`~/.cache` without `cache=`.

## Decisions

| ID | Question | Recommendation |
|----|----------|----------------|
| 09-D1 | New global flags shadow app fields named `cwd` and fail registration, which is breaking after 1.0 | Reserve `cwd` and `no-update-check` in `framework_collisions` before the tag, even if 09 lands later |
| 09-D2 | F-030's schema says `session_pid_file` is present when children spawned; its example omits it once they exited | Only while children remain tracked: a path to a deleted file helps no one |
| 09-D3 | F-060 wants interception before imports; a console script imports the app first | Export `intercept_stdout()` and have the scaffold's entry call it before the import |
| 09-D4 | `LC_ALL=C` or `C.UTF-8` | `C`, as the spec says; `C.UTF-8` is missing on some systems |
| 09-D5 | O-018 is P3 and needs a cache declaration that C-011 would own | Build `cache=` here; flags appear only on opted-in commands, so it can slip past 1.0 safely |

## Tasks

- [x] Reserve `cwd` and `no-update-check` global names (09-D1); now in `IMPLEMENTED`
- [x] Notifier variables in `child_settings`; `suppress_update_notifier`. `App.main()`
  passes the run `CI` as it was started, so `CI=1` for libraries never turns a
  terminal's plain output into JSON
- [x] `UpdateCheck`, cached daily refresh, `meta.update_available`, `--no-update-check`,
  `<APP>_NO_UPDATE`. `latest(current, timeout)` takes the timeout as float seconds, not
  `Timeout`, so a checker hands it straight to its HTTP call
- [x] `LC_ALL` and `LC_NUMERIC` for children; `preserve_locale=`; audit rule `preserve-locale`
- [x] Pipe-based fd 1 capture with marker sync; `context.text`; `intercept_stdout()`;
  scaffold entry (`entry.py`, which imports `cli.py` after the interception). Lines of
  JSON are left out of `context.text` rather than the whole write; F-060 stays Partial
  until `--debug` (11) can attribute the text
- [x] `_session.py`: root, session dir, `ctx.tmp_dir`, `ctx.temp_file()`, child `TMPDIR`,
  removal on every exit path, through a `last=True` hook on 06's `Teardown`, so
  `cleanup=` can still use the directory. A symlinked or foreign root exits 4 with
  `TEMP_DIR_UNSAFE`
- [x] `ctx.output_file()`, `data.cleanup`, pruning at run start (on the first command of a
  run, not on help). Output files live in `out/<expiry>-<request_id>/`; the `cleanup`
  built-in from 08 removes them instead of a new command
- [x] `children.pids`, `meta.session_pid_file`. No `atexit` hook: every exit path already
  stops the tracked children (pipeline's own handler, timeout, signals)
- [x] `--cwd`, `ctx.cwd`, `meta.cwd`, relative `Path` resolution, `ctx.run` default
- [x] CWD restore with `CWD_CHANGED`; audit rule `no-chdir` (warning)
- [x] `CachePolicy`, `ctx.cache`, `--no-cache`, `--cache-ttl`, `meta.cache_used`, audit rule
  `cache-declared`. A `cache=` command also declares its cache in
  `filesystem_side_effects`, and `cleanup` removes it wherever `XDG_CACHE_HOME` put it
- [x] Update COMPLIANCE rows, README, HANDOFF, ROADMAP
