# treaty

Zero-dependency Python CLI framework that implements the
[CLI Agent Spec](https://github.com/cli-agent-spec/cli-agent-spec): a manifest an agent can read in one call,
a response envelope on every exit, and typed exit codes with retry semantics.

The manifest is the treaty between the CLI author and the agent. Exit code
entries are its clauses.

```python
from dataclasses import dataclass
from treaty import App, Arg, Ctx, Exit, Flag

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

deploy = app.group("deploy", description="Manage deployments")

@deploy.command("rollback", description="Roll a service back to its previous release",
                danger_level="destructive", exit_codes=["DEPLOY_CONFLICT"])
def rollback(args: Rollback, ctx: Ctx) -> Plan:
    if args.to is None:
        raise Exit.DEPLOY_CONFLICT("No previous release recorded", context={"service": args.service})
    effect = "would_update" if args.dry_run else "updated"
    return Plan(effect=effect, service=args.service, release=args.to)

if __name__ == "__main__":
    app.main()
```

Design decisions: zero runtime dependencies in core, handlers are plain functions
over a frozen dataclass of arguments, and every command lives in one flat registry
keyed by dot-path.

A handler raises only the exit codes its manifest entry lists: the ones in `exit_codes=`,
plus `GENERAL_ERROR`, `ARG_ERROR`, and `TIMEOUT` everywhere and `CONFLICT` and
`PRECONDITION` on mutating commands. Anything else, including framework names such as
`Exit.NOT_FOUND`, must be declared, or the run exits `1` with `UNDECLARED_EXIT_CODE`.

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
before `--`, so a command cannot declare a flag with those names or the short `-h`. The
manifest lists them once, in its root `flags` map (ManifestResponse 3.0). Every other flag,
including `--timeout`, `--confirm-destructive`, `--idempotency-key`, and `--raw-payload`,
belongs to a command and goes after the full command path:

```bash
deployctl --format json deploy rollback api --to 1.3.9 --dry-run   # ok
deployctl deploy rollback api --to 1.3.9 --dry-run --format json   # ok
deployctl --dry-run deploy rollback api --to 1.3.9                 # ARG_ERROR
```

Any option repeated with a different value exits `2` naming the option; repeating the same
value is accepted, and array flags accumulate. A negative number such as `-5` is a value,
not a flag.

A command flag placed before the path fails with `ARG_ERROR`, names the command the remaining
words resolve to in `context.command`, and puts the corrected order in `suggestion`. Human
mode prints every error's suggestion as a final `hint:` line on stderr.

## Timeouts

Every handler runs under a wall-clock limit: `App(default_timeout=60)` app-wide,
`@app.command(..., timeout=5)` per command, and `--timeout` on any command declaring
`has_network_io=True` and on every streaming command (`--timeout 0` disables it; at most
one year). A stream buffered in-process (`App.call`, MCP) always has a deadline: the
caller's `timeout`, else the app default; `0` is refused there. On expiry the
framework writes a `TIMEOUT` envelope, exits `10`, and records `meta.timeout_ms` on every
response. Handlers read `ctx.timeout` to pass the same deadline to their network calls. An
idempotency key stays locked until a timed-out or cancelled handler really finishes, so a
retry never runs beside it: it waits up to its own timeout, then replays the recorded
result or exits `10` with `IDEMPOTENCY_KEY_BUSY`. An unusable state directory or a damaged
record exits `4` (`STATE_DIR_UNWRITABLE`, `IDEMPOTENCY_RECORD_CORRUPT`).

A handler that raises anything else exits `1` with `HANDLER_CRASHED`, naming the exception;
the traceback goes to stderr with secret values redacted. A result or `Exit` payload the
framework cannot serialize exits `1` with `INVALID_OUTPUT` or `INVALID_EXIT`.

## Output size

JSON output is capped at 1 MiB per envelope: `App(max_output_bytes=...)` app-wide,
`TREATY_MAX_OUTPUT_BYTES` in the environment, or the global `--max-output` flag, in
increasing precedence. Past the cap the framework follows whichever child holds most of the
bytes and cuts the list, object, or string where no child dominates to the longest prefix
that fits. `meta` gets `truncated`, `total_bytes`, and a `truncation_hint` giving the cap
that returns everything (plus `total_count` and `returned_count` when `data` is a list), and
each cut adds a `FIELD_TRUNCATED` warning naming the field. Human mode is not capped.

## Secrets

A field declared `Flag(secret=True)`, or whose name contains `token`, `secret`, `password`,
`key`, `credential`, or `auth`, never takes its value on the command line (REQ-C-016). The
framework exposes `--<name>-from-env VAR` and `--<name>-from-file PATH` instead
(REQ-O-022), and reads `<APP>_<NAME>` when neither is given; the manifest lists that default
in `secret_env_vars`. The value is read in the validation phase, coerced and pattern-checked
like any field, and handed to the handler as the field. A direct `--<name> VALUE`, a missing
variable, an unreadable or empty file, or a file path with `..` all exit `2` before anything
runs, and no error ever echoes the value: it shows as `"value": "[REDACTED]"`. Booleans are
never secrets; pass `secret=False` to opt a name like `author` out. A secret cannot be
positional, an array, or carry a short flag.

```bash
deployctl push --token-from-env DEPLOY_TOKEN      # reads $DEPLOY_TOKEN
deployctl push --token-from-file /run/secrets/tok  # reads the file, one trailing newline dropped
DEPLOYCTL_TOKEN=... deployctl push                 # the default variable
```

## Validation errors

Phase 1 keeps going past a bad value, an unknown flag, or a refused secret, so one run
reports every argument error (REQ-F-015). The envelope's `error.errors` lists each one with
its `field`, `message`, and `context`; with several, the headline `message` is
`Validation failed: N errors` and `context.fields` names them. A single error keeps its own
message and context and lists itself. Framework flags (`--timeout`, `--idempotency-key`,
a repeat with a different value) and a flag with no value at the end are collected the same
way; only invalid `--raw-payload` JSON stops parsing at once.

## Paths

A field annotated `pathlib.Path` (or `Path | None`, `tuple[Path, ...]`) reaches the handler as a
`Path` and is listed in the manifest with `pattern_type: "filepath"`. Before any handler runs,
on argv, `exec`, and `--raw-payload` alike, the framework rejects the agent hallucination
patterns of REQ-F-045 with exit `2`: any `..` segment, a percent-encoded sequence such as
`%2e%2e` or `%2f`, and null bytes. The error carries `rejected_pattern` in `context` and a
`suggestion` with the decoded or absolute form, so `../out.json` is refused but
`/abs/out.json` passes unchanged. `pattern=` is not allowed on `Path` fields. The audit rule
`path-typed` warns about `str` fields whose name looks like a path.

## Custom scalars

A domain class can annotate a field or an output attribute once the app knows how to parse
it. Register it before the commands that use it; the class itself never imports treaty:

```python
app.scalar(ResourceId, parse=ResourceId.from_boundary, pattern=r"[a-z][a-z0-9-]{0,62}")
app.scalar(TcpPort, parse=TcpPort, base=int, minimum=1, maximum=65535)
app.scalar(RunId, parse=RunId, pattern_type="uuid")
```

The value travels as its `base` (`str`, `int`, or `float`) on argv, in `exec` lines, and in
`--raw-payload`. The framework coerces the base type, checks `pattern`, `pattern_type`, or
the bounds, then calls `parse`; a `ValueError` or `TypeError` from it is one entry in
`error.errors` with the class name and the cause in `context`. The manifest lists the field
under its base type with `pattern` or `pattern_type`, and `--schema` carries the pattern,
`format`, and bounds on both `raw_payload_schema` and `output_schema`. Outputs serialize
back through `serialize`, which defaults to the class's `value` field (or `str` for a `str`
base). `pattern=` on a field of a registered type is a registration error, as is annotating
an unregistered class. `pattern_type` takes the REQ-C-020 presets `alphanumeric_id`,
`uuid`, `semver`, and `url`; `filepath` stays with `pathlib.Path`.

