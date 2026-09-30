# Ship the agent docs

**Goal:** the docs an agent reads before its first call (AGENTS.md, skill files, and the
MCP tool list) are generated from the commands, and CI fails when any of them stops
matching the binary

**You need:** a treaty app, such as the end of [Declare exit codes](../core/exit-codes.md);
the MCP step needs the `treaty[mcp]` extra from [Serve commands over MCP](mcp.md)

**Done when:** `check-docs` passes on all three, which in your project reads:

```bash
uv run treaty check-docs myapp.cli:app AGENTS.md skills mcp-tools.json
```

The chapter continues the `todo` CLI at
[`examples/tutorial/todo_exit_codes.py`](../../../examples/tutorial/todo_exit_codes.py).

## Running the checks

Run the **Check** commands from the root of a treaty checkout, in order: each one uses the
files the one before it wrote. `todo` runs the finished example, and the docs go to a scratch
directory:

<!-- check -->
```bash
todo() { uv run examples/tutorial/todo_exit_codes.py "$@"; }
rm -rf tmp/tutorial && mkdir -p tmp/tutorial
```

Each check exits non-zero when it fails: JSON output goes through `jq -e`, which exits 1 when
the condition is false.

## Why generate the docs

An agent decides how to call a CLI before it runs anything, from whatever it was given to
read. Three kinds of agent read three kinds of file:

| File | Read by | Written by |
| --- | --- | --- |
| `AGENTS.md` | coding agents working in your repository | `treaty agents-md` |
| `skills/` | agent runtimes that load one Markdown skill per task | `todo generate-skills` |
| `mcp-tools.json` | nobody at run time; it records what MCP clients were promised | `treaty-mcp --list-tools` |

A hand-written version of any of them is correct the day it is written. After that, every
renamed flag, new command, and changed exit code makes it a little more wrong, and an agent
that follows it fails with an argument error it had no way to predict. treaty writes all
three from the same registry that parses the arguments, and `check-docs` compares them with
the binary, so drift fails a build instead of an agent's call.

## Step 1: Write AGENTS.md

```bash
uv run treaty agents-md myapp.cli:app
```

writes `AGENTS.md` in the current directory (`--path` puts it elsewhere). The file has three
parts:

- **`<!-- cli-version: 1.0.0 -->`** on the first line: the version the file describes,
  which `check-docs` compares with `todo --version`
- **`## Installation`**: a guess, `uv tool install todo` from the app's name, or
  `uv tool install .` in a project made with `treaty init`. It is written once and then left
  alone, so replace it with how your CLI is really installed
- **The generated sections** between `<!-- treaty:begin -->` and `<!-- treaty:end -->`:
  Canonical Invocation (every command with its description), Non-Interactive Flags (the
  flags that stand in for prompts, such as `--confirm-destructive` on `purge`),
  Environment Variables (every `TODO_*` variable the app reads), Input Conventions, and CI
  Validation

**Check:** the file declares the app's version and lists `purge` with its description

<!-- check -->
```bash
uv run treaty agents-md examples.tutorial.todo_exit_codes:app --path tmp/tutorial/AGENTS.md \
  | jq -e '.data.effect == "created" and .data.cli_version == "1.0.0"'
grep -qx '<!-- cli-version: 1.0.0 -->' tmp/tutorial/AGENTS.md
grep -qx -- '- `todo purge`: Delete completed items' tmp/tutorial/AGENTS.md
```

## Step 2: Add what the generator cannot know

The registry knows every command, flag, and exit code. It does not know where the data
lives, which command to run first in a new checkout, or what a failure usually means in
practice. Write that yourself, outside the markers:

```markdown
## Data

Items live in one JSON file, `~/.todo.json` unless `--db` names another. Completed items
stay in it until `todo purge --confirm-destructive` removes them.
```

Run `agents-md` again whenever a command changes. It rewrites the text between the markers
and the version line, and keeps everything else, so the section above survives every
regeneration. When nothing it writes has changed, the run reports `noop` and leaves the file
alone.

**Check:** add the section, regenerate, and the section is still there

<!-- check -->
```bash
cat >> tmp/tutorial/AGENTS.md <<'EOF'

## Data

Items live in one JSON file, `~/.todo.json` unless `--db` names another. Completed items
stay in it until `todo purge --confirm-destructive` removes them.
EOF
uv run treaty agents-md examples.tutorial.todo_exit_codes:app --path tmp/tutorial/AGENTS.md \
  | jq -e '.data.effect == "noop"'
grep -qx '## Data' tmp/tutorial/AGENTS.md
```

