# Design guide

`treaty audit` checks the mechanics: every command declares its danger level and exit
codes, every mutation has an effect, every list paginates. It cannot tell whether the
names are good, whether an error helps, or whether one command should be two. This page
covers those judgement calls. It is short on purpose; the README covers the API.

## Naming command paths

- Noun, then verb: `deploy start`, `deploy rollback`, `config set`. An agent that found
  `deploy list` guesses `deploy show` correctly; one that found `list-deploys` guesses
  nothing
- One verb per meaning across the whole tool. Pick `show` or `get`, `remove` or `delete`,
  and use it everywhere; the manifest makes the inconsistency visible to every agent at once
- Keep paths shallow. Two levels cover almost every tool; a third level usually means a
  group is doing two jobs
- Name flags after what they hold, not how they are used: `--region`, not `--use-region`.
  A boolean reads as a claim: `--force`, `--all`, `--dry-run`
- Leave the built-in names alone (`doctor`, `status`, `cleanup`, and the rest):
  an app command of the same name wins, but agents that know the built-in are surprised.
  `treaty audit` reports each shadowed one
- A rename after release is `App.redirect("old", to="new")`, never a silent removal

## What belongs in `error.context`

`error.context` is for the machine: the values an agent needs to act on the failure
without parsing `message`.

- The identifiers involved: `{"deployment": "d-42", "region": "eu-west-1"}`. An agent
  retries or looks up by them
- The limit that was hit and the value that hit it: `{"max": 100, "got": 250}`
- The state that blocked the call: `{"status": "running"}` for a deployment that cannot be
  deleted while it runs

Leave out what is elsewhere or unsafe: the whole input (the agent has it), stack traces
(they go to stderr), secrets (treaty redacts known ones, but not a token you put under
a name it cannot recognize), and anything large. The next step belongs in `suggestion`
or `fix_command`, not in `context`.

## When a failure deserves its own exit code

The framework codes (0 to 13) already say what kind of failure it was: not found,
conflict, precondition, timeout. Most failures fit one of them, with the detail in
`error.code` and `error.context`.

Declare a command-specific code (79 to 125, `app.exit_code`) only when an agent should
take a different branch without reading the envelope, for example in a shell script:

- A different recovery: `QUOTA_EXCEEDED` waits for a reset, while `PRECONDITION` asks for
  a change. If the right reaction is the same, share the code
- A distinct, expected outcome that is not really an error to the caller: a health check
  that found the service degraded, a diff that found differences
- A code a documented workflow already branches on

A failure the agent cannot act on differently is `GENERAL_ERROR` with a clear `error.code`.
Once released, a code is part of the contract: renumbering it breaks every script.

## When to split a command

Split when one command would need a flag that changes what kind of thing it does:

- Different danger levels: `config show` (safe) and `config set` (mutating), never
  `config --set`. The danger level, confirmation, and `effect` are per command
- Different output shapes: a flag that swaps `data` from a list to a single object makes
  the output schema a union no agent can rely on. `deploy list` and `deploy show ID`
- Different exit codes that only some modes can reach

Keep one command when the flags only narrow or format the same result: `--limit`,
`--fields`, a filter, or `--format`. An agent can discover flags on one command more
easily than it can discover a family of near-identical commands.
