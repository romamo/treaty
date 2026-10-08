# Run the conformance kit

**Goal:** the CLI passes the spec's conformance kit at all three levels, and CI fails when
it stops passing

**You need:** a strict audit that exits 0, such as the end of
[Declare exit codes](../core/exit-codes.md), and [uv](https://docs.astral.sh/uv/) on PATH

**Done when:** the kit reports every level as passing:

<!-- check -->
```bash
uv run treaty conformance examples.tutorial.todo_exit_codes:app \
  --out examples/tutorial/conformance/todo.json --run \
  | jq -e '.data.levels == {"level_1": "pass", "level_2": "pass", "level_3": "incomplete"}
    and ([.data.checks[] | select(.status != "pass") | .id]
      == ["stream_contract", "stream_sigint"])'
```

The kit groups its checks in three levels. Level 1 is what every CLI an agent calls must do:
never wait for input, write one JSON envelope, use the spec's exit codes, keep colour and
help text off stdout, exit 2 on a bad argument, and preview a destructive command under
`--dry-run`. Level 2 adds refusing a destructive call that is not confirmed. Level 3 adds a valid
manifest, flags that work in any position, and a stream's lines. `todo` has no streaming
command, so the two stream checks have no probe to run and level 3 reads `incomplete`
rather than `pass`. The table below gives each check's level.

In your own project, drop `--out`: the profile goes to `conformance/<name>.json`, where
the audit's `profile` rule looks for it. The tutorial keeps its files under
[`examples/tutorial/conformance/`](../../../examples/tutorial/conformance/) only because this
repository holds several apps.

## What the kit checks that the audit cannot

`treaty audit` reads declarations: what each command says about itself. The kit runs the
real executable the way an agent does (stdout piped, stdin closed or left open, `NO_COLOR`
set, arguments wrong on purpose) and checks what comes back:

| Check | Level | Fails when |
| --- | --- | --- |
| `no_hang_stdin_closed` | 1 | a probe does not exit with stdin closed |
| `no_hang_stdin_open` | 1 | a probe waits on an open stdin, such as a prompt |
| `json_envelope` | 1 | stdout is not exactly one JSON envelope |
| `exit_code_contract` | 1 | an exit code is not in the spec's table, or differs from `meta.exit_code` |
| `stdout_no_ansi` | 1 | piped stdout contains colour or cursor codes |
| `no_color_honored` | 1 | stdout or stderr still has colour codes with `NO_COLOR=1` |
| `help_off_stdout` | 1 | piped `--help` prints prose on stdout |
| `invalid_input_exit_2` | 1 | a bad flag or value exits with anything but 2 |
| `dry_run_preview` | 1 | a destructive command's `--dry-run` does not exit 0 with a preview |
| `destructive_refuses_unconfirmed` | 2 | a destructive command runs without confirmation |
| `manifest_valid` | 3 | `manifest` does not validate against the spec's schema |
| `argument_order` | 3 | `--format` stops working when moved before the command path |
| `stream_contract` | 3 | a stream line is not a JSON object, or the stream does not end on exactly one `"_summary": true` line or error envelope |
| `stream_sigint` | 3 | SIGINT mid-stream does not end it on a `CANCELLED` envelope with `data.partial` and exit 130 |

treaty builds most of this in, so a treaty app passes a lot of it without any work. The
kit still earns its place: it tests the executable people actually install, including its
launcher, its environment, and any handler that writes to stdout itself.

## Step 1: Get the kit

The kit ships with the spec, in the `cli-agent-spec` repository. Clone it next to your
project, under the folder name treaty looks for by default. The repository is
`cli-agent-spec`, but treaty's default folder is `../cli-agent-ergonomics`, so the clone
names it that on purpose:

```bash
git clone https://github.com/cli-agent-spec/cli-agent-spec ../cli-agent-ergonomics
```

Anywhere else works too: pass `--spec-dir PATH` or set `TREATY_SPEC_DIR`. A location you
name that has no `conformance/run.py` exits 4 (`PRECONDITION`) rather than falling back to
the default.

**Check:** the kit's runner is where treaty looks for it

```bash
test -f "${TREATY_SPEC_DIR:-../cli-agent-ergonomics}/conformance/run.py"
```

## Step 2: Keep the probes away from real data

Probes run the real CLI. treaty derives them from your commands:

- one `read` probe per safe command, from its first example: `todo list --all`. A
  streaming command's probe adds `--no-stream --timeout 5`, so it ends with one envelope,
  the collected events or `TIMEOUT`, within the kit's 10-second limit on each run. An
  `output_file` command's probe drops the example's `--output PATH`, so it returns the
  data instead of writing the file on every run
- for each safe streaming command, a `stream` probe that reads its lines as they come, with
  a 10-second `deadline_seconds`, and one probe that sends SIGINT after the first line, for
  the kit's `stream_contract` and `stream_sigint` checks. Register a stream that never ends
  on its own with `endless=True`: it gets no `stream` probe, which would fail at the
  deadline, and takes the SIGINT probe; without one, the first safe stream takes it. A
  `has_network_io` stream gets neither, as it gets no `read` probe: each run would make its
  real requests, unless its example's `probe=` points it somewhere safe
- one `destructive` probe per destructive command, from its first example with the
  confirmation removed: `todo purge`. The kit runs it with `--dry-run`, and again with no
  flags to check that it is refused
- `version`, and `status`, a built-in that always exits 0
- when no command of yours is destructive, the built-in `cleanup` in its place, so the
  dry-run checks still run: the kit only ever previews it or sees it refused, and nothing
  is removed
- for each network command, an `invalid` probe with a malformed `--proxy`, and no `read`
  probe: each run of one would make the command's real requests. One whose example sets
  `probe=`, such as an argv pointing at a local stub (below), keeps its `read` probe
- two `invalid` probes: `manifest --etag x`, a malformed etag, and the first command's
  probe with `--no-such-flag` added, which exits 2 before anything runs
- for `argument_order`, the first example with a command-local option to move `--format`
  around, skipping network commands that get no `read` probe; without one, the built-in
  `manifest --etag` with an etag no manifest has

Mutating commands are never run: the only probes built from them are `invalid` ones, such as
a network command's malformed `--proxy`, which exit 2 before anything runs. A passthrough
command gets no probe at all: its tool owns stdout, where the kit looks for the envelope. Destructive ones
are, and the kit is there to check exactly the safety you might have got wrong. If `purge`
ignored `--dry-run`, a run against your real `~/.todo.json` would delete your completed
items.

So the kit runs a launcher, not the CLI directly, and the launcher points the CLI at a
sandbox. `todo` finds its item file through `Path.home()`, so the launcher sets `HOME`:

<!-- file: examples/tutorial/conformance/todo -->
```sh
#!/bin/sh
# Launcher the conformance kit executes. Probes run the real CLI, so HOME points at a
# sandbox here: todo's default item file is then a throwaway, never ~/.todo.json
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$here/.sandbox"
HOME="$here/.sandbox" exec "$here/../../../.venv/bin/python" "$here/../todo_exit_codes.py" "$@"
```

That one runs the example file from this repository, so do not copy it; your project's
launcher, below, runs your installed command instead.

A project made with `treaty init` already has a launcher, `conformance/todo`, without a
sandbox; a migrated project has none, so create one. Give it a sandbox as soon as a command
reads or writes real state; for `todo`, two lines give it its own `HOME`:

```sh
#!/bin/sh
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$here/.sandbox"
HOME="$here/.sandbox" exec "$here/../.venv/bin/todo" "$@"
```

Save it next to the profile, named after the app (`conformance/todo`), and make it
executable (`chmod +x`). `treaty conformance` finds it there and writes
`"command": ["./todo"]` into the profile. Add `.sandbox/` to `.gitignore`, creating the file
if the project has none.

### Beyond `HOME`

Point the sandbox at whatever else your CLI touches:

- **Settings**: an app with settings also reads its `TODO_*` variables and
  `XDG_CONFIG_HOME`, so its launcher clears them with `env -u`, or the kit reads the
  settings of the shell that started it
- **An API**: a CLI that calls one needs a server running for the whole kit run; start a
  fake one (the `http.server` fixture from [Declare network
  commands](../core/network-io.md) works as a script too) before `--run`, and point the
  launcher's environment at it. A network command gets a `read` probe only when its
  example's `probe=` names such a stub, as below
- **Things a probe names**: when a probe needs something to exist, such as a destructive
  command's example `restore 3`, the launcher seeds the sandbox with it; otherwise the dry
  run fails with your not-found code instead of previewing
- **A file a probe reads**: when an example names a file of the user's, give the probe its
  own argv on the example, pointing at a fixture you commit; agents still see the example's
  command. `probe=False` keeps an example out of the profile, and a command whose every
  example says so gets no probe at all:

```python
examples=[
    Example(
        "Import a list",
        "todo import ~/Downloads/list.json",
        probe="todo import conformance/fixtures/list.json",
    )
]
```

  The probe starts with the app's name like the command, and registration refuses one that
  does not parse or runs another command. Edit the probe here, not in the profile: the
  profile stays what treaty generates, so the next `treaty conformance` rewrites it without a
  `CONFLICT` and picks up new built-in probes

Without a launcher, the profile's command is the app's name, `todo`, found on `PATH`. Under
`uv run`, `PATH` starts with the project's environment, so the kit finds your `todo` there
and runs it against your real data, with no sandbox; only a command found nowhere exits 4,
with the kit's `INVALID_PROFILE` in `context.kit_error`. On Windows, where the `/bin/sh`
launcher cannot run, treaty uses the app's console script in the current environment
instead. Either way, `--command` names an executable of your own.

**Check:** the launcher runs the CLI against the sandbox, which holds no items: probes never
run a mutating command, and the destructive probe is only ever previewed or refused

<!-- check -->
```bash
examples/tutorial/conformance/todo list | jq -e '.ok and .data == []'
```

## Step 3: Write the profile and run the kit

```bash
$ uv run treaty conformance examples.tutorial.todo_exit_codes:app \
    --out examples/tutorial/conformance/todo.json --run --format plain
Profile: examples/tutorial/conformance/todo.json (6 probes)
Levels: level_1 pass, level_2 pass, level_3 incomplete

  pass  L3 argument_order
  pass  L2 destructive_refuses_unconfirmed
  pass  L1 dry_run_preview
  ...
  pass  L1 stdout_no_ansi
  skip  L3 stream_contract
  skip  L3 stream_sigint
```

The kit lists any failures first, then the passing checks by id, then the skipped ones.

The probe count is `todo`'s; yours follows your commands and examples.

`--run` stops with exit 6, `CONFLICT`, when the commands, the version, or the first examples
the probes come from changed since the profile was written, as they have in a project made
with `treaty init` until [Start a new
CLI](../A-new/start.md#step-9-rewrite-the-conformance-profile) Step 9. treaty never
overwrites a profile silently, since it may hold probes written by hand; `CONFLICT` names
the probes that changed. Replace it, then run the kit:

```bash
uv run treaty conformance todo.cli:app --force
uv run treaty conformance todo.cli:app --run
```

Without `--run`, it only writes the profile. treaty derives the profile from the current
commands and their examples, so change those rather than the file. A command with required
arguments and no example gets no probe at all, which is one more reason the audit asks for
examples.

If you need probes treaty cannot derive, such as a bad enum value, keep them in a profile of
your own, beside the generated one, and run the kit on it directly, as the reference's
[Conformance](../../reference.md#conformance) section does for `deployctl`.

**Check:** the chapter's **Done when** command

## What a failure looks like

The same profile, run against the argparse version from the migration chapter (its own
launcher, `todo-argparse`, uses the same sandbox):

```bash
$ uv run treaty conformance examples.tutorial.todo_exit_codes:app --out tmp/argparse/todo.json \
    --command examples/tutorial/conformance/todo-argparse --run --format plain
Profile: tmp/argparse/todo.json (6 probes)
Levels: level_1 fail, level_2 fail, level_3 fail

  fail  L1 help_off_stdout
        --help: --help wrote prose to stdout in a non-TTY; route it to stderr
  fail  L3 argument_order
        argument_order --format json before the command path: --format json before the command path exited 2, expected 0
  fail  L1 json_envelope
        list: stdout is empty
        ...
        purge: stdout is not a single JSON document (Expecting value at char 0)
        ...
  fail  L3 manifest_valid
        manifest: stdout is empty
  fail  L1 dry_run_preview
        purge --dry-run: dry-run exited 2, expected 0
  fail  L2 destructive_refuses_unconfirmed
        purge: expected exit 2 (refused before side effects) without confirmation, got 1
  fail  L1 no_hang_stdin_open
        purge: no exit within 10s; killed
  pass  L1 exit_code_contract
  ...
treaty: CONFORMANCE_FAILED: 7 conformance checks failed.
  summary: {'passed': 5, 'failed': 7, 'skipped': 0}
```

Every failure names the probe that caused it, and each one is something a migration to
treaty fixes. The fixes below are steps of the argparse chapter, since the failing version
is the argparse one; a CLI started with `treaty init` passes these checks from its first
command:

| Failed check | Cause in the argparse version | Fixed in |
| --- | --- | --- |
| `no_hang_stdin_open` | `purge` waits on `input()` | [Step 6](../B-migrate/argparse.md#step-6-replace-the-prompt-with-a-danger-level) |
| `json_envelope` | output is prose | [Step 4](../B-migrate/argparse.md#step-4-migrate-one-command-end-to-end) |
| `dry_run_preview`, `destructive_refuses_unconfirmed` | no `--dry-run`, and failures exit 1 | [Step 6](../B-migrate/argparse.md#step-6-replace-the-prompt-with-a-danger-level) |
| `manifest_valid` | no `manifest` command | [Step 2](../B-migrate/argparse.md#step-2-create-the-app) |
| `argument_order`, `help_off_stdout` | argparse's parser | the migration as a whole |

The command exits 80 with `CONFORMANCE_FAILED`, and `data` still holds every check, so an
agent reads the failures from JSON:

```bash
uv run treaty conformance myapp.cli:app --run | jq -c '[.data.checks[] | select(.status == "fail") | .id]'
```

## Step 4: Gate CI on both

Run the audit and the kit in CI. Both exit non-zero on failure:

```bash
uv run treaty audit myapp.cli:app --strict
TREATY_SPEC_DIR=/path/to/the/kit uv run treaty conformance myapp.cli:app --run
```

`TREATY_SPEC_DIR` names wherever CI cloned the spec repository; without it, treaty looks in
`../cli-agent-ergonomics`, which is why Step 1 clones it there. The folder name matters only
for that default.

Commit the profile and the launcher. When a pull request changes the commands in a way that
changes the probes, the kit step fails with `CONFLICT` until someone runs
`treaty conformance myapp.cli:app --force` and commits the new profile, so the probe changes
show up in review.

## Next

Every check passes: agents can call `todo` from a shell and rely on what it says. The next
step is serving the same commands as MCP tools: [Serve commands over MCP](mcp.md).