## Resources

A handler can take more parameters after `ctx`. Each one is annotated with a class that has
an `acquire` classmethod, and the framework calls it after validation, once per run, before
the handler:

```python
@dataclass(frozen=True, slots=True)
class Project:
    directory: Path

    @classmethod
    def acquire(cls, args: ProjectArgs, ctx: Ctx) -> Self:
        directory = args.project or Path(ctx.env["PWD"])
        if not (directory / "servers").is_dir():
            raise Exit.NO_PROJECT("not a project", context={"directory": str(directory)})
        return cls(directory)

@dataclass(frozen=True, slots=True)
class Config:
    @classmethod
    def acquire(cls, args: ProjectArgs, ctx: Ctx, project: Project) -> Self: ...

@app.command("deploy", description="Deploy a component", exit_codes=["NO_PROJECT"])
def deploy(args: DeployArgs, ctx: Ctx, config: Config, project: Project) -> Receipt: ...
```

`acquire` takes the same `(args, ctx)` as a handler plus, optionally, other resources by
annotation, so resources compose. Each class is acquired at most once per run and shared,
in dependency order, and acquisition runs under the command's timeout. A `CliExit` raised
inside `acquire` becomes that exit's envelope and a `ParseError` becomes `ARG_ERROR`, so a
missing project fails before any handler runs. A class without a classmethod `acquire`, a
missing `Ctx` annotation, or a dependency cycle is a registration error. Resources are not
part of the manifest: the flags they read, such as `--project`, live on the args dataclass,
typically a `kw_only=True` base class shared by every command. Resources must not change
process state such as the working directory, because `exec` runs many requests in one
process.

