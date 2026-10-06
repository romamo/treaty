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

## D-5: Output unions must be tagged

- Decided: 2026-10-04, in romamo/treaty#328
- Rule: A union of output types, anywhere in a command's output (the return type, list items, dict values, or fields), is accepted only when every member can be told apart, by a Literal tag field or by required keys no other member has; it is published as oneOf, and an untagged union is refused at registration with a message on adding a tag
- Why: An agent reading a response must know which shape it got without trying each schema; X | None keeps its anyOf with null
- Applies to: src/treaty/_types.py, src/treaty/_schema.py, src/treaty/_out.py, output types
- Enforced by: review
- Superseded by: D-7

## D-6: ordered=True covers every array in a command's output

- Decided: 2026-10-04, in romamo/treaty#329
- Rule: ordered=True on a command keeps handler order for every array in its output whatever the return type, except an array whose field declares its own sort (Out(sort_key=) or x-sort-key), which the more specific declaration sorts; per-property x-ordered is the finer-grained route, and there is no adapter-level ordering option
- Why: A typed return must not be less expressive than a dict return, and one command-level switch avoids a second API for the same thing
- Applies to: src/treaty/_out.py, src/treaty/_app.py, ordered, output arrays
- Enforced by: review

## D-7: Output unions must be tagged, by a Literal or an enum

- Decided: 2026-10-04, in romamo/treaty#342
- Rule: A union of output types, anywhere in a command's output (the return type, list items, dict values, or fields), is accepted only when every member can be told apart, by a Literal or enum tag field every member requires with no shared value, or by required keys no other member has; it is published as oneOf, and an untagged union is refused at registration with a message on adding a tag
- Why: An agent reading a response must know which shape it got without trying each schema; an enum is a closed set of values like a Literal, and pydantic models often tag with one
- Applies to: src/treaty/_types.py, src/treaty/_schema.py, src/treaty/_out.py, src/treaty/_protect.py, output types
- Enforced by: review
- Supersedes: D-5

## D-8: A renderer's context comes by arity

- Decided: 2026-10-06, in romamo/treaty#357
- Rule: A renderer that takes two parameters is called render(data, RenderContext); one that takes one is called render(data); renderers never receive Ctx
- Why: Existing one-parameter renderers keep working, and a renderer sees only what rendering needs (color, width), not the handler's context
- Applies to: src/treaty/_command.py, src/treaty/_app.py, renderers, FormatRenderer
- Enforced by: review

## D-9: Cleanup cut short by the grace is a warning in the envelope

- Decided: 2026-10-06, in romamo/treaty#379
- Rule: When an async handler or async stream source is still running after its cancellation grace, the run reports it as a CLEANUP_FAILED warning in the envelope (and error.context.cleanup_failed on the error), not only as a stderr note
- Why: An agent reads the JSON envelope, not stderr; it has to learn that cleanup (a connection, a lock, a flush) may not have finished
- Applies to: src/treaty/_aio.py, src/treaty/_app.py, cancellation, timeouts, async handlers
- Enforced by: review
