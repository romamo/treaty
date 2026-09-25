# treaty

Zero-dependency Python CLI framework that implements the
[CLI Agent Spec](../cli-agent-ergonomics): a manifest an agent can read in one call,
a response envelope on every exit, and typed exit codes with retry semantics.

The manifest is the treaty between the CLI author and the agent. Exit code
entries are its clauses.

```python
from dataclasses import dataclass
from treaty import App, Arg, Ctx, Exit, Flag

app = App("deployctl", version="1.4.0")
app.exit_code("CONFLICT", 79, description="Target already has a deployment in progress",
              retryable=False, side_effects="none")

@dataclass(frozen=True, slots=True)
class Rollback:
    service: str = Arg(description="Service name")
    to: str | None = Flag(default=None, description="Release tag to roll back to")
    dry_run: bool = Flag(default=False, description="Plan the rollback, write nothing")

@dataclass(frozen=True, slots=True)
class Plan:
    service: str
    release: str

deploy = app.group("deploy", description="Manage deployments")

@deploy.command("rollback", description="Roll a service back to its previous release",
                danger_level="destructive", exit_codes=["CONFLICT"])
def rollback(args: Rollback, ctx: Ctx) -> Plan:
    if args.to is None:
        raise Exit.CONFLICT("No previous release recorded", context={"service": args.service})
    return Plan(service=args.service, release=args.to)

if __name__ == "__main__":
    app.main()
```

Design decisions: zero runtime dependencies in core, handlers are plain functions
over a frozen dataclass of arguments, and every command lives in one flat registry
keyed by dot-path.

## Built-ins

Every app gets `manifest`, `version`, and `exec` (disable with `App(..., enable_exec=False)`).
`<app> --version` at the root is an alias for `<app> version`; a command's own `--version`
flag is never shadowed.
`exec` reads one `DispatchRequest` per stdin line and dispatches in-process, writing one
envelope per line with `_cmd` and `_line` in `meta`. A stream with no lines exits `2` with a
single `EMPTY_STREAM` envelope, and a terminal on stdin exits `2` with `STDIN_IS_TTY` instead of
waiting for input:

```bash
printf '%s\n' '{"_cmd":"deploy.rollback","service":"api","_opts":{"to":"1.3.9"}}' \
  | deployctl exec --ignore-errors --dry-run
```

## Timeouts

Every handler runs under a wall-clock limit: `App(default_timeout=60)` app-wide,
`@app.command(..., timeout=5)` per command, and `--timeout` on any command declaring
`has_network_io=True` (`--timeout 0` disables it). On expiry the framework writes a
`TIMEOUT` envelope, exits `10`, and records `meta.timeout_ms` on every response. Handlers
read `ctx.timeout` to pass the same deadline to their network calls.

## Output size

JSON output is capped at 1 MiB per envelope: `App(max_output_bytes=...)` app-wide,
`TREATY_MAX_OUTPUT_BYTES` in the environment, or the global `--max-output` flag, in
increasing precedence. Past the cap the framework follows whichever child holds most of the
bytes and cuts the list, object, or string where no child dominates to the longest prefix
that fits. `meta` gets `truncated`, `total_bytes`, and a `truncation_hint` giving the cap
that returns everything (plus `total_count` and `returned_count` when `data` is a list), and
each cut adds a `FIELD_TRUNCATED` warning naming the field. Human mode is not capped.
`--format`, `--help`, `--schema`, and `--max-output` are global, so a command cannot
declare a flag with those names.

## Destructive commands

A command with `danger_level="destructive"` must declare a boolean `dry_run` field. Without
`--confirm-destructive` the framework runs it in dry-run mode and exits `2` with error code
`CONFIRMATION_REQUIRED`, so the `data` payload shows what would be affected without applying it.

## Cancellation

SIGINT and SIGTERM produce a `CANCELLED` envelope with exit `130` or `143`, run the
command's optional `cleanup=` hook first, and a second signal during cleanup exits at once
without a second write. Both codes appear in every command's `exit_codes` map.

## Raw payloads

Declare `supports_raw_payload=True` and the command accepts `--raw-payload '{"name": "x"}'`
as an alternative to individual flags. The payload is checked against the same field types as
`exec` lines, and mixing it with individual flags exits `2`.

## Schemas

`tool <cmd> --schema` prints the command's manifest entry plus `parameters` and a draft-07
`output_schema` derived from the handler's return annotation. Commands with
`supports_raw_payload=True` also get `raw_payload_schema`, the JSON Schema of their args
dataclass. `tool --schema` prints the whole manifest in that form; `tool <group> --schema`
prints one group's subtree. The output is JSON in every mode.

## Conformance

`conformance/deployctl.json` is a profile for the spec's deterministic kit. With the spec checked
out as a sibling directory:

```bash
uv run --project ../cli-agent-ergonomics ../cli-agent-ergonomics/conformance/run.py conformance/deployctl.json
```

The example CLI passes all eleven checks across levels 1 to 3. The same run is a pytest test
that skips when the spec checkout is absent.

## Start a project

```bash
uv run treaty init shop-tool          # add --treaty-source ../treaty until treaty is on PyPI
cd shop-tool && uv sync && uv run pytest
uv run treaty conformance shop_tool.cli:app --run
```

`init` scaffolds a package with one command per danger level, typed outputs, declared exit
codes, a test using `app.run()`, and a conformance profile. `conformance` derives probes
from each command's first example and danger level, writes the profile, and with `--run`
executes the spec kit, exiting with `CONFORMANCE_FAILED` when checks fail. The kit is found
via `--spec-dir`, then `TREATY_SPEC_DIR`, then a sibling `cli-agent-ergonomics` checkout; a
named location without `conformance/run.py` exits `4` instead of falling through. `--out` and
`--directory` reject `..` segments, percent-encodings, and null bytes; pass an absolute path
to write outside the working directory.

## Audit your CLI

```bash
uv run treaty audit myapp.cli:app
```

Nine ordered rules check the registry and print the next steps with a fix using your own
names: missing examples, danger levels that contradict command names, mutating commands
without their own exit codes, retryable codes on non-idempotent commands, untyped outputs,
undeclared network I/O, wide mutating commands without `--raw-payload`, missing cleanup
hooks, and a missing conformance profile. `--all` lists everything, `--strict` exits 79 (`AUDIT_FAILED`) on any warning so
CI can gate on it, and piping the output gives an envelope an agent can act on. Rules see declarations only; the conformance kit
covers runtime behaviour.

## Development

```bash
uv sync
uv run pytest
uv run mypy src
uv run ruff check src tests
```

The manifest test validates against the spec schemas in the sibling
`cli-agent-ergonomics` checkout; set `TREATY_SPEC_DIR` to point elsewhere.
