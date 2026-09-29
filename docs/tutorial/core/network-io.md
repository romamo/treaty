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

The chapter gives `todo` an `import` command that adds the items listed at a URL. It starts
from [`examples/tutorial/todo_exit_codes.py`](../../../examples/tutorial/todo_exit_codes.py)
and ends at
[`examples/tutorial/todo_network.py`](../../../examples/tutorial/todo_network.py). The steps
show the parts that matter for agents; in your project, copy the `FEED_INVALID` registration
(Step 4), `Import`, `Imported`, `feed_entries` (with `from typing import get_args`), and
`import_items` from that file.

## Running the checks

Run the **Check** commands from the root of a treaty checkout, in order. `todo` runs this
chapter's example:

<!-- check -->
```bash
todo() { uv run examples/tutorial/todo_network.py "$@"; }
rm -rf tmp/tutorial && mkdir -p tmp/tutorial
```

Each check exits non-zero when it fails: JSON output goes through `jq -e`, which exits 1
when the condition is false. Calls that need a server are tests in `tests/test_tutorial.py`,
against a local one.

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
     fix: has_network_io=True, then call out through ctx.http, which keeps to the command's --timeout
```

The rule searches the handler's source, and the functions of its module it calls by name and
the resources it takes for calls into `socket`, `http.client`, `urllib`, `requests`,
`httpx`, `aiohttp`, and `grpc`. It cannot see further: a handler that calls your API client
in another module, or an SDK that wraps its own HTTP, passes the rule while calling out on
every run. List those commands yourself; the rule only catches the obvious ones.

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

`STORE_CORRUPT` and `STORE_UNWRITABLE` are the codes [Declare exit codes](exit-codes.md)
registered; a command may only declare codes the app has registered, so do that chapter
first, or declare only `FEED_INVALID`, below. The arguments are one flag, a URL, checked
before the handler runs by `pattern_type="url"`, and the result carries what was added:

<!-- file: examples/tutorial/todo_network.py -->
```python
@dataclass(frozen=True, slots=True)
class Import(Common):
    url: str = Flag(description="URL of a JSON list of items to add", pattern_type="url")
```

<!-- file: examples/tutorial/todo_network.py -->
```python
@dataclass(frozen=True, slots=True)
class Imported:
    effect: str
    added: list[Item] = Out(sort_key="id", external=True)
```

The declaration adds to the command, and only to it:

- **`--timeout`**, since a network call can take any time; the other `todo` commands keep
  the default and have no flag
- **`--proxy URL` and `--no-proxy`**, which override the proxy variables for this run
- **exit 12**, added to its exit codes automatically: the server could not be reached, or
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

`FEED_INVALID` is registered like the store codes in [Declare exit codes](exit-codes.md):

<!-- file: examples/tutorial/todo_network.py -->
```python
app.exit_code(
    "FEED_INVALID",
    81,
    description="The URL did not answer with a list of items; nothing was changed",
    retryable=False,
    side_effects="none",
)
```

It declares `side_effects="none"` because the handler checks the whole answer before it
loads or saves the item file, so a bad answer changes nothing. `feed_entries()`, in the
example file, does that check: it parses the body and returns `None` unless it is a list of
objects with a `text` and a valid `priority`. Parse everything that came over the network
before acting on any of it.

Test it against a local server, never the real feed. The tutorial's tests start one with
the standard library and call `import` in-process:

<!-- file: tests/test_tutorial.py -->
```python
@pytest.fixture
def feed(tmp_path: Path) -> Iterator[str]:
    """A local server for tmp_path/feed: todo.json holds two items, bad.json is not a list"""
    root = tmp_path / "feed"
    root.mkdir()
    items = [{"text": "Buy milk", "priority": "high"}, {"text": "Walk dog", "priority": "normal"}]
    (root / "todo.json").write_text(json.dumps(items))
    (root / "bad.json").write_text('{"oops": 1}')
    handler = functools.partial(_Quiet, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_import_adds_the_items_at_a_url_marked_untrusted(feed: str, tmp_path: Path) -> None:
    db = str(tmp_path / "todo.json")
    env = todo_network.app.call("import", {"url": f"{feed}/todo.json", "db": db}, env={})
```

In your project, put the fixture and `_Quiet` above it (a handler that keeps the server's
request log off the test output) in `tests/conftest.py`, where pytest finds a fixture for
every test file, with the imports they use:

```python
import functools
import http.server
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
```

Copy the tests under "Declare network commands" in
[`tests/test_tutorial.py`](../../../tests/test_tutorial.py) into a test file of your own,
and call your `app` where they call `todo_network.app`. Each test file needs its own
imports: `pytest`, `Path`, your `app`, and whatever else the copied code names; `uvx ruff
check --select F821 tests` lists any you missed. The chapters that follow add their own
servers to `conftest.py` the same way.

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

Only mark what really came from outside: an id the tool computed, or a count, is its own. A
command that calls out but returns only such values says so with `external=False`, which
clears the `external-data` warning.

This chapter added a network command. If your project has AGENTS.md, from `treaty init` or
[Ship the agent docs](../ship/agent-docs.md), run `uv run treaty agents-md todo.cli:app`, or
the AGENTS.md test fails; if you generated skills and an MCP tool list there, regenerate
them too, as its [Step 6](../ship/agent-docs.md#step-6-gate-ci-on-all-three) does. If the
project has a conformance profile, run `uv run treaty conformance todo.cli:app --force`
before the next run of the kit.

## Next

The audit's next rules are about commands that run other programs:
[Run other programs](programs.md).

The audit also has advice for `import`: the `cleanup` rule asks network commands for a
hook that runs however the run ends. A later chapter takes it up, in the audit's order.
