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
    effect: str
    service: str
    release: str

deploy = app.group("deploy", description="Manage deployments")

@deploy.command("rollback", description="Roll a service back to its previous release",
                danger_level="destructive", exit_codes=["CONFLICT"])
def rollback(args: Rollback, ctx: Ctx) -> Plan:
    if args.to is None:
        raise Exit.CONFLICT("No previous release recorded", context={"service": args.service})
    effect = "would_update" if args.dry_run else "updated"
    return Plan(effect=effect, service=args.service, release=args.to)

if __name__ == "__main__":
    app.main()
```

Design decisions: zero runtime dependencies in core, handlers are plain functions
over a frozen dataclass of arguments, and every command lives in one flat registry
keyed by dot-path.

## Install

treaty is not on PyPI yet; install from a checkout. Both commands are non-interactive and
safe to repeat:

```bash
uv tool install --reinstall /path/to/treaty   # the treaty CLI on PATH
uv add --editable /path/to/treaty             # the library, inside a uv project
treaty --version                               # verify: prints a JSON envelope, exits 0
```

## Built-ins

Every app gets `manifest`, `version`, and `exec` (disable with `App(..., enable_exec=False)`).
`<app> --version` at the root is an alias for `<app> version`; a command's own `--version`
flag is never shadowed.
`exec` reads one `DispatchRequest` per stdin line and dispatches in-process, writing one
envelope per line with `_cmd` and `_line` in `meta`. A stream with no lines exits `2` with a
single `EMPTY_STREAM` envelope, a piped plan over 64 KiB (`App(max_stdin_bytes=...)` or
`TREATY_MAX_STDIN_BYTES`) exits `2` with `STDIN_TOO_LARGE` before anything runs, and
`--input-file PATH` reads a plan of any size from a file (`-` is stdin, capped). A terminal on
stdin exits `2` with `STDIN_IS_TTY` instead of waiting for input:

```bash
printf '%s\n' '{"_cmd":"deploy.rollback","service":"api","_opts":{"to":"1.3.9"}}' \
  | deployctl exec --ignore-errors --dry-run
```

## Flag order

`--format`, `--help`, `--schema`, and `--max-output` are global: they are accepted anywhere
before `--`, so a command cannot declare a flag with those names. Every other flag, including
`--timeout`, `--confirm-destructive`, `--idempotency-key`, and `--raw-payload`, belongs to a
command and goes after the full command path:

```bash
deployctl --format json deploy rollback api --dry-run   # ok
deployctl deploy rollback api --dry-run --format json   # ok
deployctl --dry-run deploy rollback api                 # ARG_ERROR
```

A command flag placed before the path fails with `ARG_ERROR`, names the command the remaining
words resolve to in `context.command`, and puts the corrected order in `suggestion`. Human
mode prints every error's suggestion as a final `hint:` line on stderr.

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

## Secrets

A field declared `Flag(secret=True)` or `Arg(secret=True)`, or whose name contains `token`,
`secret`, `password`, `key`, `credential`, or `auth`, never has its value echoed: validation
errors show `"value": "[REDACTED]"` in JSON and human mode alike. Pass `secret=False` to opt a
name like `author` back out. An unrecognized `--name=value` token is reported as `--name`,
since the framework cannot know whether the value was a secret.

## Paths

A field annotated `pathlib.Path` (or `Path | None`, `tuple[Path, ...]`) reaches the handler as a
`Path` and is listed in the manifest with `pattern_type: "filepath"`. Before any handler runs,
on argv, `exec`, and `--raw-payload` alike, the framework rejects the agent hallucination
patterns of REQ-F-045 with exit `2`: any `..` segment, a percent-encoded sequence such as
`%2e%2e` or `%2f`, and null bytes. The error carries `rejected_pattern` in `context` and a
`suggestion` with the decoded or absolute form, so `../out.json` is refused but
`/abs/out.json` passes unchanged. `pattern=` is not allowed on `Path` fields. The audit rule
`path-typed` warns about `str` fields whose name looks like a path.

## Destructive commands

A command with `danger_level="destructive"` must declare a boolean `dry_run` field. Without
`--confirm-destructive` the framework runs it in dry-run mode and exits `2` with error code
`CONFIRMATION_REQUIRED`, so the `data` payload shows what would be affected without applying it.

## Effects and idempotency keys

Mutating and destructive commands return an object with an `effect` field: `created`,
`updated`, `deleted`, or `noop` on a live run, and a `would_*` value such as `would_delete`
on a dry run. Registration fails when the output type cannot carry the field, and a run
that reports the wrong kind of value exits `1` with `INVALID_EFFECT`.

The framework gives those commands `--idempotency-key` (also `idempotency_key` in `exec`
lines and `--raw-payload`). A successful live run is stored under the key; repeating the
call returns the stored `data` with `effect: "noop"` and `meta.idempotency_hit: true`
without running the handler, and reusing the key with different arguments exits `6` with
`IDEMPOTENCY_KEY_REUSED`. Failures and dry runs are never stored, a concurrent retry waits
for the first call, and records expire after 24 hours. Records live in
`App(state_dir=...)`, else `$TREATY_STATE_DIR/<app>`, else `$XDG_STATE_HOME/treaty/<app>`,
else `~/.local/state/treaty/<app>`. Handlers read the key as `ctx.idempotency_key` to pass
it on to an upstream API.

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
