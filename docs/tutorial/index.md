# treaty tutorial

This tutorial takes a CLI, new or existing, to the point where an agent can use it without
reading its source: one call to `manifest` lists every command, flag, and exit code, every
run ends with a JSON envelope, and every failure has a typed exit code that says whether a
retry is safe.

Every chapter works on one small example CLI, `todo`, and shows each change twice: in prose,
with the commands to run in your own project, and in **Check** blocks that re-run the change
against the tutorial's own copy of `todo` and pass or fail without a person reading the
output.

## Pick a track

| Track | You start from | First chapter |
| --- | --- | --- |
| A: New CLI | nothing | [Start a new CLI](A-new/start.md) |
| B: Migrate | an argparse CLI | [Migrate an argparse CLI](B-migrate/argparse.md) |
| B: Migrate | a click or typer CLI | [Migrate a click or typer CLI](B-migrate/click-typer.md) |

Both tracks end in the same place: a treaty app that `treaty audit` can inspect. From there
the core chapters take you through the audit's rules one at a time.

### The shortest path

The whole tutorial is long; these four steps are the ones every CLI needs, and the rest can
wait until the audit names them:

1. Your starting chapter from the table above
2. [Declare exit codes](core/exit-codes.md), so an agent can tell failures apart
3. [Run the conformance kit](ship/conformance.md), which checks the binary as agents call it
4. [Ship the agent docs](ship/agent-docs.md), so agents learn the CLI from files that stay
   true

## Before you start

