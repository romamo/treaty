# 06: Pagination

List commands return a bounded page and a cursor for the next one.

| Requirement | Level | Now | Gap |
|-------------|-------|-----|-----|
| F-018 Pagination metadata | 2 | Not started | No `meta.pagination` |
| F-019 Default output limit | 2 | Not started | No item limit; only the 1 MiB byte cap |
| O-003 `--limit` and `--cursor` | 2 | Not started | No flags |
| F-052 Response size cap | 2 | Partial | `truncation_hint` is prose, not a runnable command |

## Design

### Declaration

```python
@app.command("releases.list", description="...", exit_codes=(), danger_level="safe",
             paginated=True)
def releases(args: ListArgs, ctx: Ctx) -> Page[Release]:
    rows, next_cursor = store.page(after=ctx.page.cursor, limit=ctx.page.limit)
    return Page(items=rows, next_cursor=next_cursor, total=store.count())
```

- `paginated=True` adds `--limit` (int, default 20, `0` means all) and `--cursor` (string)
  to the command, and `default_limit: 20` plus `paginated: true` to the manifest and
  `--schema`. `App(default_limit=...)` changes the default for the app
- `ctx.page` is a frozen `PageRequest(limit: int | None, cursor: str | None)`; `None`
  limit means all
- `Page[T]` is a generic frozen dataclass: `items: list[T]`, `next_cursor: str | None`,
  `total: int | None`. `data` is the item list; the rest goes to `meta.pagination`:
  `{"limit": 20, "returned": 20, "truncated": true, "next_cursor": "...", "total": 50}`
- A paginated command must return `Page[T]` (registration check); a non-paginated command
  returning `Page` is a registration error too

### Cursor tokens

The framework wraps whatever the handler gives as `next_cursor` into base64url of
`{"v": 1, "c": <handler cursor>, "cmd": <path>}` and unwraps it before `ctx.page`. A cursor
that does not decode, or names another command, is a phase-1 `ARG_ERROR` with
`INVALID_CURSOR` and a suggestion to restart without `--cursor` (O-003).

### Framework-side slicing

When a handler ignores `ctx.page` and returns more than `limit` items, the framework cuts
the list and sets `truncated: true`, but `next_cursor` stays `null` because it cannot
resume the handler's source; it adds a `PAGINATION_UNSUPPORTED` warning so the author
sees the gap. The `paginated-list` audit rule flags commands whose output is `list[...]`
and whose path ends in `list` or `ls` but lack `paginated=True`.

### Runnable truncation hint (F-052)

`_cap.py` builds `meta.truncation_hint` as the argv of the same invocation plus
`--cursor <next>` when the command is paginated, or with `--max-output <needed>` otherwise,
as a JSON array and as a shell-quoted string (`truncation_command`). Rename the env var
check: the spec's `TOOL_MAX_OUTPUT_BYTES` is `<APP>_MAX_OUTPUT_BYTES`, read alongside the
existing `TREATY_MAX_OUTPUT_BYTES` (REQ-F-073 prefers the tool prefix).

### Streams and exec

Streaming list commands put `pagination` on the terminal envelope (the open item under
"Streaming handlers" in `ROADMAP.md`). In `exec`, `_opts.limit` and `_opts.cursor` work
like the flags.

## Tasks

- [x] `Page`, `PageRequest`, `ctx.page`; `paginated=`; flags; manifest fields
- [x] Cursor wrapping and `INVALID_CURSOR`
- [x] Framework slicing (no `PAGINATION_UNSUPPORTED`; see deviations)
- [x] Runnable `truncation_hint`; `<APP>_MAX_OUTPUT_BYTES`
- [x] MCP input schema gains `limit` and `cursor` (streams are not paginated; see deviations)
- [x] Audit rule `paginated-list`; README section "Lists"

## Tests

- 50 items, no flags: 20 returned, `truncated: true`, `next_cursor` set
- Following `next_cursor` three times returns 20, 20, 10 and then `next_cursor: null`
- `--limit 0` returns 50 with `truncated: false`; `--limit 100` returns 50
- A tampered cursor exits 2 with `INVALID_CURSOR`
- `--schema` shows `default_limit: 20`
- A 10,000-item response cut by the byte cap has a `truncation_hint` that, run as given,
  exits 0

## Deviations as built

- **One cursor shape, no `PAGINATION_UNSUPPORTED`.** The cursor is base64url JSON of
  `{"v": 1, "cmd": <path>, "c": <handler cursor>, "s": <skip>}`, where `skip` counts the
  items of the batch at `c` already delivered. A handler that ignores `ctx.page` and
  returns everything, one that returns a bigger batch than asked, and a page the byte cap
  cut all resume correctly, so `next_cursor` is never null while items remain (F-018
  requires that) and there is no gap to warn about. `ctx.page.limit` is `skip + limit`
- **A plain `list[T]` return is the whole collection**: `total` is its length. A source
  that cannot load everything returns `Page[T]`, whose `total` may be `None`
- **`meta.pagination` has exactly the five spec keys**; the `Pagination` definition in
  `response-envelope.json` has `additionalProperties: false`, so there is no `limit` key.
  `truncated` equals `has_more`
- **No `App(default_limit=...)`**: F-019 asks for a per-command setting, which
  `default_limit=` on the command is. `paginated` and `default_limit` appear only in
  `--schema`, since `CommandEntry` admits no extra keys; the manifest's `--limit` flag entry
  carries the same default
- **Streams are not paginated**: `paginated=True` with `streaming=True` is a registration
  error. No acceptance criterion needs a paginated stream
- **The truncation hint is one shell-quoted string**, not also a JSON array. For a cut list
  page it is the same argv with `--limit <kept> --cursor <token>` (earlier spellings of
  both removed, inserted before any `--`), and `meta.pagination` is rewritten to match;
  otherwise the same argv with `--max-output <total_bytes>`. `exec` lines and MCP calls
  have no argv to repeat, so their hint stays prose (with the limit and cursor for a page)
- **`<APP>_MAX_OUTPUT_BYTES` wins over `TREATY_MAX_OUTPUT_BYTES`**; both stay
- **`ParseError(code=...)`** sets `error.code` of a single argument error, and each
  `error.errors` entry carries its `code`; `INVALID_CURSOR` is the first user
- The `treaty rules` built-in is paginated, so the treaty CLI passes its own audit
