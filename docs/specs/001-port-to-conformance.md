# S-001: Port to conformance

status: approved
serves: G-4

## Problem

A maintainer moving an existing argparse, click, or typer CLI onto treaty (U-3) runs
`treaty scaffold-from`, then fixes what `treaty audit` reports. Today that path stops at
the handler stubs: every command is scaffolded `danger_level="mutating"` with
`exit_codes=()`, and nothing promises that the audit's next steps reach a passing kit
(romamo/treaty#472, from the accepted plan romamo/treaty#448, W-1). The new-project path
already has that promise, proven in CI: a `treaty init` scaffold passes the audit and the
kit.

Running the path on the repo's own fixtures shows where it falls short:

- A read command such as `inventory show` or `transaction list` stays mutating, and the
  audit's only warning on it asks for a `SHOW_FAILED` exit code, which cements the wrong
  danger level; the kit never probes a mutating command, so the profile treaty writes
  probes none of the ported commands, only the built-ins
- A command whose old CLI asked for `--yes` (argparse `restart`, typer `transaction add`)
  or whose name is destructive (click `release rollback`) stays mutating, and the audit
  says nothing: its danger-level rule only checks commands declared safe
- The audit's fix for a safe command with a destructive name adds a `dry_run` flag but not
  the `would_affect` output a destructive command must return, so applying it as written
  fails registration
- The `profile` finding's fix is `uv run conformance/run.py <profile>`, a path in the
  spec's checkout, rather than `treaty conformance`
- The kit runs the CLI by its name; after a port the project's console script of that name
  still runs the old CLI, and nothing says so

"Without opening the spec" means: every fix the audit gives on this path is a Python edit
it spells out (names and values, with a placeholder only for what only the author knows,
such as an exit code's description or a timeout) or a `treaty` command line; none points
at a requirement id, the spec, or the kit's own files.

## Behaviour

### `treaty scaffold-from` (`src/treaty/_scaffold_from.py`, `src/treaty/_cli.py`)

- Danger level: a command whose leading word (split on `-` and `_`, as the audit's
  `danger-level` rule splits it) is one of the audit's destructive verbs (`delete`,
  `remove`, `destroy`, `drop`, `purge`, `reset`, `rollback`, `wipe`), or whose old CLI had
  a confirmation switch scaffold-from leaves out (`--yes`, `--force-yes`, `--assume-yes`),
  is scaffolded `danger_level="destructive"`. Every other command stays
  `danger_level="mutating"`; scaffold-from never writes `danger_level="safe"`, since
  declaring a command safe lets agents run it freely and only the author knows it changes
  nothing. Both verb lists live in one place that `_audit.py` and `_scaffold_from.py`
  import
- A destructive command registers as scaffolded: its args keep the old CLI's boolean
  `--dry-run` when it had one, else gain `dry_run: bool = Flag(default=False, ...)`; its
  handler returns a frozen `<Command>Out` dataclass with `effect`, the echoed arguments,
  and `would_affect: Affects | None`, set when `dry_run` is. The module still passes ruff
  and `mypy --strict`
- `exit_codes=()` stays on every command: only the author knows the codes, and the audit
  asks for them
- The skipped entry for a left-out confirmation switch says the command was declared
  destructive, instead of asking the author to declare it
- The module docstring and the command's manifest description say which danger levels
  were inferred and that the rest are left for the author
- Output (`ScaffoldFromOut`) gains `next_steps`, an ordered list of strings, additive to
  the existing fields. With `--out` inside `--cwd`, the module path is the `--out` path
  relative to `--cwd` with a leading `src/` dropped and `/` read as `.`, such as
  `src/deploy/cli_treaty.py` as `deploy.cli_treaty`; the steps are, in order: point the
  console script at the new app (`[project.scripts] <name> = "<module>:app.main"`, then
  `uv sync`), `treaty audit <module>:app --strict`, and `treaty conformance <module>:app
  --run`. Without `--out`, or with one outside `--cwd`, `<module>` stays a placeholder.
  `--format plain` with `--out` prints these steps under `Next`, replacing today's two
  lines; without `--out` it still prints the module alone

### `treaty audit` (`src/treaty/_audit.py`)

- `danger-level` rule: a mutating command whose leading word is a read verb (`get`,
  `list`, `ls`, `show`, `status`, `describe`, `info`, `inspect`, `view`, `check`,
  `search`, `find`, `diff`) gets an advice finding: its name suggests a read, a command
  that changes nothing is `danger_level="safe"`, which agents may run without a key and
  the kit probes. The fix is `danger_level="safe"`, and says to drop the command's
  `exit_codes` if they were added only for the exit-codes rule. Being advice, it does not
  fail `--strict`
- `danger-level` rule: the fix for a safe command with a destructive name names both
  changes registration needs, the `dry_run` flag and a frozen dataclass output with
  `would_affect: treaty.Affects | None = None`
- `profile` rule: the fix for a missing profile is `treaty conformance <target> --run`,
  with the target the audit was given, which writes the profile and runs the kit
- `profile` rule: when the environment's console script named after the app
  (`importlib.metadata` entry points, group `console_scripts`) loads from a module other
  than the target's, or there is none and no `conformance/<name>` launcher, an advice
  finding says the kit would run the old CLI or nothing, with the fix
  `[project.scripts] <name> = "<module>:<attribute>.main"` then `uv sync`
- No fix the audit gives a module at any step of this path contains a requirement id
  (`REQ-`), `conformance/run.py`, or a pointer to the spec; a message may still cite a
  requirement id as a reference

### The path, end to end

Scaffold with `--out`, then apply the audit's next steps until `treaty audit <target>
--strict` exits 0, taking the `danger-level` advice and, for each command the kit should
run, the `describe` advice's example; then `treaty conformance <target> --run`. The
end state is the bar CI holds a `treaty init` scaffold to. romamo/treaty#473 adds the test
that walks this path for the click, typer, and argparse fixtures in
`tests/fixture_scaffold_*.py` (`tests/test_scaffold_from.py` or a new test beside
`tests/test_init_conformance.py`), in `.github/workflows/ci.yml` on every pull request.

## Acceptance criteria

- S-001-1: `treaty scaffold-from click fixture_scaffold_click:cli --out <file>` writes
  `release rollback` with `danger_level="destructive"`, one `dry_run` field (the old
  `--dry-run`), and an output dataclass with `would_affect`; the module registers and
  passes ruff and `mypy --strict`
- S-001-2: scaffolding the typer and argparse fixtures writes `transaction add` and
  `restart`, whose old CLIs had `--yes`, with `danger_level="destructive"` and an added
  `dry_run` field, and their `skipped` entries' reasons say the command was declared
  destructive
- S-001-3: no scaffolded module of the three fixtures contains `danger_level="safe"`, and
  every command keeps `exit_codes=()`
- S-001-4: `scaffold-from` with `--out src/deploy/cli_treaty.py` returns
  `data.next_steps` naming `deploy.cli_treaty:app` in the console-script line, `treaty
  audit deploy.cli_treaty:app --strict`, and `treaty conformance deploy.cli_treaty:app
  --run`, in that order; `--format plain` prints them; without `--out`, `--format plain`
  prints only the module
- S-001-5: `treaty audit` of each scaffolded fixture reports an advice finding of rule
  `danger-level` with fix `danger_level="safe"` for `status` (click), `check` and
  `transaction.list` (typer), and `inventory.show` (argparse), and none for the other
  commands; `--strict` exits 0 once only advice remains
- S-001-6: the `danger-level` fix for a safe command named `delete` names `dry_run` and
  `would_affect`, and a command edited as it says registers
- S-001-7: with no `conformance/*.json` under `--cwd`, the `profile` finding's fix is
  `treaty conformance <target> --run` with the audit's target, and no fix in the report
  contains `conformance/run.py`
- S-001-8: when the console script named after the app loads from another module, the
  audit reports a `profile` advice finding whose fix is the `[project.scripts]` line for
  the target; when it loads from the target's module, there is no such finding
- S-001-9: at every step of the walk on the three fixtures, no fix in `treaty audit --all`
  contains `REQ-` or `conformance/run.py`
- S-001-10: at the end of the walk on each fixture, `treaty audit <target> --strict` exits
  0, and `treaty conformance <target> --run` exits 0 with `levels.level_1` and
  `levels.level_2` `pass` and no check `fail`; its profile has a `read` probe for each
  command the walk made safe and a `destructive` probe for each destructive command

## Out of scope

- The end-to-end test in CI, which romamo/treaty#473 adds against these criteria
- Inferring `danger_level="safe"` in scaffold-from: the audit suggests it and the author
  decides
- Writing a console script, a `conformance/<name>` launcher, or a project around the
  module: scaffold-from keeps writing one module, and its next steps say what to change
- Replacing the handler stubs with calls into the old code, typed outputs for mutating
  commands, and the remaining advice (`typed-output`, `raw-payload`, `stable-order`,
  `explicit-timeout`, `builtin-shadowed`), which the kit does not need
- Level 3 of the kit: it may stay `incomplete` where the app has no stream to probe, as
  for `examples/deployctl.py`
- A real consumer port, such as the one the plan's notes suggest as evidence; the
  fixtures stand in for it here

## Decisions relied on

- D-1: scaffold-from keeps renaming an old `--format` or `--output` off treaty's flags
- D-4: the walk's test runs on Ubuntu on pull requests and on the full matrix on main

## Issues

## Verification
