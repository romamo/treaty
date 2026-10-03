# Decisions

Rules the maintainers settled, numbered in order. Triage designs and code reviews check
every change against the entries whose "Applies to" it touches. Change a rule by adding an
entry that supersedes it, never by editing an old one.

## D-1: --format is the representation flag

- Decided: 2026-09-25, in romamo/treaty#317
- Rule: Output representation is chosen with --format (and TREATY_FORMAT); no global --output or alias selects it, since --output PATH names the file a command writes its data to
- Why: --output usually names a destination, and reserving it globally for representation would take that name from every command built on treaty, even though the CLI Agent Spec's checks use --output json
- Applies to: src/treaty/_app.py, src/treaty/_flags.py, global options, CLI flags
- Enforced by: review

## D-2: Broad catches only where user code runs

- Decided: 2026-09-25, in romamo/treaty#317
- Rule: except Exception or BaseException appears only around user code (the handler, __post_init__, a scalar's parse= or serialize=, renderers, hooks) or to re-raise on another thread, each marked '# noqa: BLE001 - <reason>'; a handler crash becomes a HANDLER_CRASHED envelope with exit 1, and framework failures raise specific types
- Why: The README promises a response envelope on every exit, so a handler bug can't crash the process; everywhere else the project fails fast
- Applies to: src/treaty/*.py, error handling
- Enforced by: review (ruff's BLE rules aren't enabled)

## D-3: Python 3.14 is the floor

- Decided: 2026-10-01, in romamo/treaty#103
- Rule: requires-python stays >=3.14; a library that supports older Pythons ships its treaty CLI as an optional extra, as the docs from #114 describe
- Why: Lowering the floor to 3.12 was asked for and refused; the documented extra covers libraries with older floors
- Applies to: pyproject.toml, Python versions
- Enforced by: review

## D-4: Pull requests run CI on Ubuntu only

- Decided: 2026-10-01, in romamo/treaty#167
- Rule: A pull request runs lint, tests, and the scaffold on Ubuntu only; macOS, Windows, and free-threaded 3.14t run on main, on release commits, and on a PR labelled full-ci, which a PR touching fd-1 interception, signals, file locking, paths, or Windows retries carries
- Why: Serial landing re-runs PR CI on every rebase, and macOS was the long pole; the release bot's full matrix still gates every release
- Applies to: .github/workflows/*, CI
- Enforced by: .github/workflows/ci.yml
