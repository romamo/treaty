# Change the contract safely

**Goal:** a new release can rename a command, retire a flag, and change an output without
breaking an agent that learned the old contract, and CI fails when a change would

**You need:** a released treaty app, such as `todo` 1.0.0 at the end of
[Declare exit codes](../core/exit-codes.md); this chapter covers the audit rules `additive`
and `schema-version`

**Done when:** `todo` 1.1.0 passes the strict audit against the 1.0.0 manifest:

```bash
uv run treaty audit todo.cli:app --baseline todo-1.0.0.json --strict
```

In this repository, the 1.1.0 file is `examples.tutorial.todo_v2:app`, and the checks keep
the baseline under `tmp/tutorial/`.

The chapter takes `todo` from 1.0.0,
[`examples/tutorial/todo_exit_codes.py`](../../../examples/tutorial/todo_exit_codes.py), to
1.1.0, [`examples/tutorial/todo_v2.py`](../../../examples/tutorial/todo_v2.py): `done` is
renamed `complete`, and `list --all` gives way to `--include-done`.

## Running the checks

Run the **Check** commands from the root of a treaty checkout, in order. `before` runs
1.0.0 and `todo` runs 1.1.0; the item file goes to a scratch directory, with one item in it:

<!-- check -->
```bash
before() { uv run examples/tutorial/todo_exit_codes.py "$@"; }
todo() { uv run examples/tutorial/todo_v2.py "$@"; }
rm -rf tmp/tutorial && mkdir -p tmp/tutorial/project
before add "Buy milk" --db tmp/tutorial/todo.json > /dev/null
```

Each check exits non-zero when it fails: JSON output goes through `jq -e`, which exits 1 when
the condition is false.

## What an agent keeps from the last release

An agent does not read your changelog. It keeps what worked: an invocation in a script, a
skill file, a prompt that says "run `todo done 3`", code that reads `data.item.id`. A
release that renames `done`, drops `--all`, or moves `item` breaks every one of those, and
the agent finds out from an error in the middle of a task. So a change to the contract
follows three rules:

- **Nothing disappears without notice.** A renamed command keeps answering at its old name;
  a flag that is going away is deprecated for at least a minor release first
- **Outputs change by version.** Adding a field is a minor change; removing or retyping one
  is a major one, and the command's `schema_version` says which
- **CI checks both** against what the last release published

## Step 1: Keep the last release's manifest

The manifest is the contract, so keep a copy of it with every release:

```bash
uv run todo manifest > todo-1.0.0.json    # when you release 1.0.0, committed beside the code
```

If a release is already out and you kept no copy, make one from its tag, beside your
working tree:

```bash
git worktree add ../todo-1.0.0 v1.0.0
(cd ../todo-1.0.0 && uv run todo manifest) > todo-1.0.0.json
git worktree remove ../todo-1.0.0
```

Every check below compares the working tree against that file.

The version that counts is `App(version=...)`: `--version`, the manifest, AGENTS.md, and the
baseline's name all read it. Keep the one in `pyproject.toml` the same, or have the app read
it with `importlib.metadata.version("todo")` so there is only one to change.

**Check:** the 1.0.0 manifest lists `done` and `list --all`

<!-- check -->
```bash
before manifest > tmp/tutorial/todo-1.0.0.json
jq -e '.data.commands | has("done") and (.list.flags | has("all"))' tmp/tutorial/todo-1.0.0.json
```

## Step 2: Rename a command behind a redirect

In your project, 1.1.0 is one set of edits, which this step and the next explain;
[`todo_v2.py`](../../../examples/tutorial/todo_v2.py) has them all:

- `App(version="1.1.0")`
- `done` renamed `complete`, with its example: `todo complete 3`
- `app.redirect("done", to="complete")` at module level
- `list` gains `--include-done`, its handler honours either flag, `--all` is deprecated, and
  its example uses the new flag
- your tests call the new names: `app.call("complete", ...)` and `{"include_done": True}`;
  an `app.call("done", ...)` answers `REDIRECTED`, as an agent's would
- the MCP client test from [Serve commands over MCP](mcp.md#step-6-test-through-a-real-client)
  calls `complete`, and its expected tool list has `complete` in place of `done`, plus
  `changelog` if you take Step 5
- AGENTS.md, the skills, and the MCP tool list regenerated, as [Ship the agent
  docs](agent-docs.md) shows
- the conformance profile replaced with `treaty conformance todo.cli:app --force`, since
  `list`'s new example changes its probe; commit it with the rest

1.1.0 calls the command `complete`. The old name keeps answering, with one line at module
level, after the renamed command's definition (the redirect refuses a target that is not
registered yet):

