# 08: Additional command declarations

Size: M

Adds the optional declarations an agent reads before it runs a command: what outlives
the process, what lands on disk, what the host must provide, and which child binary gets
which argument. Each is a keyword with a manifest field and an audit rule, so it can land
after the 1.0 freeze except the two tightenings called out under API impact. The
`doctor`, `status`, and `cleanup` built-ins, `SideEffect`, and `Dependency` are built in
[13](13-built-ins.md) (13-D4); this file owns the rest of C-011 and O-031 around them.

| Requirement | Priority | Now | Gap | API change |
|-------------|----------|-----|-----|------------|
| C-010 Background-process metadata | P2 | Not started | Session teardown kills every child; no way to start one that outlives the run | Additive: `background=`, `ctx.spawn()` |
| C-011 Filesystem side effects | P3 | Not started | No declaration, listing, or cleanup | Additive: declaration and built-ins in 13; audit rule here |
| C-018 Platform and required tools | P3 | Not started | No declaration or warning | Additive: `platform=`, `required_tools=` |
| C-019 Subprocess argument schema | P1 | Partial | `os.system` is only an audit warning; no `subprocess` section | Yes: shell calls refused at registration; additive `subprocess=` |
| C-024 GUI headless behavior | P1 | Partial | Only `emit_in_output`, implied | Yes: `headless_behavior=` required with `gui_operations` |
| O-031 Dependency version matrix | P1 | Not started | No declarations, no `doctor` | Additive: `Dependency` and `doctor` in 13; manifest root `dependencies` here |

## API impact

Additive: `App.command` gains `background=`, `platform=`, `required_tools=`,
`subprocess=`, and `headless_behavior=`; `Ctx` gains `spawn()`. New exports:
`Background`, `Spawned`, `Subprocess`. Manifest fields are the ones `CommandEntry` already defines
(`spawns_background_process`, `cleanup_command`, `max_lifetime_seconds`,
`filesystem_side_effects`, `platform`, `required_tools`, `subprocess`,
`headless_behavior`) plus the root `dependencies` array, so no schema change is needed.

Breaking, to land before the freeze with 04:

- A handler whose source calls `os.system`, `os.popen`, or `subprocess.*(shell=True)`
  fails registration (today only the `no-shell` audit rule warns)
- `gui_operations` without `headless_behavior=` fails registration, as C-024 requires;
  the fix message says `headless_behavior="emit_in_output"`, today's behavior

## Design

### Background processes (C-010)

```python
@dataclass(frozen=True, slots=True)
class Background:
    cleanup_command: str          # "tool stop-watcher"
    max_lifetime_seconds: int     # >= 1

@dataclass(frozen=True, slots=True)
class Spawned:
    pid: int
    log_path: Path
```

- `ctx.spawn(argv)` starts a child in its own session with stdin from `/dev/null` and
  output to a log under `state_dir`, and leaves it out of `Processes` teardown (REQ-F-030
  still covers every `ctx.run` child). The pid and deadline go to
  `<state_dir>/background/<command>.pids`; each later spawn and the cleanup command reap
  expired entries
- `_check_ctx_calls` refuses `ctx.spawn` without `background=`, as it refuses
  `ctx.open_url` without `gui_operations`; `can_carry` requires `background_pid` and
  `cleanup_command` fields on the output type
- `cleanup_command` must route to a registered command; checked when the manifest is built,
  since registration order is free
- Audit rule `background-declared`: `subprocess.Popen(..., start_new_session=True)` or
  `os.fork` in a handler, fix `ctx.spawn(...)` with `background=`

### Filesystem side effects (C-011)

13 adds `filesystem_side_effects=[SideEffect(...)]`, `--schema` output, `status
--show-side-effects`, and `cleanup`, which meet all three criteria. This file adds the
manifest field (`CommandEntry` defines it, while 13 emits it only in `--schema`), the
`clearable_with` field 13 leaves out (checked to route to a registered command when the
manifest is built), and the audit rule `fs-side-effects` (advice): `write_text`,
`write_bytes`, `mkdir`, or `open(..., "w")` in a handler with no declaration and no
`output_file=True`.