## Streaming

A command declared `streaming=True` has a generator handler annotated `Iterator[T]`, and
every yield is one JSONL envelope line with `meta.seq` counting from 1. The stream ends
with a terminal envelope that has `data: null`, `meta.end: true`, and `meta.total`, so an
agent can tell a clean end from a killed process:

```python
@app.command("dashboard.serve", description="Serve the dashboard", streaming=True,
             cleanup=stop_server)
def serve(args: ServeArgs, ctx: Ctx) -> Iterator[ServeEvent]:
    server = start(args.port)
    yield Listening(url=server.url)
    try:
        while True:
            yield server.next_event()
    finally:
        server.close()
```

A `CliExit`, `ParseError`, timeout, or signal after some events writes the matching failure
envelope as the last line, with `meta.seq` at the last delivered event and `meta.partial`.
Streaming commands default to no timeout; an explicit `timeout=` or `--timeout` is a
deadline for the whole stream. Cancellation runs `cleanup=` and the handler's `finally`
blocks, then ends the stream with the normal `CANCELLED` envelope and exit `130` or `143`.
The manifest declares `streaming_default: true` and a `--no-stream` flag (REQ-O-004),
which returns one envelope with every event in `data` and `meta.total`; a failure under
`--no-stream` keeps the events seen so far in `data`. In `exec`, each event line carries
`_line` and `_cmd`. Streaming commands must be `safe`: the effect and idempotency
contracts describe one response. In human mode `human=` renders each event.

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
without a second write. A handler's own `except Exception` cannot swallow the signal, and a
retry waiting for an idempotency key is interrupted too. A signal that arrives after the
handler returned is held: the finished result is written with its own exit code, since
the work it reports did happen. An `exec` plan stops at the first signal, even with
`--ignore-errors`, and exits `130` or `143`. A reader that closes stdout early
(`tool logs | head`) ends the run with `141` (`OUTPUT_CLOSED`): the cleanup hook runs and
nothing more is written. All three codes appear in every command's `exit_codes` map.

## Raw payloads

Declare `supports_raw_payload=True` and the command accepts `--raw-payload '{"name": "x"}'`
as an alternative to individual flags. The payload is checked against the same field types as
`exec` lines, and mixing it with individual flags exits `2`.

## Schemas

