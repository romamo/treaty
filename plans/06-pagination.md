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

- [ ] `Page`, `PageRequest`, `ctx.page`; `paginated=`; flags; manifest fields
- [ ] Cursor wrapping and `INVALID_CURSOR`
- [ ] Framework slicing and `PAGINATION_UNSUPPORTED`
- [ ] Runnable `truncation_hint`; `<APP>_MAX_OUTPUT_BYTES`
- [ ] Terminal envelope pagination for streams; MCP input schema gains `limit` and `cursor`
- [ ] Audit rule `paginated-list`; README section "Lists"

## Tests

- 50 items, no flags: 20 returned, `truncated: true`, `next_cursor` set
- Following `next_cursor` three times returns 20, 20, 10 and then `next_cursor: null`
- `--limit 0` returns 50 with `truncated: false`; `--limit 100` returns 50
- A tampered cursor exits 2 with `INVALID_CURSOR`
- `--schema` shows `default_limit: 20`
- A 10,000-item response cut by the byte cap has a `truncation_hint` that, run as given,
  exits 0
