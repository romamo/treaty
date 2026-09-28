# Release what a run holds

**Goal:** whatever a command opens, starts, or creates for one run is given back however
the run ends: with a result, an error, a timeout, or a signal

**You need:** a treaty app with a network command, such as `todo` at the end of
[Declare network commands](network-io.md); this chapter answers the audit's `cleanup`
advice and its `resource-release` rule

**Done when:** no resource holds something it does not release, and every command that
holds anything past its handler has a hook that gives it back:

<!-- check -->
```bash
uv run treaty audit examples.tutorial.todo_network:app \
  | jq -e '[.data.next_steps[] | select(.rule == "resource-release")] == []'
```

The chapter looks at `todo`'s `import` in
[`examples/tutorial/todo_network.py`](../../../examples/tutorial/todo_network.py). To watch a
hook run on every kind of exit, the checks use
[`examples/slowctl.py`](../../../examples/slowctl.py), the repository's demo of timeouts and
cancellation, whose `fetch` command registers one.

## Running the checks

Run the **Check** commands from the root of a treaty checkout, in order:

<!-- check -->
```bash
slowctl() { uv run examples/slowctl.py "$@"; }
rm -rf tmp/tutorial && mkdir -p tmp/tutorial
export SLOWCTL_AUDIT_LOG=off
```

Each check exits non-zero when it fails: JSON output goes through `jq -e`, which exits 1 when
the condition is false. `tests/test_tutorial.py` runs the checks the same way.

## What "by any exit" means

A command that opens a connection, starts a helper process, or writes a scratch file has to
give it back. The happy path is easy to get right; the others are where things leak. When a
run ends, treaty tears it down in the same order whichever way it ended:

1. each resource's `release()`, newest first
2. the command's `cleanup=` hook

| The run ended with | Teardown |
| --- | --- |
| a result, exit 0 | runs |
| a declared exit code, or a crash | runs |
| `TIMEOUT` (10) | runs; the handler gets two seconds to finish first, then the teardown runs beside it |
| a signal: `CANCELLED` (130, 143) | runs; a second signal during it exits at once |
| a reader that closed stdout (141) | runs |
| an argument error (2) | nothing to tear down: it fails before any resource is acquired |

The teardown runs once per run, however many of those paths race. A hook that raises does
not change the exit code: its traceback goes to stderr and a `CLEANUP_FAILED` warning names
it, so the caller still gets the result the run produced.

## Step 1: Let treaty hold it

The cheapest cleanup is the one you never write. These are released by treaty on every exit
path above, signals included:

- **`ctx.tmp_dir` and `ctx.temp_file()`**: a private directory for the run, removed when it
  ends
- **`ctx.lock(name)`**: an operating-system file lock, released when the process exits, even
  on `SIGKILL`
- **`ctx.http`**: it holds no connection between requests, so there is nothing to close

`todo`'s `import` uses only `ctx.http` and the item file, which `Store.save` writes to a
sibling and renames, removing the sibling itself when the write fails. So `import` holds
nothing past its handler, and the audit's advice for it is the rule's heuristic, not a leak:

```bash
$ uv run treaty audit examples.tutorial.todo_network:app --format plain
...
  2. (advice) cleanup [import]: network command has no cleanup hook for when the run ends, by any exit
     fix: cleanup=release_resources where the function closes connections and removes temp files, and is safe to call twice
```

The rule reports every network command without a hook, because it cannot see what a
handler holds. It is `advice`, so it does not fail `--strict`: when you have checked that a
command holds nothing, leave it. A `cleanup=lambda: None` would silence it without making
anything safer.

## Step 2: Give each resource a `release`

What a run acquires for its own use belongs in a resource, and a resource that holds
something defines `release`:

```python
@dataclass(frozen=True, slots=True)
class Feed:
    connection: FeedConnection

    @classmethod
    def acquire(cls, args: Import, ctx: Ctx) -> Self:
        return cls(FeedConnection.open(args.url, timeout=ctx.timeout.seconds))

    def release(self) -> None:
        self.connection.close()
```