- Python 3.14 and [uv](https://docs.astral.sh/uv/)
- For a new CLI, nothing else: `uvx treaty init` makes a project that depends on treaty. To
  migrate a CLI, run `uv add treaty` in its project
- Read [The response envelope](envelope.md) once: every chapter reads its keys, and it
  defines the terms the chapters use

## Your project, or the tutorial's checks

Each chapter serves two ways of reading it:

- **Building your own CLI.** Work in your project and run the commands the prose shows, with
  your app's import path, such as `myapp.cli:app`, and your own command in place of `todo`.
  Each **Check** shows what the same step prints for `todo`, so you know what to expect
- **Re-running the tutorial.** The **Check** blocks run from the root of a treaty checkout,
  where the example files live. Each chapter's first block defines `todo` as a shell function
  that runs that chapter's example file, and `tests/test_tutorial.py` runs every block in
  order, so the tutorial fails its own tests when it stops being true

## How a chapter is laid out

Every chapter opens with the same three lines:

- **Goal:** what is true when you finish
- **You need:** the chapter or state it builds on
- **Done when:** the command that proves it, and what it prints

Inside, each step ends with a **Check**. The code shown is an excerpt of a runnable file
under `examples/tutorial/`, named at the top of the chapter: open it for the imports and the
definitions around an excerpt. `tests/test_tutorial.py` fails when an excerpt and its file
drift apart, so what you copy is what the tests run.

## The example files

Each file under `examples/tutorial/` is `todo` at one point in the tutorial. After
`todo_exit_codes.py`, each chapter changes its own copy, so the later files do not have each
other's commands:

| File | What it is | Chapters |
| --- | --- | --- |
| [`todo_argparse.py`](../../examples/tutorial/todo_argparse.py) | the starting point: a typical argparse CLI | [argparse](B-migrate/argparse.md), and the failing run in [conformance](ship/conformance.md) |
| [`todo_click.py`](../../examples/tutorial/todo_click.py), [`todo_typer.py`](../../examples/tutorial/todo_typer.py) | the same CLI in click and in typer | [click or typer](B-migrate/click-typer.md) |
| [`todo_treaty.py`](../../examples/tutorial/todo_treaty.py) | `todo` on treaty, where every starting chapter ends | [new CLI](A-new/start.md), [argparse](B-migrate/argparse.md), [click or typer](B-migrate/click-typer.md) |
| [`todo_exit_codes.py`](../../examples/tutorial/todo_exit_codes.py) | plus declared exit codes; the version most chapters use | [exit codes](core/exit-codes.md), the other core chapters, and every ship chapter |
| [`todo_network.py`](../../examples/tutorial/todo_network.py) | plus `import`, which fetches items over HTTP | [network](core/network-io.md), [cleanup](core/cleanup.md) |
| [`todo_payload.py`](../../examples/tutorial/todo_payload.py) | plus `edit`, which takes its fields as JSON | [raw payload](core/raw-payload.md) |
| [`todo_config.py`](../../examples/tutorial/todo_config.py) | `todo_network.py` plus settings and a token for `import` | [settings and secrets](core/config.md) |
| [`todo_pages.py`](../../examples/tutorial/todo_pages.py) | a `list` that pages by item id | [pagination](core/pagination.md) |
| [`todo_batch.py`](../../examples/tutorial/todo_batch.py) | `todo_network.py` plus `import-all`, several feeds in one call | [long-running work](core/long-running.md) |
| [`todo_git.py`](../../examples/tutorial/todo_git.py) | plus `save`, which commits the item file with git | [other programs](core/programs.md) |
| [`todo_v2.py`](../../examples/tutorial/todo_v2.py) | release 1.1.0: `done` renamed `complete`, `--all` deprecated | [stability](ship/stability.md) |
| [`new_cli/test_cli.py`](../../examples/tutorial/new_cli/test_cli.py) | the tests a new project writes for `todo` | [new CLI](A-new/start.md), [testing](ship/testing.md) |
| [`new_cli/test_contract.py`](../../examples/tutorial/new_cli/test_contract.py), [`new_cli/agent-contract.yml`](../../examples/tutorial/new_cli/agent-contract.yml) | contract tests for any treaty app, and a CI job with every gate | [testing](ship/testing.md) |
| [`conformance/`](../../examples/tutorial/conformance/) | the profile and the launchers the conformance kit runs | [conformance](ship/conformance.md) |

[Release what a run holds](core/cleanup.md) also uses
[`examples/slowctl.py`](../../examples/slowctl.py), the repository's demo of timeouts and
cancellation, since `todo` holds nothing to release. A chapter that changes `todo` names the
file it starts from and the one it ends at.

## After the first chapter: follow the audit

`treaty audit module:app` checks your commands against every rule `treaty rules` lists, and
prints the first things to fix, each with a suggested fix that uses your own names.

The chapters follow the audit's rules in the audit's order. When the audit names one of the
rules below, open its chapter. For any other rule, follow the finding's suggested fix:

| Audit rule | What it asks for | Chapter |
| --- | --- | --- |
| `describe` | an example invocation on every command | [Describe every command](core/describe.md) |
| `danger-level` | danger levels that match what command names imply | [Choose each command's danger level](core/danger-level.md) |
| `exit-codes` | command-specific exit codes on every non-safe command | [Declare exit codes](core/exit-codes.md) |
| `retryable` | retryable codes only on idempotent commands | [Declare exit codes](core/exit-codes.md) |
| `typed-output` | typed return values, so `output_schema` is informative | [Type every command's output](core/typed-output.md) |
| `paginated-list` | list commands keep the framework's pagination | [Page long lists](core/pagination.md) |
| `network-io` | `has_network_io=True` on commands that call out | [Declare network commands](core/network-io.md) |
| `subprocess-declared` | a declared argument list for each program a command runs | [Run other programs](core/programs.md) |
| `required-tools` | every program a command runs listed for `doctor` | [Run other programs](core/programs.md) |
| `preserve-locale` | children run in the C locale, or say why not | [Run other programs](core/programs.md) |
| `path-typed` | `pathlib.Path` on path-like fields | [Type path arguments as Path](core/path-typed.md) |
| `raw-payload` | `--raw-payload` on wide mutating commands | [Accept a raw JSON payload](core/raw-payload.md) |
| `cleanup` | a cleanup hook on network commands | [Release what a run holds](core/cleanup.md) |
| `async-job` | a job descriptor from commands that start work | [Run long work an agent can follow](core/long-running.md) |
| `stable-order` | a declared order for arrays of objects | [Page long lists](core/pagination.md) |
| `external-data` | content from outside the tool marked untrusted | [Declare network commands](core/network-io.md#step-5-mark-what-came-from-outside) |
| `schema-version` | an output change that bumps the command's `schema_version` | [Change the contract safely](ship/stability.md) |
| `log-not-print` | handlers log through `ctx`, never `print()` | [Log without touching stdout](core/logging.md) |
| `settings-declared` | config read through `App(settings=)`, not parsed by a handler | [Read settings and secrets](core/config.md) |
| `env-prefix` | handlers read only the app's own environment variables | [Read settings and secrets](core/config.md) |
| `profile` | a conformance profile for the spec kit | [Run the conformance kit](ship/conformance.md) |
| `additive` | nothing in the last release's manifest removed without notice (`--baseline`) | [Change the contract safely](ship/stability.md) |

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

You are done with the core when `treaty audit module:app --strict` exits 0. The shipping
chapters come after that:

- [Run the conformance kit](ship/conformance.md) runs the spec's runtime checks on the
  binary and gates CI on them
- [Serve commands over MCP](ship/mcp.md) gives agents without a shell the same commands as
  tools
- [Ship the agent docs](ship/agent-docs.md) generates the AGENTS.md, skill files, and MCP tool
  list agents read, and checks them in CI
- [Change the contract safely](ship/stability.md) takes a release to the next without
  breaking the agents that learned it
- [Test the contract and gate CI](ship/testing.md) puts every check into one test suite and
  one CI job

## For agents

The whole tutorial reduces to one loop. Run it from the project root:

```bash
uv run treaty audit myapp.cli:app --strict          # exit 0: done
uv run treaty audit myapp.cli:app | jq '.data.next_steps[0]'
# read .rule, open that chapter, apply .fix, run the project's tests, repeat
```

`next_steps` holds the first few findings, errors first, then warnings, then advice, so
what fails `--strict` is always at the top. `--all` lists every finding, and
`.data.rules[].findings` holds them all in JSON; a check that one rule is clear reads that,
since `next_steps` may stop before that rule's findings.

Rules only see declarations. After the loop ends, `treaty conformance myapp.cli:app --run`
checks runtime behaviour against the spec kit.
