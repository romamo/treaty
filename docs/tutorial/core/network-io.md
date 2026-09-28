# Declare network commands

**Goal:** every command that calls out declares it, reaches the network through `ctx.http`,
and fails with an exit code that says whether the server was ever reached

**You need:** a treaty app, such as `todo` at the end of [Declare exit codes](exit-codes.md);
this chapter clears the audit rules `network-io`, `network-timeout`, and `http-client`, and
the `external-data` warning that follows them

**Done when:** the strict audit exits 0 with a network command in the app:

<!-- check -->
```bash
uv run treaty audit examples.tutorial.todo_network:app --strict > /dev/null
```

The chapter gives `todo` an `import` command that adds the items listed at a URL. It
starts from [`examples/tutorial/todo_exit_codes.py`](../../../examples/tutorial/todo_exit_codes.py)
and ends at [`examples/tutorial/todo_network.py`](../../../examples/tutorial/todo_network.py).

## Running the checks

Run the **Check** commands from the root of a treaty checkout, in order. `todo` runs this
chapter's example:

<!-- check -->
```bash
todo() { uv run examples/tutorial/todo_network.py "$@"; }
rm -rf tmp/tutorial && mkdir -p tmp/tutorial
```

Each check exits non-zero when it fails: JSON output goes through `jq -e`, which exits 1 when
the condition is false. `tests/test_tutorial.py` runs the checks the same way; the calls
that need a server run there too, against a local one.

## Why the declaration matters

A command that calls out can fail in ways a local one cannot: the host is down, a proxy is
in the way, the certificate does not verify, the server is slow or rate-limits. An agent
needs to know in advance that a command can fail like that, and afterwards which of those
happened. `has_network_io=True` is how it knows: the manifest says so, MCP clients see the
open-world hint, and the command gets the flags and exit codes that network failures need.

## Step 1: Find the commands that call out

Write the first version the obvious way, with `urllib`, and the audit notices:

```bash
$ uv run treaty audit myapp.cli:app --format plain
...
  1. (warning) network-io [import]: handler source mentions a network library but has_network_io is not declared (heuristic)
     fix: has_network_io=True, then pass ctx.timeout.seconds to every network call
```

The rule searches the handler's own source for `socket`, `http.client`, `urllib`,
`requests`, `httpx`, `aiohttp`, and `grpc`. It cannot see further: a handler that calls
your API client in another module, or an SDK that wraps its own HTTP, passes the rule while
calling out on every run. List those commands yourself; the rule only catches the obvious
ones.

Calling `ctx.http` without the declaration is not left to the audit: the app refuses to
build, naming the handler line and the flag to add.

## Step 2: Declare it

`has_network_io=True` on the command:

<!-- file: examples/tutorial/todo_network.py -->
```python
@app.command(
    "import",
    description="Add the items listed at a URL",
    danger_level="mutating",
    has_network_io=True,
    exit_codes=["FEED_INVALID", "STORE_CORRUPT", "STORE_UNWRITABLE"],
    examples=[("Add a shared list", "todo import --url https://example.com/todo.json")],
)
```

The declaration adds to the command, and only to it:

- **`--timeout`**, since a network call can take any time; the other `todo` commands keep
  the default and have no flag
- **`--proxy URL` and `--no-proxy`**, which override the proxy variables for this run
- **exit 12** in its exit codes without declaring it: the server could not be reached, or
  answered 502 to 504
- **the open-world hint** on its MCP tool, which tells a client the call touches systems
  outside the tool

It also turns on the rules that check the network code itself: `network-timeout`,
`http-client`, `external-data`, and `cleanup`.

**Check:** `import` has the network flags and exit 12; `add` has neither

<!-- check -->
```bash
todo import --schema | jq -e '(.data.flags | has("timeout") and has("proxy") and has("no-proxy"))
  and (.data.exit_codes | has("12"))'
todo add --schema | jq -e '(.data.flags | has("proxy") or has("timeout")) | not'
```

## Step 3: Call out through `ctx.http`

With the command declared, the `urllib` version gets two more warnings:

```bash
  1. (warning) network-timeout [import]: urllib.request.urlopen(...) has no timeout=, so it can outlive --timeout
  2. (warning) http-client [import]: urllib.request.urlopen() skips ctx.http, so --proxy, --no-proxy, and the CA bundle variables do not reach it and a failure has no error.network_context (REQ-F-036, REQ-F-037)
```

Both have one fix. `ctx.http` is a small client built on the standard library that knows
the run it belongs to:

<!-- file: examples/tutorial/todo_network.py -->
```python
def import_items(args: Import, ctx: Ctx, store: Store) -> Imported:
    response = ctx.http.get(args.url)
    entries = feed_entries(response.body) if response.status == 200 else None
```

Each request goes through `HTTPS_PROXY` or `HTTP_PROXY` unless `NO_PROXY` or `--no-proxy`
says otherwise, verifies TLS against `REQUESTS_CA_BUNDLE` or `SSL_CERT_FILE` when set, and
waits at most what is left of the command's timeout. When it fails, the run ends with an
exit code and `error.network_context`: the URL, the proxy used, whether TLS was verified,
and a `curl -v` command that reproduces the request.

A refused connection shows why that matters to an agent. The request never reached a
server, so nothing changed and the call is safe to repeat, and the error says so:

**Check:** a refused connection exits 12 with `CONNECTION_FAILED`, is retryable, carries the
URL and a `curl` to reproduce it, and leaves no item file behind

<!-- check -->
```bash
todo import --url http://127.0.0.1:9/todo.json --db tmp/tutorial/todo.json \
  | jq -e '.meta.exit_code == 12 and .error.code == "CONNECTION_FAILED" and .error.retryable
    and .error.network_context.url == "http://127.0.0.1:9/todo.json"
    and .error.network_context.suggestion == "curl -v http://127.0.0.1:9/todo.json"'
test ! -e tmp/tutorial/todo.json
```

The other failures are mapped the same way: a timeout is exit 10, a certificate that does
not verify is exit 12 `TLS_VERIFY_FAILED` and not retryable, and 502 to 504 are exit 12
`UPSTREAM_UNAVAILABLE`. A 401, 403, or 429 becomes exit 8, 7, or 11 when the command
declares that code; see the [README](../../../README.md#network-and-filesystem) for the
full list.

## Step 4: Handle what the server answered

Every other status comes back to the handler as a response, and deciding what it means is
the handler's job. For `import`, anything but a 200 with a list of items is the same
failure to the caller, so it gets one exit code:

<!-- file: examples/tutorial/todo_network.py -->
```python
    if entries is None:
        raise Exit.FEED_INVALID(
            f"{args.url} did not answer with a list of items",
            context={"url": args.url, "status": response.status},
            fix_required="--url must answer 200 with a JSON list of {text, priority} objects",
        )
```

`FEED_INVALID` is registered like the store codes in [Declare exit codes](exit-codes.md),
as exit 81 with `side_effects="none"`: the handler checks the whole answer before it loads
or saves the item file, so a bad answer changes nothing. Parse everything that came over
the network before acting on any of it.

**Check:** against a local server, a list of two items is added, and a 404 or a body that
is not a list exits 81 and writes no item file

<!-- check -->
```bash
uv run pytest -q tests/test_tutorial.py -k "import_adds or changes_nothing"
```

## Step 5: Mark what came from outside

The text of an imported item was written by whoever controls the URL, and it goes straight
into the agent's context. With the network declared, the audit's `external-data` rule asks
for that to be said:

<!-- file: examples/tutorial/todo_network.py -->
```python
    added: list[Item] = Out(sort_key="id", external=True)
```

`Out(external=True)` on the field that holds outside content, or `external=True` on the
whole command, adds `"_source": "external", "_trusted": false` to `data` and an
`UNTRUSTED_CONTENT` warning. An agent that sees the tag treats the text as data to report,
never as instructions to follow, which is what keeps a hostile list item from steering it.
A caller that trusts the source passes `--no-injection-protection`, which drops the tags
and reports its use.

Only mark what really came from outside: an id the tool computed, or a count, is its own.

## Next

The audit still has advice for `import`: the `cleanup` rule asks network commands for a
hook that runs however the run ends; its chapter is not written yet. The audit's next rule
with a chapter is `path-typed`: [Type path arguments as Path](path-typed.md).
