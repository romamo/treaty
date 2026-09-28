# Page long lists

**Goal:** a command that returns a list answers one page at a time, in a stable order, with a
cursor that finds the next page even when the list changes in between

**You need:** a treaty app with a list command, such as `todo` at the end of any earlier
chapter; this chapter covers the audit rules `paginated-list` and `stable-order`

**Done when:** the audit has no `paginated-list` or `stable-order` finding, and the list's
cursor survives a change to the list between pages:

<!-- check -->
```bash
uv run treaty audit examples.tutorial.todo_pages:app \
  | jq -e '[.data.next_steps[] | select(.rule == "paginated-list" or .rule == "stable-order")] == []'
```

The chapter starts from
[`examples/tutorial/todo_exit_codes.py`](../../../examples/tutorial/todo_exit_codes.py), whose
`list` is already paginated, and ends at
[`examples/tutorial/todo_pages.py`](../../../examples/tutorial/todo_pages.py), whose `list`
pages by item id.

## Running the checks

Run the **Check** commands from the root of a treaty checkout, in order. `before` runs the
starting file and `todo` this chapter's; the item file, with 25 items, goes to a scratch
directory:

<!-- check -->
```bash
before() { uv run examples/tutorial/todo_exit_codes.py "$@"; }
todo() { uv run examples/tutorial/todo_pages.py "$@"; }
rm -rf tmp/tutorial && mkdir -p tmp/tutorial
jq -n '[range(1; 26) | {id: ., text: "Item \(.)", priority: "normal", done: false}]' \
  > tmp/tutorial/todo.json
```

Each check exits non-zero when it fails: JSON output goes through `jq -e`, which exits 1 when
the condition is false. `tests/test_tutorial.py` runs the checks the same way.

## Why a list is paged

An agent reads a command's whole answer into its context, where every item costs tokens and
crowds out the rest of the task. A list that returns everything works on the day it has ten
items and fails the day it has ten thousand. So treaty pages every list by default: a
command whose handler returns `list[T]` is a list command with no declaration, and gets:

- **`--limit N`**, 20 by default (`default_limit=` changes it per command), `0` for every item
- **`--cursor TEXT`**, to ask for the page after the one a cursor came from
- **`meta.pagination`** on every answer: `returned`, `total`, `has_more`, `truncated`, and
  `next_cursor`, which is `null` on the last page

This is a change for a migrated CLI: `todo list` used to print every item, and now prints
20. A caller that wants everything says so with `--limit 0`, and a person at a terminal
sees only the first page, since a renderer receives `data` alone and has no place to say
more exist.

**Check:** 25 items come back as 20, then the other 5; `--limit 0` returns all 25

<!-- check -->
```bash
before list --db tmp/tutorial/todo.json | jq -e '(.data | length) == 20
  and .meta.pagination.total == 25 and .meta.pagination.has_more'
cursor=$(before list --db tmp/tutorial/todo.json | jq -r .meta.pagination.next_cursor)
before list --db tmp/tutorial/todo.json --cursor "$cursor" | jq -e '[.data[].id] == [21, 22, 23, 24, 25]
  and .meta.pagination.has_more == false and .meta.pagination.next_cursor == null'
before list --db tmp/tutorial/todo.json --limit 0 | jq -e '(.data | length) == 25'
```

## Step 1: Keep it paginated, and give it an order

A small list that can never grow, such as the five states a job can be in, may opt out with
`paginated=False`. Anything that grows with use should not, and the audit says so for every
opt-out:

```bash
$ uv run treaty audit myapp.cli:app --format plain
...
  1. (advice) paginated-list [list]: returns a list with paginated=False, so it has no default limit, --limit, --cursor, or meta.pagination (REQ-F-018, REQ-F-019)
     fix: drop paginated=False unless the list is small and bounded by design
```

Pages only make sense in a fixed order, and treaty sorts every list before it pages it.
`sort_key="id"` on the command names the field to sort by; `ordered=True` keeps the
handler's order instead, for a ranking. Without either, treaty sorts by each item's JSON
text, which is stable but rarely what a caller expects, and the `stable-order` rule asks
for a key. `todo`'s `list` sorts by `id`.

## Step 2: Check what a cursor refuses

