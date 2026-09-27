# 04: Argument grammar and command surface

Size: L

Fixes what the parser accepts and how a command may change once 1.0 ships. Each item
either adds a keyword that becomes permanent at 1.0 or reserves a name (`--validate-only`,
`-` as a value), so all of it lands before the freeze.

| Requirement | Priority | Now | Gap | API change |
|-------------|----------|-----|-----|------------|
| C-026 Conditional argument dependencies | P1 | Not started | Cross-field rules live only in args `__post_init__`, invisible to `--schema` | Yes: `requires=` keyword, three exported rule types, `requires` in manifest |
| C-027 Option placement | P1 | Not started | No `option_placement`; tokens after `--` fill positionals or fail, never forwarded | Yes: `option_placement=` keyword, field on every manifest entry |
| F-067 Interspersed option parsing | P1 | Partial | Interspersed and repeat checks done; no strict exception | No: closes with C-027 |
| C-020 Resource ID validation pattern | P1 | Partial | Presets need a registered scalar; no warning for unpatterned ID fields | Yes: `pattern_type=` on `Flag` and `Arg` |
| F-049 Async handler enforcement | P1 | Partial | `async def` registers and fails at run time | Yes: registration refuses `async def` |
| F-059 JSON5 input normalization | P1 | Not started | `--raw-payload` is strict JSON | Yes: wider accepted input, new `INVALID_JSON` error code |
| O-006 `-` as stdin ID source | P3 | Not started | `-` is an ordinary value | Yes: `from_stdin=` on `Flag` and `Arg` |
| O-009 `--validate-only` | P1 | Not started | No phase-1-only run | Yes: new framework flag on every command |
| F-075 Subcommand additive stability | P1 | Not started | No lifecycle metadata or removal check | Yes: `introduced_in=`, `deprecated=`, `treaty audit --baseline` |

## API impact

Additive, safe for existing apps:

- `App.command` gains `requires=`, `option_placement="any" | "strict"`, `introduced_in=`,
  and `deprecated=`; `Flag` and `Arg` gain `pattern_type=` and `from_stdin=`; `Flag`
  gains `deprecated=`
- New exports: `RequiredWhen`, `Excludes`, `DefaultWhenAbsent`, `Deprecated`
- Manifest: `option_placement` on every `CommandEntry` (changes every etag once),
  `requires` where declared; `--schema` adds `introduced_in`,
  `deprecated_in`, `replacement`, `removed_in`; `meta.validation_only` on envelopes

Breaking, and why it is acceptable before 1.0:

- `--validate-only` is reserved on every command, so a field named `validate_only` now
  fails registration through `framework_collisions`, and `validate_only` becomes a
  framework key in `exec`, `App.call`, and MCP payloads
- `async def` handlers raise `RegistrationError`; they never worked, so only the failure
  moves earlier
- A `--raw-payload` or `exec` line that fails to parse reports `error.code: INVALID_JSON`
  instead of `ARG_ERROR` (exit stays `2`)

## Design

### Conditional rules (C-026)

Three frozen dataclasses, one per `ConditionalRule` shape: `RequiredWhen(flag, value,
then)`, `Excludes(flag, prohibited)`, and `DefaultWhenAbsent(flag, target, default)`,
with `then` and `prohibited` as `tuple[str, ...]`.

- `build_command` resolves every name through `Command.field_by_flag`, runs `value` and
  `default` through `FieldInfo.parse` so a typo or a wrong-typed value is a
  `RegistrationError`, and requires `then` and `target` fields to be optional
- A new `_rules.py` evaluates them in `_finish` before `args_type(**values)`, on the set of
  fields the caller supplied (not defaulted), so `__post_init__` sees the rule's default.
  Violations join the `_Collector` and exit 2 in one run, with `context.rule`
- `build_from_mapping` shares the evaluator, so `exec`, `App.call`, and MCP enforce the
  same rules; `command_entry` emits `requires` in declaration order
- Audit rule `conditional-rules` (advice): an args `__post_init__` whose source compares
  one field and raises on another, with the equivalent `requires=[...]` as the fix

### Option placement and forwarding (C-027, F-067)

- `option_placement="strict"` requires the last positional to be a variadic
  `tuple[str, ...]`; registration says so otherwise
- `_route` resolves the path first; for a strict command, `split_globals` and
  `parse_command_args` stop at the first positional after the path (or at a leading
  `--`), and every later token goes verbatim to the variadic field, a second `--` included
- Under `any`, tokens after `--` fill positionals as today; `exec` and MCP pass JSON, so
  placement does not apply there
- `command_entry` writes `option_placement` for every command, `"any"` included
- Audit rule `option-placement`: a handler that passes a variadic positional into
  `ctx.run` or `ctx.pipeline` without `option_placement="strict"`

### ID patterns (C-020)

`Flag(pattern_type="alphanumeric_id")` applies `PRESET_PATTERNS` from `_scalars.py`
without a registered scalar; `pattern` and `pattern_type` stay mutually exclusive, and
`filepath` stays on `Path` fields. Audit rule `id-pattern` (warning) flags `str` fields
named `id`, `*_id`, `slug`, `*_slug`, `ref`, or `*_ref` with no pattern, preset, enum, or
scalar, and its fix adds `pattern_type="alphanumeric_id"`. The error criterion already
holds: `check_pattern` names the flag and the pattern.

### Async handlers (F-049)