### Platform and required tools (C-018)

`platform=("linux", "darwin")` is checked against `sys.platform` values at registration.
Running elsewhere adds an `UNSUPPORTED_PLATFORM` warning to the envelope and still runs.
`required_tools={"dpkg-deb": "1.19.0"}` becomes one 13 `doctor` check per tool:
`shutil.which`, then the version from an app `Dependency` of the same name or from
`<tool> --version`. The
`shell_requirements` field has no `CommandEntry` key and treaty never runs a shell: not
applicable. Audit rule `required-tools`: a literal binary in `ctx.run` that is not in
`required_tools`.

### Subprocess schema and shell lint (C-019)

- Move `shell_calls` from `_audit.py` to `_scan.py`; `_check_ctx_calls` raises
  `RegistrationError` on any hit, naming the line and suggesting `ctx.run([...])`
- `Subprocess(binary, user_controlled_args, hardcoded_args)` is derived from `ctx.run`
  calls whose argv is a list literal: a constant first element is the binary, `args.<f>`
  items are user-controlled, other constants are hardcoded. `subprocess=` overrides or
  supplies it; names must be real fields. Emitted in manifest and `--schema`
- Declared user-controlled fields get a phase-1 check refusing shell metacharacters and a
  leading `-`, exit 2 `SHELL_METACHARACTER`
- Audit rule `subprocess-declared`: `ctx.run` whose argv cannot be derived and no
  `subprocess=`

### Headless behavior (C-024)

`headless_behavior` is a `StrEnum` (`emit_in_output`, `skip`, `error`) stored on
`Command`, replacing the literal in `command_entry`. `skip` makes a headless
`ctx.open_url` return False with a `GUI_SKIPPED` warning; `error` raises exit 4
(`PRECONDITION`) with the URL in `context`. `_check_gui` requires the `open_url` output
field only for `emit_in_output`. `meta.headless` and non-headless behavior are done.

### Dependency matrix (O-031)

13 builds `Dependency`, `App(dependencies=...)`, and the `doctor` checks, including the
`max_version` warning and `DOCTOR_CHECKS_FAILED`. This file adds what 13 leaves out:
the manifest root `dependencies` array (`DependencyEntry`, in the etag), with
`check_command` stored as an argv tuple, since treaty never runs a shell, and emitted as
`shlex.join`; and a dotted-numeric `Version` value object in `_deps.py` (no `packaging`)
that both files compare with. `fix_command` is published verbatim for the agent to run.

## Decisions

| ID | Question | Recommendation |
|----|----------|----------------|
| 08-D1 | C-019's metacharacter rejection is redundant with argument lists and may refuse real values | Apply it only to fields the author declares in `subprocess=`, not to derived ones |
| 08-D2 | `cleanup_command` and `clearable_with` name commands that may register later | Check them when the manifest is built and in `treaty audit`, not at registration |

## Tasks

- [ ] Move `shell_calls` to `_scan.py`; refuse shell calls at registration; migrate examples
- [ ] `headless_behavior=` required with `gui_operations`; `skip` and `error` paths
- [ ] `Subprocess` derivation, `subprocess=`, metacharacter check; audit rule
- [ ] `Version` in `_deps.py`; manifest root `dependencies` (after 13's `Dependency`)
- [ ] `platform=`, `required_tools=`, `UNSUPPORTED_PLATFORM`; `doctor` tool checks; audit rule
- [ ] Manifest `filesystem_side_effects`, `clearable_with`; audit rule `fs-side-effects`
- [ ] `Background`, `ctx.spawn`, pid files and reaping; audit rule
- [ ] Update COMPLIANCE rows, README, HANDOFF, ROADMAP