treaty calls `release` for every resource it acquired, and only those: when a second
resource's `acquire` fails, the first is still released, and the handler never runs. That is
what a `try`/`finally` in the handler cannot give you, since the handler is not the first
thing that runs.

The `resource-release` rule reports a resource class with a `close`, `__exit__`,
`terminate`, or `unlink` method and no `release`, the shape of a class that holds something
nothing gives back. `release` should only give back: a `release` that raises is reported as
`CLEANUP_FAILED` and the next resource is still released.

**Check:** no `todo` resource holds something without releasing it; `Store` holds only a path

<!-- check -->
```bash
uv run treaty audit examples.tutorial.todo_network:app \
  | jq -e '[.data.next_steps[] | select(.rule == "resource-release")] == []'
```

## Step 3: Use `cleanup=` for what outlives a resource

Some things are not per-run objects: a client an SDK creates on first use and keeps at
module level, a helper process started once, a file whose name only the module knows. The
`cleanup=` hook is for those. It takes no arguments, runs after every resource is released,
and is registered on the command:

<!-- file: examples/slowctl.py -->
```python
@app.command(
    "fetch",
    description="Pretend to call a slow upstream",
    has_network_io=True,
    cleanup=release_resources,
    danger_level="safe",
    exit_codes=(),
)
```

Write the hook for the worst case: the run may have ended before the handler created
anything, so the hook checks what exists and gives back only that. treaty runs it once per
run, but an `exec` plan runs many requests in one process, so module-level state the hook
closes may be opened again by the next line: open it lazily, and let the hook leave it ready
to be opened again.

**Check:** the hook runs after a result and after a timeout, and not after an argument
error, where nothing ran

<!-- check -->
```bash
slowctl fetch --seconds 0 2> tmp/tutorial/ok.err | jq -e '.meta.exit_code == 0'
grep -qx 'cleanup: releasing resources' tmp/tutorial/ok.err
slowctl fetch --seconds 5 --timeout 0.3 2> tmp/tutorial/timeout.err \
  | jq -e '.meta.exit_code == 10 and .error.code == "TIMEOUT"'
grep -qx 'cleanup: releasing resources' tmp/tutorial/timeout.err
slowctl fetch --bogus 2> tmp/tutorial/bad.err | jq -e '.meta.exit_code == 2'
test "$(grep -c 'cleanup' tmp/tutorial/bad.err)" -eq 0
```

## Step 4: Test the signal path

A signal is the exit path tests skip, and the one agents take: an agent that gives up on a
slow call kills it. Start the command, wait until it is working, send `SIGTERM`, and check
that the run ended as `CANCELLED` with the teardown done:

**Check:** `SIGTERM` in the middle of `fetch` exits 143 with `CANCELLED`, and the hook ran
once

<!-- check -->
```bash
uv run examples/slowctl.py fetch --seconds 10 --verbose > tmp/tutorial/sig.out 2> tmp/tutorial/sig.err &
pid=$!
for _ in $(seq 100); do grep -q fetching tmp/tutorial/sig.err && break; sleep 0.1; done
kill -TERM "$pid"
status=0 && wait "$pid" || status=$?
test "$status" -eq 143
jq -e '.error.code == "CANCELLED"' tmp/tutorial/sig.out
test "$(grep -c 'cleanup: releasing resources' tmp/tutorial/sig.err)" -eq 1
```

`--verbose` makes the handler's `ctx.log` line reach stderr off a terminal, which is how the
check knows the handler started. The command is started directly, not through the
`slowctl` shell function: a function run in the background is a subshell, and `kill` would
stop the subshell while the command it started keeps running. In a Python test, `app.run` in a thread and a signal sent to
the process do the same; `tests/test_lifecycle.py` covers each exit path that way.

## Next

That is the last rule the core chapters follow. The audit's final rule, `profile`, asks for a
conformance profile, which [Run the conformance kit](../ship/conformance.md) writes and runs.