## Step 3: Check it against the binary

`check-docs` reads the whole file, your sections included, and checks every command, flag,
and environment variable it names against the app's `--help`, and the version line against
`--version`. It also renders the sections between the markers as `agents-md` would now and
compares them with the file's, so a command or variable added since the file was written
fails too:

```bash
uv run treaty check-docs myapp.cli:app AGENTS.md
```

A mismatch exits 81 with `DOCS_OUT_OF_DATE`, one line per problem, each with its file and
line. A tip that still uses the `--yes` of the argparse version reads:

```bash
$ uv run treaty check-docs examples.tutorial.todo_exit_codes:app tmp/tutorial/drifted.md --format plain
- /path/to/treaty/tmp/tutorial/drifted.md:77 flag --yes: not in todo purge --help
treaty: DOCS_OUT_OF_DATE: 1 item in the docs disagrees with todo 1.0.0
  files: ['/path/to/treaty/tmp/tutorial/drifted.md']
hint: run treaty agents-md examples.tutorial.todo_exit_codes:app, then fix what it does not write
```

The same check catches a command that does not exist (`todo clear`), a variable the app
never reads (`TODO_DB`), and a version line left behind by a release. The hint says what to
do: regenerate for the sections treaty writes, and edit your own sections by hand, since
treaty never rewrites text outside the markers.

In your own sections, `check-docs` judges names, not prose: a tip that says the wrong thing
in the right words passes. Read those sections when a command's behaviour changes.

**Check:** the file passes as written, and a copy with a stale flag fails with exactly that
flag

<!-- check -->
```bash
uv run treaty check-docs examples.tutorial.todo_exit_codes:app tmp/tutorial/AGENTS.md \
  | jq -e '.ok and .data.mismatches == []'
cp tmp/tutorial/AGENTS.md tmp/tutorial/drifted.md
echo 'Skip the preview with `todo purge --yes`.' >> tmp/tutorial/drifted.md
uv run treaty check-docs examples.tutorial.todo_exit_codes:app tmp/tutorial/drifted.md \
  | jq -e '.meta.exit_code == 81 and .error.code == "DOCS_OUT_OF_DATE"
    and [.data.mismatches[] | [.kind, .name]] == [["flag", "--yes"]]'
```

**Check:** an AGENTS.md written before `import` existed fails once the app has it, though
every name in the file is still valid

<!-- check -->
```bash
mkdir -p tmp/tutorial/grown
cp examples/tutorial/todo_exit_codes.py tmp/tutorial/grown/todo.py
uv run treaty agents-md todo:app --cwd tmp/tutorial/grown > /dev/null
cp examples/tutorial/todo_network.py tmp/tutorial/grown/todo.py
uv run treaty check-docs todo:app AGENTS.md --cwd tmp/tutorial/grown \
  | jq -e '[.data.mismatches[] | [.kind, .name]] == [["section", "Canonical Invocation"]]'
```

## Step 4: Generate the skill files

Every treaty app has a `generate-skills` built-in. In your project, run it, like every
other `todo` command in this chapter, through uv:

```bash
uv run todo generate-skills --output-dir skills
```

It writes `CONTEXT.md`, an overview of the tool with its commands, danger levels, and exit
codes, and one `SKILL-<command>.md` for each command the app registers (built-ins get none).
Each skill starts with YAML frontmatter an agent runtime can index: `name` (`todo-purge`),
`description`, `version`, `command`, and `args`, the JSON Schema of the command's input.
The body has the usage, the flags, and guardrails taken from the declarations. For `purge`:

```markdown
## Guardrails

- Danger level: destructive
- Destructive: run with --dry-run first and read data.would_affect; apply only with --confirm-destructive, which nothing else implies
- Exit 6 CONFLICT (not retryable): The resource already exists or a version conflict was detected
- Exit 79 STORE_CORRUPT (not retryable): The item file is not a valid todo file; nothing was changed
- Exit 80 STORE_UNWRITABLE (not retryable): The item file could not be written; the previous file is intact
- Shared exit codes, 2 among them for bad arguments: todo manifest
```

The skill lists each exit code from [Declare exit codes](../core/exit-codes.md) with its
description and whether it is retryable. treaty adds `CONFLICT` to every mutating and
destructive command. Agents read these descriptions to decide what to do, so write them
carefully. Skill files are always Markdown: `--format` changes the envelope the command
answers with, not the files. Commit the directory, or copy it to wherever your agent loads
skills from.