`tool <cmd> --schema` prints the command's manifest entry plus `parameters` and a draft-07
`output_schema` derived from the handler's return annotation. Commands with
`supports_raw_payload=True` also get `raw_payload_schema`, the JSON Schema of their args
dataclass. `tool --schema` prints the whole manifest with each command's full exit-code
table (a valid ManifestResponse, so without `parameters` or `raw_payload_schema`);
`tool <group> --schema` prints one group's subtree. The output is JSON in every mode.

## MCP

With the `mcp` extra installed, any treaty app serves its commands as MCP tools over
stdio, in-process, with nothing to write:

```bash
uv add --editable "/path/to/treaty[mcp]"
treaty-mcp deployctl:app
```

One tool per command except `exec`, named with dots as underscores (`deploy_rollback`).
The input schema is the args dataclass schema with field names as declared, secrets
replaced by `<name>_from_env` and `<name>_from_file`, and the framework keys the command
declares: `timeout`, `idempotency_key`, and `confirm_destructive`. The output schema is
the response envelope around the command's `output_schema`, and every result carries the
envelope as `structuredContent` and as JSON text; `isError` mirrors `ok`. Calls go through
`App.call`, the same path as an `exec` line, so an unconfirmed destructive tool call
returns `CONFIRMATION_REQUIRED` with its dry-run preview, idempotency keys, timeouts,
effect validation, and output caps all apply, and a streaming command returns its buffered
envelope. Tool annotations map `safe` to read-only and idempotent, `destructive` to
destructive, and `has_network_io` to open-world. `App.call(path, arguments)` is public
for other in-process adapters.

## Conformance

`conformance/deployctl.json` is a profile for the spec's deterministic kit. With the spec checked
out as a sibling directory:

```bash
uv run --project ../cli-agent-ergonomics ../cli-agent-ergonomics/conformance/run.py conformance/deployctl.json
```

The example CLI passes all twelve checks across levels 1 to 3. The same run is a pytest test
that skips when the spec checkout is absent.

## Start a project

Run it beside the treaty checkout, so that `../cli-agent-ergonomics` is the spec for the
kit and `--treaty-source` (needed until treaty is on PyPI) points at the checkout:

```bash
cd ..                                  # the directory holding treaty/ and cli-agent-ergonomics/
uv run --project treaty treaty init shop-tool --treaty-source treaty
cd shop-tool && uv sync && uv run pytest
uv run treaty conformance shop_tool.cli:app --run
```

`init` scaffolds a package with one command per danger level, typed outputs, declared exit
codes, a test using `app.run()`, and a conformance profile. `conformance` derives probes
from each command's first example and danger level, writes the profile, and with `--run`
executes the spec kit, exiting with `CONFORMANCE_FAILED` when checks fail. The kit is found
via `--spec-dir`, then `TREATY_SPEC_DIR`, then `../cli-agent-ergonomics` relative to the
current directory; a named location without `conformance/run.py` exits `4` instead of
falling through. `--out`, `--spec-dir`, and `--directory` reject `..` segments,
percent-encodings, and null bytes like every `Path` flag; pass an absolute path to reach
outside the working directory. Streaming commands get no probes: the kit expects one
envelope per run.

## Audit your CLI

```bash
uv run treaty audit myapp.cli:app
```

Ten ordered rules check the registry and print the next steps with a fix using your own
names: missing examples, danger levels that contradict command names, mutating commands
without their own exit codes, retryable codes on non-idempotent commands, untyped outputs,
undeclared network I/O, path-like fields not typed `Path`, wide mutating commands without
`--raw-payload`, missing cleanup hooks, and a missing conformance profile. `--all` lists
everything, `--strict` exits 79 (`AUDIT_FAILED`) on any warning so CI can gate on it, and
piping the output gives an envelope an agent can act on. Rules see declarations only; the
conformance kit covers runtime behaviour.

## Development

```bash
uv sync
uv run pytest
uv run mypy src
uv run ruff check src tests
```

The manifest test validates against the spec schemas in the sibling
`cli-agent-ergonomics` checkout; set `TREATY_SPEC_DIR` to point elsewhere.
