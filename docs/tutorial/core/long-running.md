# Run long work an agent can follow

**Goal:** a command that works through many items has a time limit, stops before it instead
of being cut off, reports one result per item, and shows its progress, so an agent can tell
slow from stuck and retry only what failed

**You need:** a treaty app with a network command, such as `todo` at the end of
[Declare network commands](network-io.md)

**Done when:** the strict audit exits 0 with a batch command in the app, and the batch tests
pass:

<!-- check -->
```bash
uv run treaty audit examples.tutorial.todo_batch:app --strict > /dev/null
uv run pytest -q tests/test_tutorial.py -k import_all > /dev/null
```

The chapter gives `todo` an `import-all` command that imports several feeds in one call. It
starts from [`examples/tutorial/todo_network.py`](../../../examples/tutorial/todo_network.py)
and ends at [`examples/tutorial/todo_batch.py`](../../../examples/tutorial/todo_batch.py).

## Running the checks

Run the **Check** commands from the root of a treaty checkout, in order. `todo` runs this
chapter's example, and the item file goes to a scratch directory:

<!-- check -->
```bash
todo() { uv run examples/tutorial/todo_batch.py "$@"; }
rm -rf tmp/tutorial && mkdir -p tmp/tutorial
```

Each check exits non-zero when it fails: JSON output goes through `jq -e`, which exits 1
when the condition is false. Calls that need feeds are tests in `tests/test_tutorial.py`,
against a local server with fast, slow, and broken ones.

## What goes wrong with a long command

A command that imports ten feeds one after another fails an agent in ways a quick one
cannot:

- **Slow looks like stuck.** Nothing appears for a minute, and the agent kills it
- **A time limit cuts everything.** The run ends with `TIMEOUT` after eight feeds, and the
  agent cannot tell which eight
- **One failure hides the rest.** Feed seven is broken, the command exits 1, and the six
  that worked are reported as nothing
- **A retry repeats everything.** The agent sends the whole call again, and the six feeds
  are imported twice

Each step below removes one of these.

## Step 1: Set the time limit

Every handler runs under a wall-clock limit: `App(default_timeout=...)` for the app, 60
seconds unless set, and `timeout=` on a command that needs a different one. `import-all`
asks for two minutes. Here are the new command's arguments and declaration; the steps
below explain `heartbeat=` and the handler's `Batch` result:

<!-- file: examples/tutorial/todo_batch.py -->
```python
@dataclass(frozen=True, slots=True)
class ImportAll(Common):
    urls: tuple[str, ...] = Arg(
        description="URLs of JSON lists of items to add", pattern_type="url"
    )


@app.command(
    "import-all",
    description="Add the items listed at several URLs, with one result per URL",
    danger_level="mutating",
    has_network_io=True,
    timeout=120,
    heartbeat=True,
    exit_codes=["FEED_INVALID", "STORE_CORRUPT", "STORE_UNWRITABLE"],
    examples=[
        (
            "Add two shared lists",
            "todo import-all https://example.com/a.json https://example.com/b.json",
        )
    ],
)
def import_all(args: ImportAll, ctx: Ctx, store: Store) -> Batch[Imported]:
```

Network and streaming commands also take `--timeout SECONDS` from the caller, `0` for no
limit. When the limit passes, the run exits 10 with `TIMEOUT`, and every response carries
the limit it ran under in `meta.timeout_ms`.

**Check:** `import-all` runs under its own two minutes, `add` under the app's one minute

<!-- check -->
```bash
todo import-all http://127.0.0.1:9/a.json --db tmp/tutorial/todo.json \
  | jq -e '.meta.timeout_ms == 120000'
todo add "Buy milk" --db tmp/tutorial/todo.json | jq -e '.meta.timeout_ms == 60000'
```

## Step 2: Stop before the limit

`TIMEOUT` is the framework's last resort: the handler is still running, the result is lost,
and the state may be partly changed. A handler that knows it is working through a list can
do better, by checking `ctx.remaining`, the seconds left, before it starts each item, and
timing each one with `time.monotonic()` (add `import time`). The code uses four names from
treaty that Step 3 explains: `Batch` and `Outcome`, the result and one item of it,
`ItemError`, one item's error, and `CliExit`, the class of treaty's errors:

