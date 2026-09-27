# 12: Output selection and streaming flags

Size: L

Lets an agent ask for less: some fields, only the ids, a token-sized window, or a stream
instead of a buffer, plus stderr progress it can read while it waits. Phase B because
every feature is additive; the one exception is the flag names, which become reserved
global options and must be claimed before the freeze.

| Requirement | Priority | Now | Gap | API change |
|-------------|----------|-----|-----|------------|
| O-002 `--fields` | P2 | Not started | No projection | Additive, but reserves the global `--fields` |
| O-004 `--stream` | P2 | Partial | Streaming commands stream by default with `--no-stream`; no `--stream`, no warning for other commands, no `pagination` on the last line | Additive, but reserves the global `--stream` |
| O-005 `--format id` | P3 | Not started | No `Format.ID`, no primary id declaration | Additive: `Format.ID`, `id_field=` keyword |
| O-012 `--heartbeat-interval` | P2 | Not started | `--heartbeat-ms` writes JSON to stdout; nothing writes progress to stderr, no `ctx.progress()` | Additive: flag on `heartbeat=True` commands, `ctx.progress()` |
| O-049 Token budget flags | P2 | Not started | No token flags (ROADMAP 0.3.0 calls them `--max-tokens`) | Additive, but reserves four global names; new `meta` keys; `app.tokenizer()` |
| O-050 `exec` | P2 | Partial | Row is stale: `exec --format jsonl` works and is tested (`test_o001_jsonl_is_one_object_per_line_everywhere`) | No |

## API impact

- **Reserved global options** (Phase A task, before 1.0): `fields`, `stream`,
  `token-limit`, `token-count`, `token-offset`, `tokenizer` join `GLOBAL_FLAGS` and
  `split_globals`. An app field with one of these names then fails registration with the
  REQ-F-079 collision error, so reserving them after 1.0 would be a breaking change. Until
  a feature lands, passing its flag is an argument error (exit 2) that names it as reserved
- **`App.command(id_field: str | None = None)`**: names the primary id; inferred as `id` when
  the output type (or its list item type) has that field. Registration checks that the
  field exists and is a `str`, `int`, `UUID`, or registered scalar
- **`Format.ID`** member; `id` appears in the manifest's `output_formats` for commands that
  have an id field
- **`ctx.progress(message: str) -> None`**; `--heartbeat-interval SECONDS` on `heartbeat=True`
  commands, next to `--heartbeat-ms`. O-012 is not an alias of O-038: one is JSON on stdout,
  the other plain text on stderr, and both can run at once
- **`app.tokenizer(name: str, *, count: Callable[[str], int])`**, like `app.format()`; a
  built-in `approx` tokenizer; an optional `treaty[tiktoken]` extra registers `cl100k_base`
  and `o200k_base`. Core stays dependency-free
- **Envelope `meta`**: `token_limit`, `token_offset`, `next_token_offset`, `token_count`,
  `tokenizer`; `truncated` is shared with the byte cap; the last stream line gains
  `pagination`. Warning code `STREAMING_NOT_SUPPORTED`

## Design

### `--fields` (O-002)

Valued global, parsed in `split_globals` into `GlobalOptions.fields: tuple[str, ...]`
(comma-split, empty names refused with exit 2). `_Run._present` (shared with 07) keeps only
the named top-level keys of object `data`, and of each object item of list `data`, after
masking and trust tags and before the token budget and byte cap. Unknown names are ignored;
`_source` and `_trusted` are always kept; `meta`, `error`, `warnings` are untouched. Applies
to each stream event and each exec line; an exec line may set it in `_opts`. The manifest
description says the projected `data` no longer satisfies `output_schema`'s `required`.

### `--stream` (O-004)

- Global switch. On a `streaming=True` command it changes nothing (those stream by default,
  the Level 2 choice recorded in HANDOFF); with `--no-stream` it exits 2 as a contradiction
- On any other command the run is buffered as usual and gains warning
  `STREAMING_NOT_SUPPORTED` naming the command; exit 0 (the criterion; the spec's wire
  example shows an exit-2 error, see 12-D1)
- `_Run.stream` adds `pagination` to the terminal envelope: `total` and `returned` are
  `seq`, `truncated` and `has_more` false, `next_cursor` null; a partial stream keeps its
  `partial: true`
- `--format jsonl` already equals `json` for every command; no change

### `--format id` (O-005)

- `_route` refuses `--format id` on a command without an id field: exit 2, message names
  `id_field=`. `App.formats` offers `id` when any command has one
- `_Run._emit_id` writes `str(value) + "\n"` for object data, one line per item for list
  data and per event for streams; nothing else reaches stdout. An id containing whitespace
  or a control character ends the run with `INVALID_OUTPUT` (exit 1), since it cannot be
  piped. Errors go to stderr as in plain mode
- A page with `has_more` writes `next: --cursor <token>` on stderr, so the ids stay pipeable
- Audit rule `id-field` (advice): the output type has no `id` but exactly one field ending in
  `_id`, `uuid`, or `slug`; fix `id_field="<name>"`

### `--heartbeat-interval` (O-012)