`_inspect_handler` rejects `inspect.iscoroutinefunction` and `isasyncgenfunction`; the
same check covers `cleanup=`, `cursor_check=`, and resource `acquire`. `_invoke` guards
what escapes it (a decorated coroutine): an awaitable result is closed and reported as
`HANDLER_CRASHED` naming the handler. Not applicable: "sync handler produces a warning",
since treaty's handlers are sync by design and the parse/parseAsync race has no analogue
with one dispatch path; "unawaited async operation", since treaty starts no event loop.
Teardown ordering already holds: `cleanup=` runs before the envelope is written.

### JSON5 normalization (F-059)

A dependency-free `_json5.py` rewrites trailing commas, `//` and `/* */` comments, and
unquoted keys to strict JSON, then `loads_strict` parses it. `_decode_raw_payload` and
`parse_dispatch_line` use it. When normalization fails, a bounded repair pass (quote bare
words, insert a missing `:` or `,`, close open brackets) tries again; if that parses, the
error carries `context.corrected_input` and the command still exits 2. Treaty has no
`type: json` flag, so `--raw-payload` and `exec` lines are the whole surface.

### `-` from stdin (O-006)

`from_stdin=True` on a `str`, scalar, or array field makes the literal `-` read stdin
through the existing capped reader (`StdinCap`). A trailing newline is stripped; an array
takes one item per non-empty line; a scalar given two lines exits 2. Empty stdin exits 2
with `EMPTY_STDIN`, a terminal on stdin exits 2 without reading. At most one such field
per command, never with `stdin_input=True` or on `exec`. `FlagEntry` allows no extra key,
so the manifest description gains "(- reads it from stdin)", as `multiline` does.

### `--validate-only` (O-009)

A `_switch` row in `_framework.FLAGS` for every command. `_route` runs parsing, rules,
`__post_init__`, and `cursor_check`, then returns `data: null`,
`meta.validation_only: true`, exit 0, before `run.execute`; the credential gate, the
idempotency store, and the handler never run. Failures exit 2 (the criterion; the
schema's "exit 3" contradicts it). `--help` lists it through `FLAGS`.

### Lifecycle and deprecation (F-075)

- `introduced_in="1.2.0"` and `deprecated=Deprecated(since, replacement, removed_in)`
  on `App.command`; a deprecated run writes the spec's `DEPRECATED` JSON line to stderr
  and adds a `DEPRECATED` warning to the envelope. `Flag(deprecated=...)` does the same
  with `DEPRECATED_FLAG`
- After removal, the old name is kept with `App.redirect(old, to=...)` from
  [03](03-error-contract.md): exit 13 with `error.redirect`, and the old name in the
  target's manifest `aliases`. `Deprecated.replacement` is checked against it
- "Removal without deprecation fails at startup": `treaty audit --baseline manifest.json`
  compares against the last released manifest and fails on a command, flag, or exit code
  that vanished without a `redirect()` or `deprecated=` record (see 04-D3)

## Decisions

| ID | Question | Recommendation |
|----|----------|----------------|
| 04-D1 | C-020 asks for a "registration warning"; treaty has no warning channel at registration | The `id-pattern` audit rule is that warning; a Python warning on every CLI start is noise |
| 04-D2 | F-075 puts `introduced_in` and `deprecated_in` in the manifest, but `CommandEntry` has `additionalProperties: false` without them | Emit them in `--schema` now and propose the fields upstream in `manifest-response.json` |
| 04-D3 | Detect removals at startup or against a baseline | Baseline in `treaty audit`: startup has no previous surface to compare with |
| 04-D4 | Accept `async def` by running it under `asyncio.run` | Reject for 1.0; accepting later is additive, withdrawing is not |

## Tasks

- [x] `pattern_type=` on `Flag` and `Arg`; audit rule `id-pattern`
- [x] Refuse async handlers, hooks, and acquires; awaitable-result guard in `_invoke`
- [x] `_json5.py` with repair pass; `INVALID_JSON` and `corrected_input`. The repair is
  the same one-pass parser run leniently (bare words, missing `:` or `,`, unclosed
  brackets); `corrected_input` is a top-level `ErrorDetail` field and also in `context`.
  An `exec` line that is not JSON is `INVALID_JSON`; a line that is JSON but not a
  request stays `DISPATCH_PARSE_ERROR`
- [x] `RequiredWhen`, `Excludes`, `DefaultWhenAbsent`; `_rules.py`; manifest `requires`;
  audit rule. Rules name flags by their `--` spelling and refuse array flags; a violation
  stops phase 1 before `__post_init__`, as a missing field does. "Present" is a boolean
  that is true, or any other value that is not null
- [x] `option_placement=`; strict split in `_route`; forwarding; audit rule. Simpler than
  planned: `_parse.strict_argv` inserts `--` before the first positional of a strict
  command, and the existing `--` handling does the rest
- [x] `from_stdin=` and `EMPTY_STDIN`. Allowed on any value field but booleans and
  secrets; a terminal on stdin is `STDIN_IS_TTY`, exit 2
- [x] `--validate-only` and `meta.validation_only`. No conformance profile probe: the
  profile schema has no probe kind for it. `meta.validation_only` is on the success
  envelope only
- [x] `introduced_in=`, `Deprecated`, `Flag(deprecated=)`; `treaty audit --baseline`
  (rule `additive`, listed by `treaty rules`). A removal passes when the baseline marked
  it deprecated (the manifest description of a deprecated command or flag ends in
  "(deprecated since X; use Y)", since `CommandEntry` has no key for it), when the old
  command path redirects, or when the major version went up. `Deprecated.replacement`
  must be a registered command, checked with `fix_commands` in `App.check_fixes`
- [x] Update COMPLIANCE rows, README, HANDOFF, ROADMAP (drop the 0.3.0 bullets done here).
  F-075 stays Partial for the manifest keys and the startup check (X6, 04-D2, 04-D3)
