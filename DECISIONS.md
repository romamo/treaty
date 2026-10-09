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
- Superseded by: D-11

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

## D-10: Streams on stdout use the spec's line shape

- Decided: 2026-10-07, in romamo/treaty#389
- Rule: A stream on stdout writes bare item lines (_seq when numbered), then exactly one terminal line: a "_summary": true line with the ResponseMeta fields, or an error envelope on failure; cancellation ends CANCELLED with data {"partial": true} and error.context.signal; exec lines and App.call keep one envelope per event
- Why: REQ-O-004 and REQ-F-069 define stream lines this way, and a spec-reading agent stopped at treaty's first envelope line; changing the wire format after 1.0 would need a major version
- Applies to: src/treaty/_app.py, streams, jsonl, stdin_records
- Enforced by: review; the spec kit's stream_contract and stream_sigint checks

## D-11: ordered=True leaves set arrays sorted

- Decided: 2026-10-07, in romamo/treaty#387
- Rule: ordered=True keeps handler order for every array in a command's output except one whose field declares its own sort (Out(sort_key=) or x-sort-key) or one with uniqueItems: true (a set or frozenset), which keeps its canonical sort and gets no x-ordered; an explicit per-property x-ordered: true on a set still wins
- Why: A set has no order of its own, so following handler order made output follow the hash seed
- Applies to: src/treaty/_out.py, src/treaty/_app.py, ordered, output arrays, sets
- Enforced by: review
- Supersedes: D-6

## D-12: A manifest states every key whose absence means something at its schema version

- Decided: 2026-10-08, in romamo/treaty#414
- Rule: At ManifestResponse 3.19 a command whose stdout is a protocol stream (mcp serve) says stdout: "protocol" and its protocol, with no output_schema or output formats, and every command no MCP server offers says mcp: false; the manifest and the MCP tool list share one predicate for the latter
- Why: From 3.16 a missing stdout means stdout carries envelopes and from 3.17 a missing mcp means the server may offer the command, so leaving them out would misdescribe mcp serve and mcp=False commands to a spec-reading agent
- Applies to: src/treaty/_manifest.py, src/treaty/_tools.py, src/treaty/_mcp_serve.py, manifest schema_version, mcp serve
- Enforced by: review; tests/test_manifest_protocol_mcp.py against the spec's schema

## D-13: mcp serve follows REQ-C-032 on shutdown

- Decided: 2026-10-08, in romamo/treaty#415
- Rule: mcp serve exits 0 with no envelope when stdin closes (or the client closes stdout); SIGINT exits 130 and SIGTERM 143, each with the envelope on the last line of stderr; a failure before serving keeps its envelope; the stopped_by and tool_calls result is no longer reported
- Why: The manifest says stdout: "protocol" for mcp serve (D-12), and REQ-C-032 defines what such a command does on a clean and a signalled shutdown; this reverses the exit 0 on a signal from #313
- Applies to: src/treaty/_mcp_serve.py, src/treaty/_app.py, mcp serve, signals, exit codes
- Enforced by: review; subprocess tests for stdin close and each signal

## D-14: A person's confirmation has no flag

- Decided: 2026-10-08, in romamo/treaty#424
- Rule: ctx.attest, allowed by requires_person=True, is the one prompt no flag answers, --yes included; off a terminal it exits 4 with PERSON_REQUIRED and a suggestion naming no flag. requires_person implies interactive=True and mcp=False with no opt-in to serving it; the manifest says so in the description and mcp: false, and only --schema carries requires_person: true
- Why: Any answer an agent can pass as a flag lets it approve its own proposal (cloudfall-dev/cloudfall#25), a departure from REQ-C-005's --yes that a person-only step needs; an MCP server's stdin is its protocol, never a terminal, so a served command could only fail; ManifestResponse's CommandEntry rejects keys it does not define. It is a speed bump and a record, not a boundary: the boundary is a separate OS user
- Applies to: src/treaty/_prompt.py, src/treaty/_context.py, src/treaty/_command.py, src/treaty/_manifest.py, ctx.attest, requires_person, --yes, mcp
- Enforced by: review; tests/test_attest.py
- Superseded by: D-16

## D-15: A person-only command is not resumable

- Decided: 2026-10-09, in romamo/treaty#426
- Rule: requires_person=True with resumable=True is a RegistrationError, as requires_person on a passthrough command already is; a person-only command restarts from its confirmation
- Why: --resume-from is a flag, and resuming past the step that calls ctx.attest answers it without a person, which D-14 forbids; refusing the pair is small and fails closed, while guarding the resume needs the asking step recorded at run time
- Applies to: src/treaty/_command.py, requires_person, resumable, --resume-from, ctx.attest
- Enforced by: review; tests/test_attest.py

## D-16: A person's confirmation has no flag, and the manifest entry says so

- Decided: 2026-10-09, in romamo/treaty#431
- Rule: ctx.attest, allowed by requires_person=True, is the one prompt no flag answers, --yes included; off a terminal it exits 4 with PERSON_REQUIRED and a suggestion naming no flag. requires_person implies interactive=True and mcp=False with no opt-in to serving it; the manifest entry says requires_person: true (ManifestResponse 3.21, REQ-C-036) beside interactive: true and mcp: false, and a manifest listing such a command says schema_version 3.21 while one without stays at its earlier version; the description keeps its marker, and --schema carries requires_person: true too
- Why: cli-agent-spec v1.16.0 adopted treaty's design as REQ-C-036 and gave CommandEntry the key D-14 lacked, so an agent reading the manifest learns it from a key rather than description text; the rest of D-14 stands: any answer an agent can pass as a flag lets it approve its own proposal, and an MCP server's stdin is never a terminal. It is a speed bump and a record, not a boundary: the boundary is a separate OS user
- Applies to: src/treaty/_prompt.py, src/treaty/_context.py, src/treaty/_command.py, src/treaty/_manifest.py, ctx.attest, requires_person, --yes, mcp, manifest schema_version
- Enforced by: review; tests/test_attest.py against the spec's schema
- Supersedes: D-14
