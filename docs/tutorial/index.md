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

## The example files

Every chapter works on one small CLI, `todo`, and each file under `examples/tutorial/` is
`todo` at one point in the tutorial. The files do not form a single line: after
`todo_exit_codes.py`, two chapters each add one command to their own copy, so
`todo_network.py` has `import` and `todo_payload.py` has `edit`, but neither has the other's.

| File | What it is | Chapters |
| --- | --- | --- |
| [`todo_argparse.py`](../../examples/tutorial/todo_argparse.py) | the starting point: a typical argparse CLI | [argparse](B-migrate/argparse.md), and the failing run in [conformance](ship/conformance.md) |
| [`todo_click.py`](../../examples/tutorial/todo_click.py), [`todo_typer.py`](../../examples/tutorial/todo_typer.py) | the same CLI in click and in typer | [click or typer](B-migrate/click-typer.md) |
| [`todo_treaty.py`](../../examples/tutorial/todo_treaty.py) | `todo` on treaty, where every starting chapter ends | [new CLI](A-new/start.md), [argparse](B-migrate/argparse.md), [click or typer](B-migrate/click-typer.md) |
| [`todo_exit_codes.py`](../../examples/tutorial/todo_exit_codes.py) | plus declared exit codes; the version most chapters use | [exit codes](core/exit-codes.md), the other core chapters, and every ship chapter |
| [`todo_network.py`](../../examples/tutorial/todo_network.py) | plus `import`, which fetches items over HTTP | [network](core/network-io.md), [cleanup](core/cleanup.md) |
| [`todo_payload.py`](../../examples/tutorial/todo_payload.py) | plus `edit`, which takes its fields as JSON | [raw payload](core/raw-payload.md) |
| [`new_cli/test_cli.py`](../../examples/tutorial/new_cli/test_cli.py) | the tests a new project writes for `todo` | [new CLI](A-new/start.md) |
| [`conformance/`](../../examples/tutorial/conformance/) | the profile and the launchers the conformance kit runs | [conformance](ship/conformance.md) |

[Release what a run holds](core/cleanup.md) also uses [`examples/slowctl.py`](../../examples/slowctl.py),
the repository's demo of timeouts and cancellation, since `todo` holds nothing to release.
A chapter that changes `todo` names the file it starts from and the one it ends at.

## After the first chapter: follow the audit

`treaty audit module:app` checks your commands against every rule `treaty rules` lists, in
that order, and prints the first things to fix, with a suggested fix that uses your own
names. The core chapters follow the rules below, a subset in the same order, so when the
audit's first finding names one of them, its chapter is the one to read next. For any other
rule, the finding's suggested fix is the guide:

| Audit rule | What it asks for | Chapter |
| --- | --- | --- |
| `describe` | an example invocation on every command | [Describe every command](core/describe.md) |
| `danger-level` | danger levels that match what command names imply | [Choose each command's danger level](core/danger-level.md) |
| `exit-codes` | command-specific exit codes on every non-safe command | [Declare exit codes](core/exit-codes.md) |
| `retryable` | retryable codes only on idempotent commands | [Declare exit codes](core/exit-codes.md) |
| `typed-output` | typed return values, so `output_schema` is informative | [Type every command's output](core/typed-output.md) |
| `network-io` | `has_network_io=True` on commands that call out | [Declare network commands](core/network-io.md) |
| `path-typed` | `pathlib.Path` on path-like fields | [Type path arguments as Path](core/path-typed.md) |
| `raw-payload` | `--raw-payload` on wide mutating commands | [Accept a raw JSON payload](core/raw-payload.md) |
| `cleanup` | a cleanup hook on network commands | [Release what a run holds](core/cleanup.md) |
| `profile` | a conformance profile for the spec kit | [Run the conformance kit](ship/conformance.md) |

### Advice you can leave

Findings come in three severities. An `error` or a `warning` fails `--strict`; `advice` does
not. Advice comes from heuristics that guess from names, so read each one and decide: apply
the fix, or leave it when the guess does not fit your command. `todo` keeps two, and says why:

- **`multiline-flag` on `add`**: the rule sees a field named `text` and suggests allowing
  line breaks. `todo` keeps each item on one line on purpose, since `list` prints one item
  per line, so refusing a newline is the behaviour it wants
- **`already-exists` on `add`**: the rule sees a create command and suggests answering a
  repeated create with the item that is already there. That fits a create where the caller
  names the resource, such as `create widget`. `todo add` assigns the id itself and allows
  two items with the same text, so there is no existing item to answer with; a retried
  `add` is made safe with `--idempotency-key`, as
  [Choose each command's danger level](core/danger-level.md#step-3-what-mutating-adds) shows

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
