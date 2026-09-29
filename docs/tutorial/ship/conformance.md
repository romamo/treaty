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
  | jq -e '.data.levels == {"level_1": "pass", "level_2": "pass", "level_3": "pass"}'
```

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

treaty builds most of this in, so a treaty app passes a lot of it without any work. The
kit still earns its place: it tests the executable people actually install, including its
launcher, its environment, and any handler that writes to stdout itself.

## Step 1: Get the kit

The kit ships with the spec. Clone it next to your project, where treaty looks by default:

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

- one `read` probe per safe command, from its first example: `todo list --all`
- one `destructive` probe per destructive command, from its first example with the
  confirmation removed: `todo purge`. The kit runs it with `--dry-run`, and again with no
  flags to check that it is refused
- `version`, and an `invalid` probe that adds `--no-such-flag` to the first probe

Mutating commands are never probed. Destructive ones are, and the kit is there to check
exactly the safety you might have got wrong. If `purge` ignored `--dry-run`, a run against
your real `~/.todo.json` would delete your completed items.

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

Save it next to the profile, named after the app, and make it executable (`chmod +x`).
`treaty conformance` finds it there and writes `"command": ["./todo"]` into the profile.
Add `.sandbox/` to `.gitignore`.

Point the sandbox at whatever your CLI touches: an environment variable for a config file,
a test account's credentials, a mock server's URL. A project made with `treaty init` already
has a launcher, without a sandbox; add one as soon as a command reads or writes real state.

On Windows the `/bin/sh` launcher cannot run. treaty falls back to the app's console script
in the current environment, or you pass `--command` with an executable of your own.

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
Profile: examples/tutorial/conformance/todo.json (4 probes)
Levels: level_1 pass, level_2 pass, level_3 pass

  pass  L1 no_hang_stdin_closed
  pass  L1 no_hang_stdin_open
  ...
  pass  L3 argument_order
```

Without `--run`, it only writes the profile. treaty derives the profile from the current
commands and their examples, so change those rather than the file. A command with required
arguments and no example gets no probe at all, which is one more reason the audit asks for
examples.

treaty never overwrites a profile silently. When the commands change, the next run finds that
the profile on disk differs from the one it would write and stops with exit 6, `CONFLICT`,
naming the probes that changed, since a differing profile may hold probes someone wrote by
hand. Pass `--force` to replace it with the generated one. A profile that already matches is
left as it is.

If you need probes treaty cannot derive, such as a bad enum value, keep them in a profile of
your own, beside the generated one, and run the kit on it directly, as the README's
[Conformance](../../../README.md#conformance) section does for `deployctl`.

**Check:** the chapter's **Done when** command

## What a failure looks like

The same profile, run against the argparse version from the migration chapter (its own
launcher, `todo-argparse`, uses the same sandbox):

```bash
$ uv run treaty conformance examples.tutorial.todo_exit_codes:app --out tmp/argparse/todo.json \
    --command examples/tutorial/conformance/todo-argparse --run --format plain
treaty: CONFORMANCE_FAILED: 7 conformance checks failed
  summary: {'passed': 5, 'failed': 7, 'skipped': 0}
Profile: tmp/argparse/todo.json (4 probes)
Levels: level_1 fail, level_2 fail, level_3 fail

  pass  L1 no_hang_stdin_closed
  fail  L1 no_hang_stdin_open
        purge: no exit within 10s; killed
  fail  L1 json_envelope
        purge: stdout is not a single JSON document (Expecting value at char 0)
        ...
  pass  L1 exit_code_contract
  ...
  fail  L1 help_off_stdout
        --help: --help wrote prose to stdout in a non-TTY; route it to stderr
  fail  L1 dry_run_preview
        purge --dry-run: dry-run exited 2, expected 0
  fail  L2 destructive_refuses_unconfirmed
        purge: expected exit 2 (refused before side effects) without confirmation, got 1
  fail  L3 manifest_valid
        manifest: stdout is empty
  fail  L3 argument_order
        argument_order --format json before the command path: --format json before the command path exited 2, expected 0
```

Every failure names the probe that caused it, and each one is something an earlier chapter
fixed:

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