- `FrameworkFlag` row in `_framework.py`, argv only, `SECONDS` in (0, 86400]; off unless given
- `ctx.progress()` stores the latest status on the run, redacted by `_Run._redactor` and
  cleaned of escapes; `_check_ctx_calls` refuses it in a command without `heartbeat=True`
  (fix: `heartbeat=True`), like `ctx.prompt` without `interactive=True`
- `call_with_timeout` takes `heartbeats: Sequence[Heartbeat]`, each with its own next-due
  time, so `--heartbeat-ms` and `--heartbeat-interval` tick together on the waiting thread.
  The line is `[<elapsed>s] <status>`, or `running` before the first `progress()` call
- `--quiet` suppression is not applicable until O-008 adds `--quiet`; the ticker reads a
  `quiet` flag of the run that O-008 sets

### Token budget (O-049)

- Spec names win over ROADMAP's `--max-tokens`: `--token-limit N`, `--token-offset N`,
  `--token-count`, `--tokenizer NAME`, all global and argv only
- Measured over `data` rendered in the chosen `--format` (json text, or the renderer's text),
  not the envelope, so `meta` can report the count
- `_cap.py` gains a `Budget(limit, measure)` so `cap_envelope`'s cut search works in tokens
  as well as bytes. Cuts fall on item and field boundaries, never inside a token
- Windows are whole items of the data array (or of the largest list in object data): the
  window starts at the first item ending after `--token-offset`; `meta.token_offset` is that
  item's start, `meta.next_token_offset` where the next window starts
- `--token-count`: the command runs as it would without the flag (a mutating command still
  applies; the flag changes output, not effects), then `data: null` and `meta.token_count`,
  written as a JSON envelope whatever `--format` says, like `--schema`

### `exec` (O-050)

All criteria are met today: in-process dispatch, `_cmd`/`_line` in `meta`,
`--ignore-errors`, `--dry-run` forwarding by danger level, `DISPATCH_PARSE_ERROR` with
`phase: validation`, exit 2 for a stream with no parseable line, and `--format jsonl`.
The work is one test per criterion and moving the row to Done.

## Decisions

| ID | Question | Recommendation |
|----|----------|----------------|
| 12-D1 | `--stream` on a non-streaming command: warning (criterion) or exit-2 `STREAMING_NOT_SUPPORTED` (wire example) | Warning and a buffered envelope; the criterion is the bar |
| 12-D2 | O-049 wants a sentinel appended to `data`; a `"[TRUNCATED]"` item breaks list item schemas | No sentinel in `data`: `meta.truncated`, `next_token_offset`, and `FIELD_TRUNCATED` warnings, as the byte cap does; record the deviation |
| 12-D3 | `--stream` on paginated list commands (items as events, `pagination` last) | Not for 1.0; the reserved name allows it later without a break |
| 12-D4 | Built-in tokenizer: `ceil(utf-8 bytes / 4)` or a word-and-punctuation split | Bytes over 4: stable, cheap, and within about 20% of `cl100k_base` on JSON |

## Tasks

- [x] Phase A: reserve the six global names in `GLOBAL_FLAGS` and `split_globals`; each exits 2 as reserved (landed earlier; all six are now in `IMPLEMENTED`)
- [x] Root manifest `flags` entries and help rows for every new global
- [x] `--fields` projection in `_Run._present`; exec `_opts.fields` (also an MCP argument). Applies to built-ins too, since the criterion says every command
- [x] `--stream`: warning, contradiction with `--no-stream`, `pagination` on the terminal envelope (only on a clean end; `--no-stream` drops it)
- [x] `id_field=`, `Format.ID`, manifest `output_formats`; audit rule `id-field`. No `_emit_id`: `id_lines` is the `id` renderer, and `_present` checks the ids first (`INVALID_OUTPUT`). Inference and the registration check read the output schema, so a `dict` output needs `id_field=` and is checked as it answers. `output_formats` lists only `id`
- [x] `--heartbeat-interval`, `heartbeats` in `call_with_timeout`. `ctx.progress()` already existed (11) and stays callable anywhere, so no `_check_ctx_calls` rule; it records the status for the heartbeat line
- [x] `app.tokenizer()` and `approx`; `--token-limit`, `--token-offset`, `--token-count`, `--tokenizer` (in `_select.py`). No `Budget` in `_cap.py`: `_cap.shrink` is the byte cap's cut search with a pluggable `fits`, which the token budget calls. The budget measures `data` as compact JSON in every `--format`, not the renderer's text; `--token-count` with `--token-limit` or `--token-offset` exits 2. `app.tokenizer(default=True)` sets the default
- [x] `treaty[tiktoken]` extra; `cl100k_base` and `o200k_base` resolve lazily when `--tokenizer` names them, and exit 2 `TOKENIZER_UNAVAILABLE` without the extra
- [x] Tests named after each acceptance criterion of O-002, O-004, O-005, O-012, O-049, O-050 (`tests/test_output_selection.py`)
- [x] COMPLIANCE rows (all six Done: O-008 landed with 11, so O-012 is Done too); README, HANDOFF, ROADMAP (the `--max-tokens` line)