<!-- file: examples/tutorial/todo_batch.py -->
```python
def import_all(args: ImportAll, ctx: Ctx, store: Store) -> Batch[Imported]:
    results: list[Outcome[Imported]] = []
    took = 0.0
    for n, url in enumerate(args.urls):
        left = ctx.remaining
        if left is not None and left < took:
            # Less time left than the last feed took: leave this one for a retry
            not_started = ItemError("NOT_STARTED", "the time ran out before this feed", True)
            results.append(Outcome(url, error=not_started))
            continue
        ctx.progress("importing feeds", done=n, total=len(args.urls))
        started = time.monotonic()
        try:
            results.append(Outcome(url, import_items(Import(url=url, db=args.db), ctx, store)))
        except CliExit as failure:
            results.append(Outcome(url, error=failure))
        took = time.monotonic() - started
    return Batch(results)
```

A feed starts only when the time left is at least what the last one took. The ones it has no
time for are not started, and say so with `NOT_STARTED`, marked retryable, since nothing
happened to them. The run then ends with a result the agent can act on: what was imported,
and what to send again. With three feeds that take a second each and `--timeout 2.5`, the
first two are imported and the third is reported as not started; the tests check exactly
that.

Each feed goes through `import_items`, the handler of `import`, called as a plain function:
the batch adds nothing to how one feed is imported, only to how many are.

## Step 3: Report one result per item

The handler returns `treaty.Batch[T]`, one `treaty.Item` per feed with its value or its
error. `todo` has an `Item` of its own, so the example imports treaty's as `Outcome`. An
item's error is an `ItemError(code, message, retryable)`, or any `Exit` the item raised,
which is why the loop catches `CliExit`, the class of every `Exit.NAME(...)` and of every
failed `ctx.http` request, and nothing wider.

`data` becomes a summary and one result per item, in the order the handler worked:

```json
{
  "effect": "created",
  "partial": true,
  "summary": {"total": 2, "succeeded": 1, "failed": 1},
  "results": [
    {"id": "https://example.com/a.json", "ok": true, "effect": "created",
     "added": [{"id": 1, "text": "Buy milk", "priority": "normal", "done": false}]},
    {"id": "https://example.com/bad.json", "ok": false,
     "error": {"code": "FEED_INVALID", "message": "https://example.com/bad.json did not answer with a list of items.", "retryable": false}}
  ]
}
```

Any failed item makes the run exit 3 with `PARTIAL_FAILURE` ("1 of 2 items failed"), also
when every item failed, and keeps `data`, with `partial: true` when some items succeeded.
An agent reads `results`, keeps what worked, and sends only the failed feeds again: the
retryable ones at once, the others after fixing what their error names.

**Check:** two feeds that refuse the connection: exit 3, both failed, both retryable, and
nothing partial, since nothing succeeded

<!-- check -->
```bash
todo import-all http://127.0.0.1:9/a.json http://127.0.0.1:9/b.json --db tmp/tutorial/todo.json \
  | jq -e '.meta.exit_code == 3 and .error.code == "PARTIAL_FAILURE" and .data.partial == false
    and .data.summary == {"failed": 2, "succeeded": 0, "total": 2}
    and [.data.results[] | [.ok, .error.code, .error.retryable]]
      == [[false, "CONNECTION_FAILED", true], [false, "CONNECTION_FAILED", true]]'
```

## Step 4: Show that it is working

`ctx.progress("importing feeds", done=n, total=...)` in the loop reports where the command
is. Where it shows depends on who is watching:

- **A person at a terminal** sees each line on stderr
- **An agent** sees them with `--verbose`, as JSON lines on stderr; off a terminal, only
  errors and warnings reach stderr without it
- **`--heartbeat-interval SECONDS`** prints the latest progress message as plain text on
  stderr at that interval, for a person following a long run in a log

