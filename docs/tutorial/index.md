# treaty tutorial

This tutorial takes a CLI, new or existing, to the point where an agent can use it without
reading its source: one call to `manifest` lists every command, flag, and exit code, every
run ends with a JSON envelope, and every failure has a typed exit code that says whether a
retry is safe.

It is written for two readers. A developer reads the prose; a coding agent follows the
**Check** commands, which pass or fail without a person looking at the output.

## Pick a track

| Track | You start from | First chapter |
| --- | --- | --- |
| A: New CLI | nothing | [Start a new CLI](A-new/start.md) |
| B: Migrate | an argparse CLI | [Migrate an argparse CLI](B-migrate/argparse.md) |
| B: Migrate | a click or typer CLI | [Migrate a click or typer CLI](B-migrate/click-typer.md) |

Both tracks end in the same place: a treaty app that `treaty audit` can inspect. From there
the core chapters take you through the audit's rules one at a time.

## Before you start

- Python 3.14 and [uv](https://docs.astral.sh/uv/)
- `uv add treaty` inside your project, and `uv tool install treaty` for the `treaty` command
- Verify with `treaty --version`: it prints a JSON envelope and exits 0

## How a chapter is laid out

Every chapter opens with the same three lines:

- **Goal:** what is true when you finish
- **You need:** the chapter or state it builds on
- **Done when:** the command that proves it, and what it prints

Inside, each step ends with a **Check**. Code in the chapters is taken from the runnable
files under `examples/tutorial/`, and `tests/test_tutorial.py` fails when the two drift
apart, so what you copy is what the tests run.

## After the first chapter: follow the audit

`treaty audit module:app` checks your commands against every rule `treaty rules` lists, in
that order, and prints the first things to fix, with a suggested fix that uses your own
names. The core chapters follow the rules below, a subset in the same order, so when the
audit's first finding names one of them, its chapter is the one to read next. For any other
rule, the finding's suggested fix is the guide:

| Audit rule | What it asks for | Chapter |
| --- | --- | --- |
| `describe` | an example invocation on every command | [Describe every command](core/describe.md) |
| `danger-level` | danger levels that match what command names imply | not written yet |
| `exit-codes` | command-specific exit codes on every non-safe command | [Declare exit codes](core/exit-codes.md) |
| `retryable` | retryable codes only on idempotent commands | [Declare exit codes](core/exit-codes.md) |
| `typed-output` | typed return values, so `output_schema` is informative | not written yet |
| `network-io` | `has_network_io=True` on commands that call out | not written yet |
| `path-typed` | `pathlib.Path` on path-like fields | not written yet |
| `raw-payload` | `--raw-payload` on wide mutating commands | not written yet |
| `cleanup` | a cleanup hook on network commands | not written yet |
| `profile` | a conformance profile for the spec kit | [Run the conformance kit](ship/conformance.md) |

You are done with the core when `treaty audit module:app --strict` exits 0. Shipping comes
after that: [Run the conformance kit](ship/conformance.md) puts the CLI through the spec's
runtime checks and gates CI on them, [Serve commands over MCP](ship/mcp.md) gives agents
without a shell the same commands as tools, and [Ship the agent docs](ship/agent-docs.md)
generates the AGENTS.md, skill files, and MCP tool list agents read, and checks them in CI.

## For agents

The whole tutorial reduces to one loop. Run it from the project root:

```bash
uv run treaty audit myapp.cli:app --strict          # exit 0: done
uv run treaty audit myapp.cli:app | jq '.data.next_steps[0]'
# read .rule, open that chapter, apply .fix, run the project's tests, repeat
```

Rules only see declarations. After the loop ends, `treaty conformance myapp.cli:app --run`
checks runtime behaviour against the spec kit.