<!-- file: examples/tutorial/todo_v2.py -->
```python
app.redirect("done", to="complete")
```

`todo done 1` then exits 13 with `REDIRECTED`, and `error.redirect.command` holds the same
call under the new name, arguments and all, ready to run as it is. An agent reruns it and
updates what it keeps; in `exec` and MCP, the replacement is the new command path. The
manifest lists `done` among `complete`'s `aliases`, so an agent reading the manifest finds
the old name too.

**Check:** the old name answers with the new call; the manifest records the alias

<!-- check -->
```bash
todo done 1 --db tmp/tutorial/todo.json | jq -e '.meta.exit_code == 13 and .error.code == "REDIRECTED"
  and .error.redirect.command == "todo complete 1 --db tmp/tutorial/todo.json"'
todo manifest | jq -e '.data.commands.complete.aliases == ["done"]'
```

`treaty audit --baseline` holds you to it. Its `additive` rule compares the app with the
baseline manifest and reports, as an `error`, every command, flag, and exit code that is
gone without a redirect or a release that deprecated it, unless the major version went up.
Without the redirect line, 1.1.0 fails. To see it in your project, comment out the
`app.redirect` line and run `uv run treaty audit todo.cli:app --baseline todo-1.0.0.json`;
the check below does the same to a copy:

**Check:** 1.1.0 passes against the baseline; the same code without the redirect has an
`additive` error for `done`

<!-- check -->
```bash
uv run treaty audit examples.tutorial.todo_v2:app --baseline tmp/tutorial/todo-1.0.0.json --strict > /dev/null
grep -v '^app.redirect' examples/tutorial/todo_v2.py > tmp/tutorial/project/todo.py
uv run treaty audit todo:app --cwd tmp/tutorial/project --baseline "$PWD/tmp/tutorial/todo-1.0.0.json" \
  | jq -e '[.data.rules[].findings[] | select(.rule == "additive") | [.severity, .command]] == [["error", "done"]]'
```

## Step 3: Deprecate a flag before removing it

`--all` becomes `--include-done` in 1.1.0 and goes away in 2.0.0. Until then both work, and
the old one says what replaces it:

<!-- file: examples/tutorial/todo_v2.py -->
```python
@dataclass(frozen=True, slots=True)
class ListArgs(Common):
    all: bool = Flag(
        default=False,
        short="a",
        description="Include completed items",
        deprecated=Deprecated("1.1.0", replacement="include-done", removed_in="2.0.0"),
    )
    include_done: bool = Flag(default=False, description="Include completed items")
```

A run that passes `--all` still succeeds, with a `DEPRECATED_FLAG` warning in the envelope
and a line on stderr naming the replacement and the version that removes it. The manifest's
description says it is deprecated, so an agent reading the contract picks the new flag. A
whole command is deprecated the same way, with `deprecated=Deprecated(...)` on
`@app.command`, and warns `DEPRECATED`.

Update the examples when you deprecate: `list`'s example now shows `--include-done`, since
agents copy examples before they read descriptions. Update suggestions and fixes that name
the old flag too, such as `done`'s `NOT_FOUND` suggestion.

**Check:** `--all` still works and warns with the replacement; the schema marks it

<!-- check -->
```bash
todo list --all --db tmp/tutorial/todo.json 2> /dev/null | jq -e '.ok
  and .warnings[0].code == "DEPRECATED_FLAG" and .warnings[0].context.replacement == "--include-done"'
todo list --schema | jq -e '.data.flags.all.description
  == "Include completed items (deprecated since 1.1.0; use --include-done)"'
```

## Step 4: Version the output

A command's output is part of the contract too. Each command has a `schema_version`,
`"1.0"` unless declared, reported as `meta.schema_version` on every response:

- **Adding a field** is a minor change: an agent that ignores unknown keys keeps working.
  Bump the minor, `schema_version="1.1"`