`heartbeat=True` on the command adds one more signal, on stdout: while the handler runs, a
line `{"status": "running", "heartbeat": true, "elapsed_ms": N}` every `--heartbeat-ms`
milliseconds, 10 000 by default, `0` for none. An agent that reads stdout line by line sees
the command is alive without waiting for the end. The envelope is still the last line, so
an agent that waits for the end parses the last line of stdout, and `--schema` shows
`heartbeat_ms`, so it knows which lines to skip. The two flags are easy to mix up:

| Flag | Unit | Where | For |
| --- | --- | --- | --- |
| `--heartbeat-interval` | seconds | stderr, plain text | a person reading a log |
| `--heartbeat-ms` | milliseconds | stdout, JSON lines before the envelope | an agent reading stdout |

**Check:** with `--verbose`, one progress line per feed reaches stderr; the schema announces
the heartbeat

<!-- check -->
```bash
todo import-all http://127.0.0.1:9/a.json http://127.0.0.1:9/b.json --db tmp/tutorial/todo.json \
  --verbose 2> tmp/tutorial/progress.err > /dev/null || true
test "$(grep -c '"level":"progress"' tmp/tutorial/progress.err)" -eq 2
todo import-all --schema | jq -e '.data.heartbeat_ms == 10000'
```

## Step 5: Mark what came from outside

The items a batch imports were written by whoever controls each feed, as in
[Declare network commands](network-io.md#step-5-mark-what-came-from-outside). `import-all`
needs no marking of its own: each result is an `Imported`, whose `added` field is already
`Out(external=True)`, and treaty protects a batch's results by their own type. So `data`
carries `"_trusted": false` and the run adds an `UNTRUSTED_CONTENT` warning, also when some
feeds failed and the run exits 3, since the ones that worked are still in `data`.

To test your own `import-all`, add the `_Feeds` handler and the `feeds` fixture under
"Long-running work" in [`tests/test_tutorial.py`](../../../tests/test_tutorial.py) to
`tests/conftest.py`, beside the server from [Declare network
commands](network-io.md#step-4-handle-what-the-server-answered): it serves feeds that answer
slowly, or with a body that is not a list, and needs `time` besides that server's imports.
Copy the `_results` helper and the `import_all` tests into a test file, with `Envelope` from
`treaty` among its imports.

**Check:** the tests for this chapter pass: a good and a broken feed, tagged as external
though one failed; three slow feeds under a short limit; and the tags on a full success

<!-- check -->
```bash
uv run pytest -q tests/test_tutorial.py -k import_all
```

## Other shapes of long work

`import-all` returns when its work is done. Work with a different shape has its own tool;
the README covers each:

| The work | Use | README |
| --- | --- | --- |
| produces results as it goes, and may never end | `streaming=True`, a generator handler; one JSON line per event; `safe` commands only | [Streaming](../../../README.md#streaming) |
| goes on after the command returns | `async_job=True` returning a `treaty.Job`, and `App(jobs=...)` for `job status` and `job cancel` | [Async jobs](../../../README.md#async-jobs) |
| is ordered steps that can be resumed | `steps=[...]`, `ctx.step(name)` before each, `resumable=True` for `--resume-from` | [Multi-step commands](../../../README.md#multi-step-commands) |
| starts a process that outlives the run | `ctx.spawn`, declared with `background=` | [Declarations](../../../README.md#declarations) |

The `async-job` audit rule reports a command named `start`, `submit`, `enqueue`, `launch`,
or `trigger` that does not return a job, since the name suggests work that goes on after
the command returns and an agent has nothing to poll.

If your project already has AGENTS.md, from `treaty init` or [Ship the agent
docs](../ship/agent-docs.md), this chapter changed the commands, so regenerate what is
derived from them: `uv run treaty agents-md todo.cli:app`, or the AGENTS.md test fails, and,
once it has a conformance profile, `uv run treaty conformance todo.cli:app --force` before
the next run of the kit.

## Next

The next rules this tutorial takes up are `settings-declared` and `env-prefix`, about how a
command reads its settings and secrets: [Read settings and secrets](config.md).
`log-not-print`, which the audit checks before them, comes after that chapter, since it uses
the `todo` it builds.