**Check:** a context file and one skill per `todo` command, and they pass `check-docs`; the
`null` in the list is `CONTEXT.md`, which belongs to no single command

<!-- check -->
```bash
todo generate-skills --output-dir tmp/tutorial/skills \
  | jq -e '[.data.skills[].command] == [null, "add", "done", "list", "purge"]'
uv run treaty check-docs examples.tutorial.todo_exit_codes:app tmp/tutorial/skills | jq -e '.ok'
```

## Step 5: Save the MCP tool list

An MCP client learns the tools when it connects, so there is nothing to hand it in advance.
What drifts is your promise: a client, or an agent prompt, built against yesterday's tool
list breaks when a field is renamed. Save the list and commit it:

```bash
uv run treaty-mcp myapp.cli:app --list-tools > mcp-tools.json
```

`mcp-validate`, another built-in, compares a saved list with the current commands:

```bash
uv run todo mcp-validate --mcp-schema-file mcp-tools.json
```

It reports tools missing from the file, tools the file has that the app no longer serves,
and, for each tool, input and output fields that were added, removed, or changed JSON type.
Drift exits 1 with `SCHEMA_DRIFT_DETECTED` and the differences in `data.drift`. It compares
names and types only: a changed description or a new enum value passes, so regenerate the
file and read the diff when those matter.

**Check:** the fresh list matches; a list where `done`'s `id` was still a string is reported
as exactly that change

<!-- check -->
```bash
uv run treaty-mcp examples.tutorial.todo_exit_codes:app --list-tools > tmp/tutorial/mcp-tools.json
todo mcp-validate --mcp-schema-file tmp/tutorial/mcp-tools.json | jq -e '.ok'
jq '(.tools[] | select(.name == "done") | .inputSchema.properties.id.type) = "string"' \
  tmp/tutorial/mcp-tools.json > tmp/tutorial/mcp-tools-old.json
todo mcp-validate --mcp-schema-file tmp/tutorial/mcp-tools-old.json | jq -e '.meta.exit_code == 1
  and .error.code == "SCHEMA_DRIFT_DETECTED"
  and .data.drift.changed == [{"command": "done", "field": "input.id", "cli_type": "integer", "mcp_type": "string"}]'
```

## Step 6: Gate CI on all three

Two kinds of check cover the docs. `check-docs` fails when a name the docs use no longer
exists or AGENTS.md's generated sections are out of date, and `mcp-validate` when a tool
changed; regenerating and diffing fails when anything treaty writes has changed at all, in
the skill files and the tool list too, and shows the change in the pull request. Below,
`myapp` is your CLI's command and `myapp.cli:app` the import path of its `App`:

```bash
uv run treaty check-docs myapp.cli:app AGENTS.md skills mcp-tools.json
uv run myapp mcp-validate --mcp-schema-file mcp-tools.json

uv run treaty agents-md myapp.cli:app
rm -rf skills && uv run myapp generate-skills --output-dir skills
uv run treaty-mcp myapp.cli:app --list-tools > mcp-tools.json
git add --intent-to-add AGENTS.md skills mcp-tools.json
git diff --exit-code AGENTS.md skills mcp-tools.json
```

`generate-skills` writes a file per command and never deletes one, so the `rm -rf skills`
first: after a rename, the old command's skill file would stay behind and still pass the
diff. `git diff` ignores files git does not track, so `git add --intent-to-add` first: a new
command's skill file, never committed, then shows up in the diff and fails it.

A project made with `treaty init` runs the AGENTS.md part as a test,
`tests/test_agents_md.py`, so `uv run pytest` fails as soon as AGENTS.md drifts ([Start a
new CLI](../A-new/start.md#step-8-regenerate-agentsmd) shows it failing and the fix); a
migrated project adds it in [Test the contract and gate
CI](testing.md#step-2-test-the-contract-in-every-project).

When a check fails, regenerate, read the diff, and commit it with the change that caused
it. The diff is the part of your release notes that agents read.

**Check:** the chapter's **Done when**, on the files this chapter wrote

<!-- check -->
```bash
uv run treaty check-docs examples.tutorial.todo_exit_codes:app \
  tmp/tutorial/AGENTS.md tmp/tutorial/skills tmp/tutorial/mcp-tools.json | jq -e '.ok'
```

## Next

`todo` passes the strict audit and the conformance kit, serves the same contract over MCP,
and ships docs that CI keeps honest. The next step is the next release, which changes that
contract without breaking the agents that learned it: [Change the contract
safely](stability.md).
