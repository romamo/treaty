# treaty

[![PyPI](https://img.shields.io/pypi/v/treaty?include_prereleases&label=PyPI)](https://pypi.org/project/treaty/)
[![Python](https://img.shields.io/badge/python-3.14-blue)](https://pypi.org/project/treaty/)
[![CI](https://github.com/romamo/treaty/actions/workflows/ci.yml/badge.svg)](https://github.com/romamo/treaty/actions/workflows/ci.yml)
[![Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)](https://github.com/romamo/treaty/blob/main/pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/romamo/treaty/blob/main/LICENSE)

**A Python CLI framework for command-line tools that AI agents can call safely.**

treaty implements the [CLI Agent Spec](https://github.com/cli-agent-spec/cli-agent-spec):
an agent reads every command, flag, and exit code in one `manifest` call, gets a JSON
envelope on every exit, and knows from the exit code alone whether a retry is safe. The
manifest is the treaty between the CLI author and the agent; exit codes are its clauses.

## Why treaty

Agents drive CLIs by trial and error: they parse `--help`, guess flags, scrape stderr, and
retry commands that already half-ran. treaty makes the contract explicit and checks it:

- **One-call discovery:** `manifest` returns every command, flag, example, and exit code as JSON
- **An envelope on every exit:** `ok`, `data`, `error`, `warnings`, `meta`, even when a handler crashes
- **Typed exit codes:** each one says whether it is retryable and what side effects it left; a
  handler that raises an undeclared code fails loudly
- **Declared danger:** every command is `safe`, `mutating`, or `destructive`; destructive ones
  refuse without `--confirm-destructive` and show a dry-run preview instead
- **Safe retries:** idempotency keys and session deduplication for mutating commands
- **Strict validation:** every argument error in one run, exit `2`, and the handler never called
- **Secrets stay secret:** read only from env or file, redacted in output and logs
- **MCP for free:** `treaty-mcp module:app` serves any treaty app's commands as MCP tools
- **Zero runtime dependencies** in the core

## Install

```bash
uv add treaty                # the library, inside a uv project
uv tool install treaty       # the treaty CLI on PATH
```

Requires Python 3.14. Add the `mcp` extra (`treaty[mcp]`) for the MCP server.

## Quick start

Handlers are plain functions over a frozen dataclass of arguments:

```python
from dataclasses import dataclass
from treaty import Affects, App, Arg, Ctx, Exit, Flag

app = App("deployctl", version="1.4.0")
app.exit_code("DEPLOY_CONFLICT", 79, description="Target already has a deployment in progress",
              retryable=False, side_effects="none")

@dataclass(frozen=True, slots=True)
class Rollback:
    service: str = Arg(description="Service name")
    to: str | None = Flag(default=None, description="Release tag to roll back to")
    dry_run: bool = Flag(default=False, description="Plan the rollback, write nothing")

@dataclass(frozen=True, slots=True)
class Plan:
    effect: str
    service: str
    release: str
    would_affect: Affects | None = None

deploy = app.group("deploy", description="Manage deployments")

@deploy.command("rollback", description="Roll a service back to its previous release",
                danger_level="destructive", exit_codes=["DEPLOY_CONFLICT"])
def rollback(args: Rollback, ctx: Ctx) -> Plan:
    if args.to is None:
        raise Exit.DEPLOY_CONFLICT("No previous release recorded", context={"service": args.service})
    if args.dry_run:
        affects = Affects(f"Rolls {args.service} back to {args.to}", (f"service/{args.service}",), 1)
        return Plan("would_update", args.service, args.to, affects)
    return Plan("updated", args.service, args.to)

if __name__ == "__main__":
    app.main()
```

## What the agent sees

The manifest entry for `deploy rollback` declares its danger level and every exit code it
can return:

```json
{
  "danger_level": "destructive",
  "exit_codes": {
    "6":  {"name": "CONFLICT", "retryable": false, "side_effects": "none"},
    "79": {"name": "DEPLOY_CONFLICT", "retryable": false, "side_effects": "none",
           "description": "Target already has a deployment in progress"}
  }
}
```

`deployctl deploy rollback api --to v1.3.0` without confirmation changes nothing. The
agent gets exit `2`, a preview of what would happen, and the exact fix (`meta` trimmed):

```json
{
  "ok": false,
  "data": {
    "effect": "would_update",
    "service": "api",
    "release": "v1.3.0",
    "would_affect": {"summary": "Rolls api back to v1.3.0", "resources": ["service/api"], "count": 1}
  },
  "error": {
    "code": "CONFIRMATION_REQUIRED",
    "message": "Command deploy.rollback is destructive and was not applied; it would: Rolls api back to v1.3.0",
    "phase": "validation",
    "retryable": false,
    "suggestion": "rerun with --confirm-destructive to apply (confirm_destructive: true in exec, MCP, or --raw-payload)"
  },
  "warnings": [],
  "meta": {"command": "deploy.rollback", "exit_code": 2, "dry_run": true, "request_id": "66bd15e2696b"}
}
```

Without `--to`, the handler raises `DEPLOY_CONFLICT`: exit `79`, `retryable: false`, and
`error.context` names the service.

Output is JSON automatically when stdout is not a terminal and human-readable text when it
is. Off a terminal nothing prompts: an answer a command needs fails with an exit code.

## Tooling

The `treaty` CLI is itself a treaty app:

| Command | What it does |
| --- | --- |
| `treaty init` | Scaffold a new CLI project that passes the audit |
| `treaty scaffold-from` | Write a treaty module from a typer, click, or argparse CLI |
| `treaty audit` | Check an app against the spec and print the next fix, in your own names |
| `treaty conformance` | Generate a conformance profile and run the spec's kit |
| `treaty agents-md` | Write `AGENTS.md` from the registry |
| `treaty generate-skills` | Write agent skill files from the command schemas |
| `treaty-mcp` | Serve any treaty app's commands as MCP tools over stdio |

Every app also gets built-ins: `manifest`, `version`, `exec`, `doctor`, `status`,
`cleanup`, `completion`, and more.

## Documentation

- [Tutorial](https://github.com/romamo/treaty/blob/main/docs/tutorial/index.md): take a CLI,
  new or migrated from argparse, click, or typer, through every audit rule to a release
- [Reference](https://github.com/romamo/treaty/blob/main/docs/reference.md): the full API,
  one section per concern
- [Design guide](https://github.com/romamo/treaty/blob/main/docs/guide.md): naming,
  error context, exit codes, and when to split a command
- [Public API](https://github.com/romamo/treaty/blob/main/docs/api.md): the surface frozen at 1.0
- [Spec compliance](https://github.com/romamo/treaty/blob/main/COMPLIANCE.md): the
  requirement-by-requirement score
- [Changelog](https://github.com/romamo/treaty/blob/main/CHANGELOG.md)

## Status

treaty is in release candidates for 1.0 and follows semantic versioning from 1.0; until
then a release may break an app, and the changelog's "Breaking" sections say where. treaty
1.0 claims CLI Agent Spec Level 2 conformance.

## Contributing

Issues and pull requests are welcome. See
[CONTRIBUTING.md](https://github.com/romamo/treaty/blob/main/CONTRIBUTING.md) for the
development setup and how changes are released, and
[SECURITY.md](https://github.com/romamo/treaty/blob/main/SECURITY.md) to report a
vulnerability. Everyone taking part follows the
[Code of Conduct](https://github.com/romamo/treaty/blob/main/CODE_OF_CONDUCT.md).

## License

[MIT](https://github.com/romamo/treaty/blob/main/LICENSE)