treaty's cursor is opaque text that encodes the command, a digest of the other arguments,
and where the last page stopped. It is checked before anything runs: a cursor that does not
decode, or that came from a call with other arguments, exits 2 with `INVALID_CURSOR`, so an
agent that mixes up two listings is told instead of handed the wrong page.

**Check:** a made-up cursor and a cursor from a call without `--all` are both refused

<!-- check -->
```bash
before list --db tmp/tutorial/todo.json --cursor abc | jq -e '.meta.exit_code == 2
  and .error.code == "INVALID_CURSOR"'
before list --db tmp/tutorial/todo.json --all --cursor "$cursor" | jq -e '.error.code == "INVALID_CURSOR"
  and .error.message == "--cursor is invalid: it was issued for different arguments."'
```

## Step 3: Page by key when the list can change

When the handler returns the whole list, treaty cuts the pages itself, and its cursor
records a position: "20 items were delivered". That is exact while the list stays the same.
If an item before the cut goes away between two calls, every later item moves up one place,
and the next page starts one item too late:

**Check:** after item 5 is purged between the two pages, the position cursor skips item 21

<!-- check -->
```bash
cursor=$(before list --db tmp/tutorial/todo.json | jq -r .meta.pagination.next_cursor)
before done 5 --db tmp/tutorial/todo.json > /dev/null
before purge --db tmp/tutorial/todo.json --confirm-destructive > /dev/null
before list --db tmp/tutorial/todo.json --cursor "$cursor" | jq -e '[.data[].id] == [22, 23, 24, 25]'
```

A cursor keyed on the data does not move: "after item 20" means the same thing whatever
happened before item 20. A handler provides one by returning `Page[T]` instead of `list[T]`,
reading the request from `ctx.page`:

<!-- file: examples/tutorial/todo_pages.py -->
```python
def list_items(args: ListArgs, ctx: Ctx, store: Store) -> Page[Item]:
    request = ctx.page
    assert request is not None  # a command that returns a Page always gets one
    shown = sorted((i for i in store.load() if args.all or not i.done), key=lambda i: i.id)
    after = int(request.cursor) if request.cursor is not None else 0
    rest = [i for i in shown if i.id > after]
    batch = rest if request.limit is None else rest[: request.limit]
    more = len(batch) < len(rest)
    return Page(items=batch, next_cursor=str(batch[-1].id) if more else None, total=len(shown))
```

`ctx.page.limit` is `None` under `--limit 0`, and `ctx.page.cursor` is `None` on the first
page. The handler's `next_cursor`, here the last id on the page, travels inside treaty's own
cursor, so the caller still gets opaque text and the same `INVALID_CURSOR` checks. The
command also declares `cursor_check=after_id`, a function that refuses a cursor the handler
could not have issued before the handler runs:

<!-- file: examples/tutorial/todo_pages.py -->
```python
def after_id(cursor: str) -> None:
    """A list cursor is the id of the last item a page returned"""
    if not cursor.isdigit():
        raise ParseError("a list cursor is an item number", context={"cursor": cursor})
```

The same shape serves a source too large to load at once: a database query with
`WHERE id > ? ORDER BY id LIMIT ?`, or an API that returns its own continuation token, which
becomes `next_cursor` as it is.

**Check:** the same purge between pages, with the keyed cursor: the second page starts at
item 21

<!-- check -->
```bash
jq -n '[range(1; 26) | {id: ., text: "Item \(.)", priority: "normal", done: false}]' \
  > tmp/tutorial/todo.json
cursor=$(todo list --db tmp/tutorial/todo.json | jq -r .meta.pagination.next_cursor)
todo done 5 --db tmp/tutorial/todo.json > /dev/null
todo purge --db tmp/tutorial/todo.json --confirm-destructive > /dev/null
todo list --db tmp/tutorial/todo.json --cursor "$cursor" | jq -e '[.data[].id] == [21, 22, 23, 24, 25]
  and .meta.pagination.total == 24 and .meta.pagination.has_more == false'
```

## Paging from `exec` and MCP

The pagination flags have JSON spellings, as every framework flag does: `limit` as an
integer and `cursor` as text, in the `_opts` of an `exec` line and as arguments of the MCP
tool. An agent pages the same way on every route: read `meta.pagination.next_cursor`, and
send it back until it is `null`.

## Next

The audit's next rule is `network-io`, for commands that call out:
[Declare network commands](network-io.md).