- **Removing, renaming, or retyping a field** is a major change. Bump the major, and give
  callers that still need the old shape a `compat=` shim, which `--schema-version 1`
  selects; see [Response meta](../../../README.md#response-meta) in the README

`treaty schema-lock myapp.cli:app` records every command's schema version and output schema
in `treaty-schema.lock`. Commit it; from then on the audit's `schema-version` rule compares
each output with the lock, and reports a change the version does not announce:

```bash
  1. (warning) schema-version [add]: output schema gained fields since treaty-schema.lock, but schema_version is still 1.0 (REQ-F-022)
     fix: schema_version="1.1", then treaty schema-lock to record it
```

**Check:** lock the 1.1.0 outputs, then add a field to `Item` without a version bump: the
audit reports every command whose result holds an item, four in this file

<!-- check -->
```bash
cp examples/tutorial/todo_v2.py tmp/tutorial/project/todo.py
uv run treaty schema-lock todo:app --cwd tmp/tutorial/project | jq -e '.data.commands == 4'
perl -pi -e 's/^    done: bool$/    done: bool\n    note: str = ""/' tmp/tutorial/project/todo.py
uv run treaty audit todo:app --cwd tmp/tutorial/project \
  | jq -e '[.data.rules[].findings[] | select(.rule == "schema-version") | .command] == ["add", "complete", "list", "purge"]'
```

The lock and the module are both read from `--cwd`, which is why the check copies `todo`
into its own directory: a real project runs the same commands from its root.

## Step 5: Write it down for agents

This step is optional: the redirect, the deprecation warning, and the manifest already tell
an agent what moved. A schema changelog also lets it ask what changed since the release it
learned, instead of failing into each change.

Add one keyword to the `App(...)` you already have, keeping every other argument, such as
`settings=` from [Read settings and secrets](../core/config.md):

```python
    schema_changelog=Path(__file__).parent / "schema-changelog.json",
```

That gives the app a `changelog` command, which lists each version's added, removed, and
changed fields and whether it breaks callers; `--since 1.0.0` keeps the newer ones.
`treaty changelog-add myapp.cli:app` writes the next entry. It keeps a snapshot of the
manifest, `<app>.manifest.json`, beside the changelog file, diffs the live manifest against
it, then updates the snapshot, so each entry holds what changed since the one before.

With no snapshot, the first entry lists every command as added. Seed the snapshot with the
last release's manifest from Step 1, so the first entry holds only this release's changes:

```bash
cp todo-1.0.0.json src/todo/todo.manifest.json   # beside schema-changelog.json
uv run treaty changelog-add todo.cli:app
```

Run `changelog-add` once per release, after the last contract change. The entry for 1.1.0
lists `complete`, `list.flags.include-done`, and `changelog` itself as added, and `done` as
removed. It is marked `breaking`: a call to `done` now exits 13 instead of completing the
item, even though the redirect says what to run instead. The README describes the file's
format under Schema changelog, in [Response meta](../../../README.md#response-meta).

**Check:** seeded with the 1.0.0 manifest, the 1.1.0 entry records the rename and the new
flag, and the `changelog` command serves it

<!-- check -->
```bash
mkdir -p tmp/tutorial/changelog
perl -pe 's|companions=\("mkdir",\)\)$|companions=("mkdir",), schema_changelog=Path(__file__).parent / "schema-changelog.json")|' \
  examples/tutorial/todo_v2.py > tmp/tutorial/changelog/todo.py
cp tmp/tutorial/todo-1.0.0.json tmp/tutorial/changelog/todo.manifest.json
uv run treaty changelog-add todo:app --cwd tmp/tutorial/changelog | jq -e '.data.entry
  | .version == "1.1.0" and .breaking and (.removed | index("done")) != null
  and (.added | index("list.flags.include-done")) != null'
uv run tmp/tutorial/changelog/todo.py changelog --since 1.0.0 \
  | jq -e '[.data.entries[].version] == ["1.1.0"]'
```

## Step 6: Gate the release

Run the contract checks in CI, beside the audit and the conformance kit:

```bash
uv run treaty audit myapp.cli:app --strict                                 # every rule
uv run treaty audit myapp.cli:app --baseline myapp-1.0.0.json --strict     # nothing vanished
```

With `treaty-schema.lock` committed, the first line also runs `schema-version` against it.
At each release, save the new manifest as the next baseline, run `treaty schema-lock` if an
output changed, regenerate the agent docs from [Ship the agent docs](agent-docs.md), and
replace the conformance profile with `--force` when the commands' examples changed, so every
file an agent reads names the new commands.

## Next

`todo` can change from release to release without leaving an agent behind. The last step
puts every check the tutorial built into one test suite and one CI job:
[Test the contract and gate CI](testing.md).
