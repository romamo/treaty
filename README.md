# treaty

Zero-dependency Python CLI framework that implements the
[CLI Agent Spec](https://github.com/cli-agent-spec/cli-agent-spec): a manifest an agent can read in one call,
a response envelope on every exit, and typed exit codes with retry semantics.

The manifest is the treaty between the CLI author and the agent. Exit code
entries are its clauses.

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

Design decisions: zero runtime dependencies in core, handlers are plain functions
over a frozen dataclass of arguments, and every command lives in one flat registry
keyed by dot-path.

Every command declares `danger_level=` (`safe`, `mutating`, or `destructive`) and
`exit_codes=`; leaving either out is a `RegistrationError` naming the fix, and
`exit_codes=()` is the explicit "only the implicit codes" (REQ-C-001, REQ-C-002). `SUCCESS`
is always part of the map. Breaking after 0.0.6: both used to default to `safe` and `()`.

A handler raises only the exit codes its manifest entry lists: the ones in `exit_codes=`,
plus `GENERAL_ERROR`, `ARG_ERROR`, `TIMEOUT`, and `PRECONDITION` everywhere and `CONFLICT`
on mutating commands. Anything else, including framework names such as
`Exit.NOT_FOUND`, must be declared, or the run exits `1` with `UNDECLARED_EXIT_CODE`.

## Validation

Exit `2` means nothing ran: every exit-2 envelope has `phase: validation` and the handler
was never called, so an agent fixes the input and reissues. Checks that span several
fields go in the args dataclass's `__post_init__`, which runs in phase 1; its `ParseError`
joins the other field errors in `error.errors`, and `raise ParseError.combine([...])`
reports several at once:

```python
@dataclass(frozen=True, slots=True)
class Window:
    start: int = Flag(description="First hour")
    end: int = Flag(description="Last hour")

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ParseError("end is before start", context={"field": "end"})
```

A rule the manifest can show is better declared than coded: `requires=` on the command
lists `RequiredWhen("format", "csv", then=("separator",))`, `Excludes("output",
prohibited=("stdout",))`, and `DefaultWhenAbsent("output", target="level", default=9)`.
They are checked in phase 1 on what the caller passed, before `__post_init__`, on argv,
`exec`, `App.call`, and MCP alike, and appear in the manifest and `--schema` as `requires`,
so an agent knows the combination before its first call. The `conditional-rules` audit
rule spots a `__post_init__` that compares one field and raises about another.

A command that takes its subject by one of several identifiers declares
`RequiresAny(("isin", "figi", "symbol"))`: with none of them given it exits `2` listing
the three. `RequiresOne(("json", "yaml"))` also refuses two. A flag counts as given as for
the other rules: any value, the default included, except null, and a boolean only when
true. They appear in the manifest and `--schema` as `requires` entries, the
`ConditionalRule` shapes `{"any_of": [...]}` and `{"one_of": [...]}`, and `--schema`
also shows them as `requires_groups` and as `anyOf`/`oneOf` of the
`raw_payload_schema`; `--help` lists every rule under Rules, and an MCP tool's
description ends with them. The audit suggests `RequiresAny` for a
`__post_init__` that raises when none of several fields is set.

A positional with a default is optional: `Arg(default=None, ...)` makes the manifest's
`PositionalEntry` say `required: false`, leaves it out of `--schema`'s `required`, and shows
it as `[query]` in `--help`. Optional positionals may follow required ones, never precede
them. A command that takes its subject as a positional or from a flag names both in one
`RequiresOne`:

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class Resolve:
    query: str | None = Arg(default=None, description="Instrument query or identifier")
    figi: str | None = Flag(default=None, description="Resolve by FIGI instead")

# @app.command("resolve", ..., requires=[RequiresOne(("query", "figi"))])
```

So `resolve AAPL`, `resolve --figi BBG000B9XRY4`, and `resolve -- --odd-name` run, while
`resolve` and `resolve AAPL --figi BBG000B9XRY4` exit `2` before the handler runs.

`--validate-only` on any command runs phase 1 and stops: exit `0` with `data: null` and
`meta.validation_only: true`, or exit `2` listing every error. The credential gate, the
idempotency store, and the handler never run.

An identifier field declares its shape: `Flag(pattern_type="alphanumeric_id")` (or `uuid`,
`semver`, `url`) without registering a scalar, or `pattern="[a-z0-9-]{3,64}"`; the error
names the flag and the pattern. The `id-pattern` audit rule warns about `str` fields named
`id`, `*_id`, `slug`, or `ref` with neither. `Flag(from_stdin=True)` lets `--id -` read the
value from stdin, as in `tool get --format id | tool delete --id -`; an array takes one
item per line, and empty stdin exits `2` with `EMPTY_STDIN`.

A handler may be `async def`, and so may a resource's `acquire` and `release`:

```python
class Db:
    @classmethod
    async def acquire(cls, args: object, ctx: Ctx) -> Db:
        return cls(await asyncpg.create_pool(DSN))

    async def release(self) -> None:
        await self.pool.close()


@app.command("sync", description="Sync the catalog", danger_level="mutating", exit_codes=())
async def sync(args: SyncArgs, ctx: Ctx, db: Db) -> Synced:
    ...
```

Each run gets one event loop, on a thread of its own, shared by the handler and its
async resources, so a pool opened in `acquire` works in the handler and closes in
`release`. At the command's timeout the handler is cancelled, so its `finally` blocks
run, and the run answers `TIMEOUT`. Tasks the handler started but did not await are
cancelled and reported in an `UNAWAITED_TASKS` warning. An async resource needs an async
handler, and a sync resource cannot depend on one. `ctx.http`, `ctx.run`, and `ctx.lock`
block, so an async handler calls its own async clients instead. Streaming handlers,
`cleanup=`, `cursor_check=`, and other hooks stay plain `def`: `async def` there is a
`RegistrationError` instead of a body that never runs.

A `ParseError` or `Exit.ARG_ERROR` raised by a handler or a resource's `acquire` comes
after user code ran, so it exits `1` with `VALIDATION_AFTER_START` and `phase: execution`,
keeping its message, context, and suggestion. Move the check into `__post_init__` to get
exit `2`.

A `str` value containing a newline, carriage return, or null byte is refused in phase 1
on every route (argv, `exec`, `--raw-payload`, MCP) with `rejected_pattern` in the
context, because it can end a command line or log record wherever the value goes next.
`Flag(multiline=True)` accepts line breaks for message bodies, and the manifest adds
"(may contain newlines)" to its description; the `multiline-flag` audit rule suggests it
for fields named like `message`, `body`, `description`, or `text`. Secrets are exempt.
`Flag(max_bytes=255)` bounds a text value in UTF-8 bytes, such as a backend column: a
longer one exits 2 with `FIELD_TOO_LARGE` and `max_bytes` and `actual_bytes` in the
context, never the value (rule `field-limits`).

## Install

Every command here is non-interactive and safe to repeat:

```bash
uv tool install treaty         # the treaty CLI on PATH
uv add treaty                  # the library, inside a uv project
treaty --version               # verify: prints a JSON envelope, exits 0
```

To track unreleased changes, install from a checkout instead:

```bash
uv tool install --reinstall /path/to/treaty
uv add --editable /path/to/treaty
```

The [tutorial](https://github.com/romamo/treaty/blob/main/docs/tutorial/index.md) takes a CLI, new or migrated from argparse, click, or
typer, through every audit rule to a tested, documented release.

## Built-ins

Every app gets `manifest`, `version`, and `exec` (disable with `App(..., enable_exec=False)`),
plus `doctor`, `cleanup`, `status` (see Declarations), `generate-skills`, `mcp-validate`
(see MCP), `audit-log` (see Audit log), and `completion`, which yield: an app command or
group of the same name replaces them, and the `builtin-shadowed` audit rule says so.
`App(schema_changelog=...)` adds `changelog` (see Schemas).

- Each built-in's manifest entry, and its `--schema`, carries `"builtin": true`
  (ManifestResponse 3.1), so an agent listing what the tool does can drop the framework's
  commands without a name list. An app command has no `builtin` key, which reads as `false`,
  and that includes an app command that replaces a built-in's name (REQ-O-041)
- `manifest --etag sha256:...` answers exit 0, `data: null`, and `meta.not_modified: true`
  while the manifest is unchanged, on the CLI, in `exec`, and through `App.call`
  (REQ-O-041)
- `completion bash` or `completion zsh` prints a completion script generated from the
  manifest: every command and group, every flag, and the values of enum and path flags and
  positionals. The script is static, so a tab press runs no Python and writes no audit log
  entry; regenerate it after an upgrade. `--format plain` prints the script alone (off a
  terminal the answer is the envelope, with the script in `data.script`). MCP serves no
  `completion` tool:

  ```bash
  source <(myapp completion bash --format plain)         # this bash session
  myapp completion zsh --format plain > ~/.zfunc/_myapp  # zsh, ~/.zfunc on fpath
  ```

- `generate-skills --output-dir skills` writes `CONTEXT.md` and one `SKILL-<command>.md`
  per app command: frontmatter with `name`, `description`, `version`, `command`, and the
  `args` schema (every value JSON, so valid YAML), then at least three examples,
  guardrails from the danger level and exit codes, and patterns (REQ-O-034). Skill files
  are always Markdown: `--format` is the envelope's representation
- `treaty agents-md myapp.cli:app` writes AGENTS.md from the registry: a
  `<!-- cli-version: -->` line, then Canonical Invocation, Non-Interactive Flags,
  Environment Variables, Input Conventions, and CI Validation between treaty markers;
  text outside them, `## Installation` included, is kept. `treaty check-docs myapp.cli:app
  AGENTS.md skills` exits 81 (`DOCS_OUT_OF_DATE`) when a declared version, a section, or a
  command, flag, or variable named there disagrees with `--help`, or a generated section
  differs from what `agents-md` writes now, such as one a new command is missing from, one
  line per mismatch
  (REQ-O-043 to REQ-O-046). `treaty init` writes AGENTS.md and a test that runs the check
`App(credentials=...)` adds `check-permissions` (see Credentials) and `App(jobs=...)` adds
`job status` and `job cancel` (see Async jobs).
`<app> --version` at the root is an alias for `<app> version`; a command's own `--version`
flag is never shadowed.
`exec` reads one `DispatchRequest` per stdin line and dispatches in-process, writing one
envelope per line with `_cmd` and `_line` in `meta`. A stream with no lines exits `2` with a
single `EMPTY_STREAM` envelope, a piped plan over 64 KiB (`App(max_stdin_bytes=...)` or
`<APP>_MAX_STDIN_BYTES`) exits `2` with `STDIN_TOO_LARGE` before anything runs, and
`--input-file PATH` reads a plan of any size from a file (`-` is stdin, capped). A terminal on
stdin exits `2` with `STDIN_IS_TTY` instead of waiting for input. A line whose `_cmd` is no
registered command is `UNKNOWN_COMMAND`, unless `App(exec_fallback=)` passes it to the
dispatcher of a CLI being migrated (see the [migration
chapter](https://github.com/romamo/treaty/blob/main/docs/tutorial/B-migrate/click-typer.md#migrating-a-large-cli-one-command-at-a-time)):

```bash
printf '%s\n' '{"_cmd":"deploy.rollback","service":"api","_opts":{"to":"1.3.9"}}' \
  | deployctl exec --ignore-errors --dry-run
```

Any command can read a payload the same way: `stdin_input=True` adds `--input-file` and puts
the text in `ctx.stdin_text` before the handler runs; over the cap (`<APP>_MAX_STDIN_BYTES`
also sets it) the run exits `2` with `STDIN_TOO_LARGE` and a `hint` naming `--input-file`.
In `exec` and MCP, where stdin is taken, such a command needs `input_file`.

A command that reads a record stream takes `stdin_input="lines"` instead: nothing is read
before the handler runs, and `ctx.stdin_lines` yields one line at a time (without its `\n`
or `\r\n`) as the producer writes it, so there is no total cap. Each line has a cap of its
own, `App(max_line_bytes=...)` (1 MiB by default): a longer line ends the run with exit `1`
and `LINE_TOO_LARGE`, and a line that is not UTF-8 with `LINE_NOT_UTF8`, each with the
line number in `error.context.line`. With `streaming=True` the command is a filter, one
event out as each line comes in, so `a | b | c` runs concurrently; every line read restarts
the stream's idle timeout, as every event does. `--input-file` reads the lines from a file,
and in `exec`, `App.call`, and MCP the request gives them as `input_lines`, an array of
strings. `--schema` says `"stdin_mode": "lines"`, and the manifest's `stdin` declares each
command's mode, `buffered`, `lines`, or `records`, with a cap other than the spec's
default (ManifestResponse 3.8).

```python
@app.command("resolve", description="Resolve each ISIN", danger_level="safe",
             exit_codes=(), streaming=True, stdin_input="lines")
def resolve(args: NoArgs, ctx: Ctx) -> Iterator[Resolved]:
    for line in ctx.stdin_lines:
        yield lookup(json.loads(line))
```

When the input is another command's output, `stdin_records=Sec` (a frozen dataclass) does
the unwrapping: `ctx.stdin_records` yields one `Sec` per record. A line may be a bare JSON
object, as `--format ndjson` writes, or a treaty envelope, as `json` and `jsonl` write: an
envelope gives its `data` (each item of an array), and a stream's terminal envelope, or a
whole response, ends the input. An upstream `ok: false` ends the run with exit `1` and
`UPSTREAM_FAILED`, the upstream error in `error.context.upstream` with secret-named keys
redacted and tokens masked; a stream that stops before its terminal envelope (the producer
was killed) with `UPSTREAM_INCOMPLETE`, so a failed upstream never reads as a short input.
Each record is checked field by field, as an argument payload is, and built, so its
`__post_init__` runs; a line that fails exits `1` with `RECORD_INVALID` and
`error.context.line` and `error.context.field`. Keys a record has no field for are refused:
`--fields` on the producer keeps only the ones it needs. An upstream response that was one
page of more, or had fields cut to fit its output cap, adds an `UPSTREAM_TRUNCATED` warning.
`--schema` gives the record type as `stdin_records_schema`, to match against a producer's
`output_schema`. Bare lines carry no status, so with `ndjson` a failed producer shows only in
its own exit code: run the pipeline with `set -o pipefail`.

```python
@app.command("resolve", description="Resolve each security", danger_level="safe",
             exit_codes=(), streaming=True, stdin_records=Sec)
def resolve(args: NoArgs, ctx: Ctx) -> Iterator[Resolved]:
    for sec in ctx.stdin_records:
        yield lookup(sec.isin)
```

```bash
probe securities --format json | probe resolve --format json
```

## Flag order

`--format`, `--help`, `--schema` (alias `--print-schema`), `--output-schema`,
`--schema-version`, `--stable-output`, `--unmask`, `--no-injection-protection`, and `--max-output` are global: they are accepted anywhere before `--`,
so a command cannot declare a flag with those names or the short `-h`. The manifest lists
them once, in its root `flags` map (ManifestResponse 3.0 and later). The framework's other
command flags (`--config`, `--quiet`, `--verbose`, `--debug`, `--fields`, `--cwd`, and the
rest of `RESERVED_GLOBAL` in `_framework.py`) cannot be field names either: registration
refuses a field that would never reach the handler. A name reserved before its feature
lands exits `2` with `RESERVED_FLAG` when passed; every reserved name is implemented today.
The short `-v` (`--verbose`, and `-vv` for `--debug`) is not reserved: a command may still
declare `short="v"` and keeps it (see [Logging and verbosity](#logging-and-verbosity)).
Every other flag, including `--timeout`, `--confirm-destructive`, `--idempotency-key`, and `--raw-payload`,
belongs to a command and goes after the full command path:

```bash
deployctl --format json deploy rollback api --to 1.3.9 --dry-run   # ok
deployctl deploy rollback api --to 1.3.9 --dry-run --format json   # ok
deployctl --dry-run deploy rollback api --to 1.3.9                 # ARG_ERROR
```

Any option repeated with a different value exits `2` naming the option; repeating the same
value is accepted, and array flags accumulate. A negative number such as `-5` is a value,
not a flag.

A command that forwards its trailing arguments to a child declares
`option_placement="strict"`: options, global or local, go before the first positional, and
that positional and every token after it reach the positionals verbatim, the last of them a
`tuple[str, ...]`. So `tool run --format json ./script --child-flag` hands `--child-flag`
to the script, and `tool run ./script --format plain` forwards `--format plain` instead of
parsing it. Every manifest entry carries `option_placement` (`any` by default), and the
`option-placement` audit rule flags a variadic positional passed to `ctx.run` under `any`.

A command that wraps another tool's own argument parser declares `passthrough=True`. Its
args type is `NoArgs`, `ctx.argv_rest` holds every token after the command path verbatim
(`--help`, `--`, and a global's name included), and the handler returns the tool's exit
code, or lets the parser's `SystemExit` through, which becomes the process exit code:

```python
@app.command("ingest", description="Import statements", danger_level="mutating",
             exit_codes=(), passthrough=True)
def ingest(args: NoArgs, ctx: Ctx) -> int:
    return beangulp_main(list(ctx.argv_rest))
```

The tool owns stdout, so treaty writes the final envelope (exit code, duration, any
exception) as the last line on stderr, whatever `--format` says, and to `--output PATH` as
well. Treaty's own flags go before the path: `tool --output env.json --timeout 600 ingest
extract statement.csv`. The timeout, signals, session deduplication, and the audit log
apply as to any other command; the log records `argv` as `[OMITTED]`, since treaty cannot
tell a secret in it. `help_command=("help",)` hands the tool that argv in place of a lone
`--help` or `-h` after the path. The manifest entry says so (REQ-C-031,
ManifestResponse 3.9): `arguments: "passthrough"`, `option_placement: "strict"`,
`help_argv` for `help_command=`, `output_file: "envelope"`, and no `flags`, since treaty's
own go before the path as global options do; `--schema` lists them. Exec lines and `App.call` pass the tool's tokens as `"argv": [...]`, and `treaty-mcp`
lists no passthrough command. The delegated parser follows its own colour rules, not
treaty's: Python 3.14's argparse colours its help and errors when `FORCE_COLOR` is set,
even off a terminal, and `NO_COLOR` turns that off. A passthrough command cannot be
destructive, and REQ-C-031 exempts it from the requirements that would need treaty to
parse or shape the tool's arguments and output.

A command flag placed before the path fails with `ARG_ERROR`, names the command the remaining
words resolve to in `context.command`, and puts the corrected order in `suggestion`. Plain
mode prints every error's suggestion as a final `hint:` line on stderr.

## Renamed commands

A command that moves keeps answering at its old path, with exit `13`:

```python
app.redirect("deploy.undo", to="deploy.rollback")  # reason="renamed", permanent=True
```

`deployctl deploy undo api --to 1.3.9` exits `13` with `REDIRECTED` and
`error.redirect.command` set to `deployctl deploy rollback api --to 1.3.9`, the same
arguments under the new path, ready to run as is. In `exec` and MCP the replacement is the
new path. The target lists `deploy.undo` in its manifest `aliases`; registering a redirect
to a missing command or from a live path is a `RegistrationError`.

Before a command or flag goes, it is deprecated for at least a minor release:

```python
@app.command("old-sub", ..., introduced_in="1.0.0",
             deprecated=Deprecated("1.1.0", replacement="new-sub", removed_in="2.0.0"))
```

It keeps working, and every run writes
`{"level":"warn","code":"DEPRECATED","message":"...","replacement":"tool new-sub"}` to
stderr and a `DEPRECATED` warning to the envelope; `Flag(deprecated=Deprecated(...))` does
the same with `DEPRECATED_FLAG` when the flag is passed. `--schema` lists `introduced_in`,
`deprecated_in`, `replacement`, and `removed_in`. `treaty audit app:cli --baseline
manifest.json` compares against the last release's `manifest` output and fails on a
command, flag, or exit code that vanished without a redirect or a deprecating release,
unless the major version went up.

## Output formats

`--format` takes `json`, `jsonl`, `ndjson`, `plain`, `tsv`, and any format the app registers. With no flag,
`<APP>_FORMAT` decides (`DEPLOYCTL_FORMAT` for `deployctl`, failing as the same `--format`
value would); without that, the format is `json` when stdout is not a terminal
or `CI` is set, and `plain` otherwise.

`json` writes the full response envelope, one compact line per envelope; it is the contract
agents read and takes no renderer. `jsonl` is the same output under the name line-oriented
readers ask for. Every other format writes the result data as text and errors as prose on
stderr. `tsv` is built in: a header row, then one row per item (nested values as compact
JSON, `null` as an empty field, and a backslash, tab, or line break in a value escaped as
`\\`, `\t`, `\n`, `\r`, never quoted); `treaty.table(",")` is the same renderer for CSV
with the `csv` module's quoting, `app.format(Format.CSV, render=table(","))`.

`ndjson` writes `data` alone for `jq -c`, `duckdb read_json`, `mlr`, or the next command in a
pipe: one compact JSON line per item of a list result or per event of a stream (a stream
event that is a list stays one line), a single result as one line, and nothing for `null`
data. Keys are sorted and values masked and redacted as in the envelope, and `--fields`
applies. The status goes elsewhere: the exit code, and on stderr one JSON line each for
the error (`{"error": {...}}`, credential-named context `[REDACTED]`), every warning
(a `WarningDetail` object), a cut page (`{"pagination": {...}}`), and `ctx.log` lines.
`--max-output` caps what a buffered answer writes: whole records only, stopping before
the first that would pass the cap, so a first record larger than the cap leaves stdout
empty. A stream is capped record by record, as `jsonl` caps each envelope: a record over
the cap is left out and the stream goes on, so a pipeline is never silenced. Each cut
writes one `{"truncation": {...}}` line to stderr (`truncated`, `total_bytes`,
`returned_bytes`, `total_count`, `returned_count`, `omitted_count`, `max_output_bytes`,
`seq` for a stream's record, and a `truncation_hint` rerun with a larger `--max-output`)
and a `FIELD_TRUNCATED` warning on `data`. The exit code is unchanged, as it is for a cut
envelope, unless `--warnings-as-errors` is set. `ndjson` takes no renderer, and `exec`,
`App.call`, and the MCP adapter answer with envelopes whatever `--format` says.

A renderer receives `data` as JSON values (dicts and lists, after secret redaction) and
returns the text. A format is a `Format` member, or a name treaty does not know:

```python
from treaty import App, Format

app = App("hello", version="0.1.0")
app.format(Format.CSV, render=render_csv)  # offers --format csv to every command


@app.command(
    "greet",
    description="Say hello",
    danger_level="safe",
    exit_codes=(),
    renderers={Format.PLAIN: render_greet},
)
```

A command's `renderers=` overrides the app's renderer for that format. `Format` lists every
format treaty knows (`plain`, `json`, `jsonl`, `ndjson`, `csv`, `tsv`, `yaml`,
`markdown`); an app offers `plain`, `json`, `jsonl`, `ndjson`, `tsv`, and the ones it
registers, and the manifest and `--help` list exactly those. Each command's manifest
entry lists the ones beyond `json`, `jsonl`, `tsv`, `plain`, and `ndjson` in
`output_formats`, with `id` where it has an `id_field`. Any other value exits `2` listing
them, before anything runs or any file is written.

A command's `renderers=` can also name a format the app does not register: that command
alone offers it. Its manifest entry lists it in `output_formats`, its `--help` and
completion offer it, and `--format` naming it on another command exits `2` listing that
command's formats. `<APP>_FORMAT` naming it is a default for the session, so a command
without it answers as if the variable were unset. Wrap the renderer in `FormatRenderer`
to state what it writes, which the command's manifest entry lists in
`output_media_types` (ManifestResponse 3.12):

```python
@app.command(
    "why",
    description="Explain a failure",
    danger_level="safe",
    exit_codes=(),
    renderers={"html": FormatRenderer(render_page, media_type="text/html")},
)
```

An app can offer a format treaty does not list, such as a page for a person:
`app.format("html", render=render_html, media_type="text/html")`. The name (lowercase
letters and digits, words joined by `-` or `_`) joins `--format`'s values, `<APP>_FORMAT`,
the manifest, `--help`, and completion, and a command's `renderers={"html": ...}` overrides
it. It runs as `plain` does: errors go to stderr as prose, `--output` writes the rendered
text, and `ctx.mode` is `Format.PLAIN`; `ctx.format_name` is the name the caller asked for
(`FormatName("html")`), so a handler can tell it from `plain`; an `exec` line and
`App.call` report `json`, as they run in JSON. `media_type` is the root `format` flag's
`media_types` entry in the manifest (ManifestResponse 3.12), beside treaty's own for
`ndjson`, `csv`, `yaml`, and `markdown`, and `--help` states it; `plain` and `tsv` keep the
spec's media type. A string naming a `Format` member is
that member, and `json`, `jsonl`, `ndjson`, and `id`, which treaty writes itself, take no
renderer.

A command declared `output_file=True` takes `--output PATH`: the result goes to the file in
the `--format` representation, and stdout gets the JSON envelope with `data: {"path": ...,
"bytes": N}`. A failed run writes no file. `--output json` (any format name) exits `2`
suggesting `--format json`; `--output` never selects a representation.

A command returning `treaty.Binary` writes the raw bytes instead, whatever the `--format`,
and `data` is `{"path": ..., "bytes": N, "content_type": ..., "sha256": ...}`, with
`sha256` in lowercase hex and `content_type` only when the command declared one. The
manifest says which a command does: `"output_file": "binary"` here, `"formatted"` on the
other `output_file=` commands (ManifestResponse 3.3), `"envelope"` on a passthrough
command, and `"handler"` where the app's own `output` field takes the path and its handler
writes the file (3.6). A base other than the working directory adds `output_file_base`,
`project_root` or `resource` (3.7). Without `--output` the bytes stay
base64 in `data`, and the `binary-output-file` audit rule suggests `output_file=True`.
`--output -` exits `2` there, as stdout carries only the envelope.

A relative `--output`, raw bytes or not, lands in the working directory (or `--cwd`). A
CLI whose paths are relative to a project it resolves names that directory instead, and an
absolute `--output` is used as given:

```python
@app.command("render", description="Render the inventory", danger_level="safe",
             exit_codes=(), output_file=Project)  # a resource class with a directory
def render(args: RenderArgs, ctx: Ctx) -> Inventory: ...
```

`output_file=` takes `True` (the working directory), `treaty.OutputBase.PROJECT_ROOT` (the
command's `project_root=`; with no root found a relative `--output` exits `2`), a resource
class with a `directory`, or a function `(ctx: Ctx, *resources) -> Path`. The resource or
function is resolved before the handler runs, and the `--output` description in `--help`
and the manifest names where a relative path lands. A failed run still writes no file, and
a path with `..` exits `2` whatever the base.

Without a renderer, `plain` prints flat lines, one item each: `key: value`, with dotted
paths for nested values (`release.tag: 1.3.9`) and line breaks inside strings escaped. A
list of objects whose values are all scalars, such as a `list[Item]` result, prints as an
aligned table instead: a header of the field names in the dataclass's field order, one row
per object, numbers (and `Decimal` fields) right-aligned, `null` as an empty cell, and
`(no rows)` for an empty `list[Item]`. A list of models an output adapter writes, such as
pydantic's, is a table too, its columns in the order the dump writes them and its integer
and number properties right-aligned:

```text
id  name  size_bytes
 1  api         2048
```

When `COLUMNS` is a positive integer, a wider table cuts its widest text column, one cell
at a time down to four, ending a cut value in `…`; numbers are never cut, and a file
written with `--output` is never cut. Wide East Asian characters count as two cells.
`Out(table=False)` leaves a field out of the table, and a field holding an object or a
list, unless left out that way, keeps the whole list in `key: value` blocks, the way a
stream prints its events. JSON, `jsonl`, `ndjson`, and `tsv` do not change.
`app.format(Format.PLAIN, render=...)` replaces the table and the lines for the whole app.
`manifest` and `--schema` stay JSON in every text format, since they are read by programs
(one line in `ndjson`).

Breaking after 0.0.4: `renderers={Format.PLAIN: ...}` replaces `plain=`, which is no
longer accepted, and `Format` replaces `OutputMode`.

Breaking after 0.0.3: `plain` replaces `human`, and `plain=` replaces `human=`. `human` is no
longer accepted anywhere; `--format human` exits `2` listing the allowed values.

## Timeouts

Every handler runs under a wall-clock limit: `App(default_timeout=60)` app-wide,
`@app.command(..., timeout=5)` per command, and `--timeout` on any command declaring
`has_network_io=True`, on every streaming command, and on every command whose own
`timeout=` is `None` or longer than the app default, so a caller can bound one run of
long work (`--timeout 0` disables it; at most one year). A stream buffered in-process (`App.call`, MCP) always has a deadline: the
caller's `timeout`, else the app default; `0` is refused there. On expiry the
framework writes a `TIMEOUT` envelope, exits `10`, and records `meta.timeout_ms` on every
response. Handlers read `ctx.remaining`, the seconds left (`None` without a limit), to
pass the same deadline to their network calls, and `ctx.expired` to stop a long loop
with the work done so far rather than run on after `TIMEOUT`;
the `network-timeout` audit rule flags `urlopen`, `http.client` connections,
`socket.create_connection`, `requests`, and `httpx` calls without `timeout=` in network
commands (REQ-C-012). The `timeout-budget` rule warns when a command's `retry=Retry(...)`
may wait longer in all than its timeout, with a `timeout=` sized to the waits, and when a
`heartbeat=True` command inherits the app default; the `explicit-timeout` advice asks a
mutating or destructive command on the app default to declare `timeout=` (the default
itself counts, `None` runs unbounded). An
idempotency key stays locked until a timed-out or cancelled handler really finishes, so a
retry never runs beside it: it waits up to its own timeout, then replays the recorded
result or exits `10` with `IDEMPOTENCY_KEY_BUSY`. An unusable state directory or a damaged
record exits `4` (`STATE_DIR_UNWRITABLE`, `IDEMPOTENCY_RECORD_CORRUPT`).

A handler that raises anything else exits `1` with `HANDLER_CRASHED`, naming the exception;
the traceback goes to stderr with secret values redacted. A result or `Exit` payload the
framework cannot serialize exits `1` with `INVALID_OUTPUT` or `INVALID_EXIT`.

## Long-running commands

`App.main()` sets `PYTHONUNBUFFERED=1` for children and makes stdout line-buffered when it is
not a terminal, so a reader gets each envelope and event as it is written (REQ-F-053). A
command declared `heartbeat=True` also writes `{"status": "running", "heartbeat": true,
"elapsed_ms": N}` lines to stdout every `--heartbeat-ms` (default 10 000, `0` for none)
while its handler runs, in JSON mode from argv; the envelope is still the last line.
`--schema` shows `heartbeat_ms` so an agent knows which lines to skip.

## Stdout hygiene

Stdout carries only envelopes (REQ-F-006). While a command runs, `sys.stdout` points at
stderr, so a stray `print()` from the handler or a library lands there and the envelope
gets a `THIRD_PARTY_STDOUT` warning with the text (first 4 KiB) and the byte count; lines
of JSON go to stderr unreported. Under `App.main()` file descriptor 1 is a pipe to
stderr, so a child process or a C extension cannot write ahead of the envelope and its
text is in the warning too; on stderr that text is cleaned as a stray `print()` is, so
only colors (where the run may color) of its terminal escapes stay. `App.run()` swaps
only `sys.stdout`. A library that prints on import is caught when the entry module calls
`treaty.intercept_stdout()` before importing the app, as the `entry.py` of `treaty init`
does (REQ-F-060). Under
`treaty-mcp` the swap lasts the whole process. Handlers log with
`ctx.log("connecting", host=host)`: one line on stderr, a JSON object with `level`,
`message`, and `fields` in JSON mode and `message key=value` otherwise. Declared secrets
and fields named like credentials (`token`, `password`, `API_KEY`, `DB_PASS`,
`Authorization`, `Cookie`, at any depth) print as `[REDACTED]` (REQ-F-051).

## Logging and verbosity

`ctx.log` is INFO; `ctx.progress("copying", done=3, total=10)`, `ctx.debug(...)`, and
`ctx.log_error(...)` write the other levels. What reaches stderr depends on the run
(REQ-F-038, REQ-O-008):

| Run | Written on stderr |
|-----|-------------------|
| Off a terminal, or under `CI` | Errors and warnings only: tracebacks, `log_error`, deprecation lines |
| A terminal | Also info and progress, and stray `print()` text |
| `--verbose` | Info and progress anywhere, even under `CI` |
| `--debug` | Also `ctx.debug` and the framework's trace: config resolution, each `ctx.http` request (headers redacted), each child's argv and exit, locks, the audit log; debug records of any `logging` logger, such as urllib3's; a stray `print()` is attributed to its file and line |
| `--quiet` | Nothing, not even errors: the envelope carries them |

`-v` is short for `--verbose`, and `-vv` or `-v -v` for `--debug`, in any position. Unlike
`-h`, they yield: a command that declares `Flag(short="v")` keeps `-v` for its own flag, on
that command only, where the long forms still work. The manifest keeps them out of the root
`flags` shorts, so no command's own `-v` collides there.

Records of Python's `logging` module, a library's included, go the same way by their own
level: a WARNING or ERROR record is written as a `warn` or `error` line wherever
`log_error` is, INFO where `ctx.log` is, and DEBUG under `--debug`, each redacted, with
the logger's name in `fields.logger`. A handler sits on the root logger for the whole run,
`App.call` included, and the root's level is lowered for the run when it would hide what
the run shows, then restored; so no record falls through to `logging.lastResort`, which
writes to `sys.stderr` unredacted. A handler abandoned at its timeout keeps it on the
root until its thread ends: a record at WARNING or above that it logs after the run
returned is written to `sys.stderr` as `lastResort` would, its secrets redacted, and not at
all when the host's own handler takes the record. While such a thread lives, `sys.stdout`
and `sys.stderr` are wrapped: what that thread prints or writes is redacted and goes to
stderr, every other thread's writes pass through, and the streams are put back once it
ends, unless the host replaced one meanwhile. The same wrapping stands while an `App.call`
runs: what its handler prints or writes to `sys.stderr`, on the calling thread or on the
worker a timeout runs it on, is redacted and stays on the stream it was written to, and
the host's own threads write through untouched; a handler that outlives its timeout goes
over to stderr as its call returns. Under `app.run`, `sys.stdout` stays the run's own
stand-in. Bytes written through `.buffer` pass through as they are.

The three flags are exclusive (two exit `2`). Stray `print()` text off a terminal is
dropped, and still reported in `THIRD_PARTY_STDOUT`. The `log-not-print` audit rule flags
handlers that call `print()` or `sys.stderr.write`.

`--warnings-as-errors` turns a successful run with any warning into exit `1` with
`WARNINGS_AS_ERRORS` (`context.count` and `codes`); the warnings and `data` stay, since a
mutating command already applied its effect. Framework warnings count too, and each exec
line and a stream's terminal envelope are checked (REQ-O-025).

## Audit log

The audit log is off by default, and nothing is created for it until it is turned on
(REQ-O-030). The app turns it on with `App(audit_log=treaty.AuditLog())`; the operator, or
an agent runtime, with `<APP>_AUDIT_LOG`, which wins over the app:

| `<APP>_AUDIT_LOG` | Effect |
|---|---|
| unset | on only when the app passed an `AuditLog` |
| `1` | on, at `AuditLog.path`, else the default path |
| `0` | off, even when the app turned it on |
| an absolute path | on, at that path |

Any other value exits `2` with `INVALID_AUDIT_LOG_SETTING` for every invocation except
`--help` and `--version`; the message names the accepted values.
The default path is `$XDG_STATE_HOME/<app>/audit.jsonl`, else
`~/.local/state/<app>/audit.jsonl`.

While the log is on, every invocation that resolves to a command appends one line to
`audit.jsonl`, whatever its outcome: argument errors, `--validate-only`, `--dry-run`, and
a destructive command refused without `--confirm-destructive` included. An unknown
command, `--help`, `--version`, `completion`, `manifest`, `--schema`, and `audit-log`
itself are not logged. Each line matches the spec's `audit-log-entry.json`: `timestamp`,
`command`, `args` (the parsed arguments, never raw argv, plus `validate_only`,
`confirm_destructive`, and `no_injection_protection` when given), `exit_code`,
`duration_ms`, `request_id`, `warnings` (the codes), and `trace_id` and `session_id`
(`<APP>_SESSION`) when set. Secret fields, the login token, and every key named like a
credential are `[REDACTED]` at any depth (REQ-F-034). A line never exceeds 16 KiB: the
largest `args` values become `[TRUNCATED]` and `truncated` is `true`.

A value that is no secret but should not be kept, such as a message body, is declared
`Flag(audit=False)` or `Arg(audit=False)`: its key stays in `args` with the value
`[OMITTED]`. Unlike `secret=True` it is still passed on argv and echoed in errors, and
`exec`, `--raw-payload`, MCP, and `app.call` take it as before; with `secret=True` too, the
secret's `[REDACTED]` wins. The manifest adds "(omitted from the audit log)" to such an
argument's description, and the args schema marks the property `"x-audited": false`. The
audit log is the only record that keeps arguments: idempotency records hold a hash of the
arguments and the `data` a repeat replays, so they are not masked, and `--debug` traces
carry no arguments.

`meta.audit_log_path` names the file on every response, and the manifest lists it as a
`log` side effect of each logged command; neither appears while the log is off. A log that
cannot be written adds `AUDIT_LOG_UNAVAILABLE` and never fails the command. The file is
created `0600` and its new directories `0700` whatever the umask, and each line is one
`write`. Past `max_bytes` (10 MiB), or once its first entry is older than `max_age_days`
(30), it rotates to `audit.1.jsonl`, keeping `keep` (5) rotated files; rotated files older
than `max_age_days` are deleted before each append, so it never holds more than
`(keep + 1) * max_bytes`, 60 MiB by default, plus one entry. `cleanup` never removes it.

```bash
export DEPLOYCTL_AUDIT_LOG=1                          # on for this shell's runs
deployctl deploy --env prod                           # appends one entry
deployctl audit-log --since 1h --format jsonl         # one entry per line
deployctl audit-log --trace-id abc123 --limit 100     # the newest 100 of one trace
```

`audit-log` is on every app, since the operator can turn the log on for any of them. It
streams entries oldest first, filtered by `--since` (`30s`, `15m`, `1h`, `7d`, or ISO 8601
with a UTC offset), `--command` (`config` matches `config set` but not `configure`), and
`--trace-id`, redacting them again as it reads. While the log is off it exits `4` with
`AUDIT_LOG_DISABLED`, so "nothing was recorded" never reads as "nothing happened".

In JSON mode every string value is cleaned before it is written: ANSI escape sequences
are removed, and null bytes and lone surrogates become U+FFFD (REQ-F-007, REQ-F-016).
Object keys and carriage returns are left as returned. Text formats clean values the same
way before they are rendered, and show a key's or a value's other controls as escapes
(`\x1b`, `\x07`, `\r`), the Unicode bidirectional embeddings, overrides, and isolates
among them (`\u202e`), which JSON keeps as data; the LRM, RLM, and ALM marks stay text. A
renderer's own text keeps its colors only where the run may color, and a CRLF. Stderr error lines show every control as its escape. `ctx.color` tells a renderer whether it may
color: never in JSON mode, under `NO_COLOR` (even empty), `CI`, `GITHUB_ACTIONS`,
`JENKINS_URL`, or `TERM=dumb`, or when stdout is not a terminal (REQ-F-008). `App.main()`
sets `PAGER=cat` and `GIT_PAGER=cat` for every child process, and `NO_COLOR=1` whenever
color is off (REQ-F-010); `App.run()` leaves the process environment alone. See
[Running programs](#running-programs) for what `ctx.run` children get.

`datetime`, `date`, and `time` results are ISO 8601 strings (`2026-09-27T10:00:00Z` for
UTC), and `Decimal` is fixed-point text; the output schema says `format: date-time` and so
on (REQ-F-005). A naive `datetime` is `INVALID_OUTPUT`: give it a `tzinfo`.

Every `error.message` is written as a sentence, with a capital first letter and closing
punctuation, author messages included (REQ-C-013). A recoverable error (`retryable`, or one
with `fix_required`) always has a `suggestion`: the one given to `Exit`, else the exit
code's `app.exit_code(..., suggestion=...)`, else the `fix_required` text or a generic
retry step. The audit rule `exit-code-suggestion` flags the app's retryable codes without
one; a framework code such as `RATE_LIMITED` takes its suggestion on the raise.

## Running programs

Handlers start other programs with `ctx.run` and `ctx.pipeline`, which take argument
lists and never a shell:

```python
done = ctx.run(["git", "log", "-1", "--format=%H"])
head = ctx.pipeline([["git", "log", "--oneline"], ["head", "-5"]]).stdout
```

- Arguments reach the program as literal text: `"hello world"`, `"*.json"`, and
  `"; rm -rf /"` are one argument each, never split, expanded, or executed, so treaty does
  not need to reject shell metacharacters (REQ-F-044, REQ-F-062). A string where the list
  belongs is `SHELL_STRING_PROHIBITED`: a `RegistrationError` when the handler's source
  shows it (`ctx.run("git log")`, an f-string, a concatenation), exit `1` when it happens at run
  time. A handler calling `os.system`, `os.popen`, or anything with `shell=True` fails
  registration (REQ-C-019)
- Children read `/dev/null` unless given `input=`, and get `NO_COLOR=1`, `PAGER=cat`,
  `GIT_PAGER=cat`, `MANPAGER=cat`, `LESS=-F -X -R`, and an empty `MORE`; off a terminal
  also `EDITOR`, `VISUAL`, and `GIT_EDITOR` set to `true`, so an editor exits at once
  (REQ-F-046, REQ-F-055). Off a terminal or under `CI` they also get `CI=1`,
  `NO_UPDATE_NOTIFIER=1`, and the npm, Homebrew, pip, and gh notifier switches, and
  `app.suppress_update_notifier(fn)` adds an app's own (REQ-F-050). Every child gets
  `LANG=C`, `LC_MESSAGES=C`, `LC_NUMERIC=C`, and `LC_CTYPE=C.UTF-8` (`C` where the
  platform has no `C.UTF-8`), with `LC_ALL` and the user's other `LC_*` variables
  removed, so messages are English, numbers and dates are C, and tools that need UTF-8,
  like Ansible, still start, unless the command has `preserve_locale=True` (REQ-F-066).
  Grandchildren inherit them;
  `env=` overrides single variables
- Children get the run's own temp directory as `TMPDIR`, `TEMP`, and `TMP`, and are
  listed in its `children.pids` while they run (REQ-F-030, REQ-F-032)
- A non-zero exit raises `SUBPROCESS_FAILED` (exit `1`) with `argv`, `returncode`,
  `stage`, and the last 4 KiB of stderr in `context`, with secret argument values and
  `ctx.token` redacted and the stderr marked external (see "Output security");
  `check=False` returns a `Completed` instead. In a pipeline any
  failing stage fails the whole, the first one named (REQ-F-065). `ctx.pipeline` checks
  each stage like `set -o pipefail`, except that a stage ended by SIGPIPE is not a failure
  when a later stage succeeded, so `yes | head -1` exits `0`
- `timeout=` defaults to what is left of the command's timeout and never exceeds it;
  running out stops the child and raises `TIMEOUT` naming the stage that hung. Each child
  starts in its own session, so it cannot open the terminal, and a signal or timeout sends
  SIGTERM to its process group, background grandchildren included, then SIGKILL after 2
  seconds, before the `CANCELLED` or `TIMEOUT` envelope is written (REQ-F-031); no child
  starts after that
- `ctx.run(argv, stream=True)` shows a long child's progress, such as
  `ansible-playbook` or `terraform apply`: each line it writes, on stdout or stderr, is a
  `ctx.log` INFO line as it arrives, secrets redacted (each line of a multi-line secret
  too, and a secret a 64 KiB line split cuts), so it shows on a terminal or with
  `--verbose`, and never on stdout, which keeps only the envelope. Off a terminal, in
  `App.call`, and over MCP, the lines are dropped as `ctx.log`'s are. `Completed.stdout`
  and `stderr`, and `SUBPROCESS_FAILED`'s `context.stderr`, keep the last 4096
  characters; the timeout, signals, locale, `input=`, and `children.pids` work as without
  it
- `ctx.run(argv, stream="always")` is for a child whose log is the progress an agent waits
  on: each line goes to stderr as plain text, redacted and escape-cleaned the same way, in
  any `--format` and verbosity; `--quiet` silences it, and `App.call` and MCP drop it. Two
  children streaming at once interleave whole lines. The command declares
  `@app.command(..., child_log=True)`, else the call raises `RegistrationError`, and its
  manifest entry has `stderr: "child_log"` (ManifestResponse 3.11); a passthrough command
  cannot declare it
- On Windows only the child itself is stopped, not its grandchildren

`App.main()` writes the same pager and, off a terminal, editor and update-notifier
settings into `os.environ` before any command runs, so programs and libraries started
without `ctx.run` inherit them too; treaty's own mode still reads `CI` as it was.

## Session hygiene

- `ctx.tmp_dir` is the run's private directory, `<temp>/<app>-<uid>/<request_id>/`
  (under `instances/<id>/` with `--instance-id`), `0700` whatever the umask, made on first
  use and removed when the run ends, signals included; `ctx.temp_file(".json")` makes a
  `0600` file in it, and `meta.session_tmp_dir` names it (REQ-F-032)
- `ctx.output_file("report.json", keep_seconds=300)` is a `0600` file for the caller that
  outlives the run; an object `data` gets `cleanup` with the shell `command` that deletes
  it and `auto_cleanup_after_seconds`, after which the next run of the tool deletes it,
  as does the `cleanup` built-in (REQ-F-043)
- `--cwd PATH` resolves relative `Path` arguments, `--input-file`, `--output`, the project
  config file, and `ctx.run` children under `PATH`, reported as `ctx.cwd` and `meta.cwd`;
  a missing directory exits `2` before anything runs. The process never changes
  directory: a handler that calls `os.chdir` is changed back with a `CWD_CHANGED`
  warning, and the `no-chdir` audit rule flags it (REQ-O-017, REQ-F-041)
- `App(update_check=checker)`, an object with `latest(current, timeout)`, sets
  `meta.update_available` when a newer release exists, from a cache a daemon thread
  refreshes daily, so no run waits. It runs only for a person at a terminal: never under
  `CI`, with `<APP>_NO_UPDATE`, or with `--no-update-check` (REQ-F-029, REQ-O-020)
- `cache=treaty.CachePolicy(ttl_seconds=3600)` gives `ctx.cache.get(key)` and
  `ctx.cache.put(key, data)`, bytes under `$XDG_CACHE_HOME/<app>/<command>/`, plus
  `--no-cache` and `--cache-ttl SECONDS` on that command only; `meta.cache_used` says
  whether a read hit (REQ-O-018)

A command that opens a browser declares `gui_operations=["browser_open"]` and
`headless_behavior=`, which registration requires (REQ-C-024). `ctx.open_url(url)` opens
it and returns `True`, except in a headless run (no terminal on stdin and stdout, `CI`, or
no `DISPLAY` or `WAYLAND_DISPLAY` on Linux or over SSH): then nothing opens, every
envelope of the run has `meta.headless: true` (REQ-F-057), and `headless_behavior` says
what happens instead:

- `"emit_in_output"`: the URL lands in `data.open_url`, so the output type needs an
  `open_url: str | None` field
- `"skip"`: `ctx.open_url` returns `False` with a `GUI_SKIPPED` warning naming the URL
- `"error"`: the run exits `4`, `PRECONDITION`, code `GUI_UNAVAILABLE`, with the URL in
  `error.context.url`

`ctx.headless` tells the handler.

## Network and filesystem

A `has_network_io=True` command gets `ctx.http`, a small stdlib client:

```python
response = ctx.http.get(url)  # or ctx.http.post(url, json={...}), ctx.http.request(...)
return Release(**response.json())
```

- It goes out through `HTTPS_PROXY` or `HTTP_PROXY` (either case), skips the proxy for
  hosts in `NO_PROXY`, sends Basic proxy auth from `user:password@` in the proxy URL, and
  verifies TLS against `REQUESTS_CA_BUNDLE`, else `SSL_CERT_FILE`, else the system store,
  all read from the run's environment (REQ-F-036)
- `--proxy URL` overrides the variables and `--no-proxy` connects directly; both reach
  `ctx.run` children too, and only network commands have them (REQ-O-019)
- Each request waits at most what is left of the command's timeout
- A failure ends the run with `error.network_context` (`url`, `proxy_used`, null when
  direct, `proxy_source`, `no_proxy`, `ssl_verify`, `status_code`, and a
  `curl -v [--proxy P] URL` suggestion, credentials removed): exit `12`
  `CONNECTION_FAILED`, and `12` `UPSTREAM_UNAVAILABLE` for 502 to 504, both retryable;
  `12` `TLS_VERIFY_FAILED`, not retryable until the CA bundle changes; `10` `TIMEOUT`,
  retryable on a `safe` command only; and, when the command declares the exit code,
  `8` `UNAUTHENTICATED` for 401, `7` `PERMISSION_DENIED` for 403, and `11` `RATE_LIMITED`
  with `retry_after_ms` for 429. Any other status is returned (REQ-F-037, REQ-F-063)
- On a `retry=` command, connection failures, timeouts, and 502 to 504 are retried within
  `--retries`, counted in `meta.retries`; a failure that outlasts those retries is not
  retryable, so an agent does not retry on top of them

A client of the handler's own, such as the `requests.Session` of a library the CLI wraps,
goes out the same way through `ctx.network`, a `treaty.NetworkSettings`:

```python
session = requests.Session()
session.trust_env = False  # treaty already read the environment
session.proxies = ctx.network.proxies  # {"http": ..., "https": ...}, {} under --no-proxy
bundle = ctx.network.ca_bundle
session.verify = True if bundle is None else str(bundle)
```

- `proxies` is the `requests`-style mapping of the schemes that go through a proxy:
  `--proxy` for both, else `HTTP_PROXY` and `HTTPS_PROXY` (either case); empty under
  `--no-proxy` or a `NO_PROXY` of `*`. A `host:port` value reads as `http://host:port`, and
  a value that is no http proxy URL exits `4` `PROXY_INVALID`
- `proxy_for(url)` is the proxy `ctx.http` would use for that URL, `NO_PROXY` applied, or
  `None` for a direct connection: for `httpx.Client(proxy=...)` or a per-request choice
- `ca_bundle` is the `Path` of `REQUESTS_CA_BUNDLE`, else `SSL_CERT_FILE`, or `None` for
  the system store
- A proxy URL keeps its `user:password@` for the client to authenticate with; the repr of
  `ctx.network` removes it
- The `http-client` audit rule advises on a network command whose handler reaches none of
  `ctx.http`, `ctx.network`, and `ctx.run`: a client of its own would not see `--proxy`

A `recursive_traversal=True` command gets `ctx.walk(root)`, which yields a
`treaty.WalkEntry(path, depth, is_dir, is_symlink)` per entry, depth first in name order,
plus `--no-follow-symlinks` and `--max-depth N` (default 50):

- A followed symlink back to a directory above it exits `4` `SYMLINK_LOOP` with `path`,
  `loop_target`, and `completed_count` in `error.context`; two links to one directory are
  fine (REQ-F-061)
- An entry deeper than `--max-depth` exits `4` `DEPTH_EXCEEDED` with `max_depth`, `path`,
  and `hint`, rather than leaving part of the tree out
- With `--no-follow-symlinks` symlinks are listed but never entered; the walk's `count`
  and `symlinks_skipped` are there for the result (REQ-O-040)

The `http-client` audit rule flags `urlopen`, `requests`, and `httpx` in a network command,
and `recursive-traversal` warns on a walk a circular symlink can loop: `shutil.copytree`,
recursive `glob`, and `os.walk`, `Path.walk`, or `rglob` told to follow symlinks. Without
that they cannot loop, nor can `shutil.rmtree`, and the rule only advises that no
`--max-depth` bounds them.

## Declarations

Optional keywords on `app.command` tell an agent, before it runs a command, what the
command needs and what it leaves behind. Each is in the manifest and `--schema`:

```python
from treaty import Background, Dependency, SideEffect, Subprocess

app = App("tool", version="1.0.0", dependencies=[
    Dependency("terraform", check_command=("terraform", "version"), min_version="1.5.0",
               fix_command="brew install terraform", version_regex=r"Terraform v([\d.]+)"),
])

@app.command(
    "package",
    description="Build a Debian package",
    danger_level="safe",
    exit_codes=(),
    platform=["linux"],
    required_tools={"dpkg-deb": "1.19.0"},
    subprocess=Subprocess("dpkg-deb", user_controlled_args=("source",), hardcoded_args=("--build",)),
    filesystem_side_effects=[SideEffect("~/.cache/tool/", "cache", ttl_seconds=3600)],
)
def package(args: PackageArgs, ctx: Ctx) -> Packaged: ...
```

- `subprocess=` names the child binary, the fields that become its arguments, and the
  arguments always passed (REQ-C-019). A declared field holding `; | & $ ( ) < >`, a
  backtick, a line break, or a leading `-` exits `2` with `SHELL_METACHARACTER` before the
  handler runs. Without it the manifest's `subprocess` is derived from `ctx.run([...])`
  list literals, unchecked, and the `subprocess-declared` audit rule flags a command whose argument list cannot be read, or that starts a program outside `ctx.run` (`subprocess.run` and its siblings, `os.exec*`, `os.spawn*`, however imported)
- `platform=` lists `sys.platform` values; elsewhere the command still runs, with an
  `UNSUPPORTED_PLATFORM` warning. `required_tools=` maps each program the command runs to
  its minimum version (REQ-C-018)
- `App(dependencies=[...])` lists the tool's external dependencies at the manifest root
  (REQ-O-031); `check_command` is an argument list, shown with `shlex.join`
- `doctor` checks every dependency and required tool: `data.dependencies` with
  `found_version` and `ok`, `data.checks` with each tool's `version`, `required`, and a
  `fix`. A failure exits `4` with `DOCTOR_CHECKS_FAILED`; a version above `max_version`
  is a `DEPENDENCY_ABOVE_MAX` warning. Every dependency is also a `data.checks` entry;
  `data.checks` also says whether the state and user config directories can be written,
  and runs `App(checks=[...])`: functions of the ctx returning `treaty.Check(name, ok, fix=..., version=, required=, error=)`, or
  `treaty.endpoint(url, fix=...)`, a GET through `ctx.http` so proxy variables apply,
  whose failure carries `network_context` (REQ-O-026). A failing `Check` without `fix` is
  `INVALID_OUTPUT`; the `doctor-fix` audit rule finds one
- `filesystem_side_effects=` lists where the command writes: `path` (absolute or `~/`,
  with `{placeholders}` matching any segment), `type` (`cache`, `log`, `temp`,
  `credential`, `config`, `output`), `ttl_seconds`, and `clearable_with`, an invocation
  checked to name a command (REQ-C-011). `output` is where the command writes its product,
  such as a rendered dashboard, including its default when `--output` is absent: it takes
  no `ttl_seconds` or `clearable_with` (a `RegistrationError`), and a per-call `--output`
  path is declared by `output_file=` instead. `cleanup` (destructive; `--dry-run` lists) removes every
  declared `temp`, `cache`, and `log` path, the caches, and handed-out output files;
  `--scope temp|cache|logs` narrows it, `--min-age SECONDS` keeps anything changed more
  recently (listed under `skipped`), and `data.cleaned` gives each path's `type` and
  `bytes_freed`, with `total_bytes_freed` (REQ-O-027). A path it cannot remove is listed
  in `data.failed` with a `CLEANUP_INCOMPLETE` warning. `credential`, `config`, and
  `output` paths, and a declared path holding one, are never removed under any `--scope`, nor a match reached through a symlink a placeholder matched; a path
  of just `/` or `~/` plus a placeholder, or with a `..` segment, is refused
- A path under the project starts with `{project_root}/`, such as
  `SideEffect("{project_root}/tmp/dashboard/", "output")`, on a command declaring
  `project_root=` markers (without them it is a `RegistrationError`). `cleanup` and
  `status` find the project from their own cwd (or `--cwd`) up, as the command does; with
  no marker found (one at `/`, in the home directory, or above it counts as none) they
  touch nothing of it and warn `PROJECT_ROOT_NOT_FOUND`, never taking the cwd for the
  project, and a match whose directory resolves outside the project
  through a symlink is left alone. A safe command whose writes are all declared, this way
  or another, passes the `fs-side-effects` audit rule; writing `cache`, `log`, `temp`, or
  `output` paths keeps a command `safe` (REQ-C-002)
- `status` (safe, exits 0 whatever exists) lists every declared side effect with the
  absolute paths it matches and their sizes (`--show-side-effects`), and the state files,
  config files, idempotency records, the audit log, and declared `credential` and `config`
  paths, with `purpose`, `exists`, and `bytes`, never their values (`--show-state-files`);
  with neither flag, both. `App(credentials=)` adds `logged_in`, and `status --show-config`
  is the global `--show-config` (REQ-O-028)
- `background=Background("tool stop-watcher", max_lifetime_seconds=3600)` allows
  `ctx.spawn(argv)`, which starts a child in its own session with output to a log under
  the state directory and leaves it running when the run ends; the output carries
  `background_pid` and `cleanup_command` (REQ-C-010). A later spawn of the command stops
  entries past their lifetime

## Prompts

Nothing waits for input an agent cannot give. A command that asks a person declares
`interactive=True`, which adds `--yes` and `--non-interactive`, and asks through `ctx`:

```python
@app.command("init", description="Create a project", danger_level="safe", exit_codes=(),
             interactive=True)
def init(args: InitArgs, ctx: Ctx) -> Project:
    name = args.name if args.name is not None else ctx.prompt("Project name", flag="name")
    return Project(name=name, overwrite=ctx.confirm("Overwrite existing files?"))
```

- `ctx.prompt(text, flag=...)` asks only when stdin and stdout are terminals and
  `--non-interactive` is absent; otherwise the run exits `4` with `INPUT_REQUIRED`, the
  prompt in `context`, and a suggestion naming `--<flag>` (REQ-F-009, REQ-C-005)
- `ctx.confirm(text)` returns `True` under `--yes` without asking; off a terminal without
  it, exit `4` suggests `--yes`. `--yes` on an interactive command that never asks changes
  nothing
- `ctx.edit(initial)` opens `$VISUAL` or `$EDITOR` on a terminal. The command declares
  `editor_alternatives=["message"]`, the flags that replace the editor; off a terminal
  the run exits `4` with `EDITOR_REQUIRED` and those flags in `error.alternatives`
  (REQ-F-055, REQ-C-023)
- When no one can answer, `input()` exits `4` with `INTERACTIVE_BLOCKED`, even inside
  `except Exception` (REQ-F-047): a terminal stdin is never read, and an empty stdin
  (`/dev/null`) fails its first `readline()`. Piped data reads as usual through `input()`,
  `readline()`, `read()`, `readlines()`, iteration, and `fileinput`

Calling `ctx.prompt`, `ctx.confirm`, or `ctx.edit` without the declaration is a
`RegistrationError` when the handler's source shows it. The manifest lists
`interactive`, `requires_editor`, and `non_interactive_alternatives`; exit `4` is in
every command's map. Running with no arguments prints help and exits `0`; treaty has no REPL.

## Output size

JSON output is capped at 1 MiB per envelope: `App(max_output_bytes=...)` app-wide,
`<APP>_MAX_OUTPUT_BYTES` in the environment, or the global `--max-output` flag, in increasing precedence. Past the cap
the framework follows whichever child holds most of the bytes and cuts the list, object, or
string where no child dominates to the longest prefix that fits. `meta` gets `truncated`,
`total_bytes`, and a `truncation_hint` that is a command to run as given (plus
`total_count` and `returned_count` when `data` is a list), and each cut adds a
`FIELD_TRUNCATED` warning naming the field. For a list command whose page was cut the hint
is the next page, `--limit <kept> --cursor <token>`, and `meta.pagination` points there
too; otherwise it is the same command with a `--max-output` 1 KiB above the full size,
since a rerun's `meta` can be a few bytes longer. `--limit` and `--cursor` on the command
line win over the same keys in a `--raw-payload`, so a hint appended to such a call runs.
Plain mode is not capped. A value a backend already cut goes through
`ctx.truncated(text, field="body", original_length=4200)`, which appends the marker, adds
a `FIELD_TRUNCATED` warning on `data.body`, and sets `meta.truncated`.

## Lists

Every command that returns `list[T]` or `treaty.Page[T]` is a list command (REQ-F-018),
with no declaration: it gets `--limit` (default 20, `default_limit=` per command, `0` for
every item) and `--cursor`. `paginated=False` opts a small, bounded `list[T]` out, and the
`paginated-list` audit rule advises on each opt-out; streams are never paginated.
Every successful response carries `meta.pagination` with `total`, `returned`, `truncated`,
`has_more`, and `next_cursor`; pass `next_cursor` as `--cursor` for the next page. Text
formats carry no `meta`, so a cut page writes `20 of 25 shown; next page: --cursor ...,
or --limit 0 for all` to stderr (`--format id` writes `next: --cursor ...`):

```python
@app.command("releases.list", description="List releases", danger_level="safe",
             exit_codes=())
def releases(args: NoArgs, ctx: Ctx) -> list[Release]:
    return store.all()  # the framework slices it
```

A source too large to load returns one batch as a `Page`, reading `ctx.page`:

```python
def releases(args: NoArgs, ctx: Ctx) -> Page[Release]:
    rows, after = store.page(after=ctx.page.cursor, limit=ctx.page.limit)
    return Page(items=rows, next_cursor=after, total=store.count())
```

The framework slices whatever it gets to the limit, so a batch larger than asked is fine.
Its own cursor is URL-safe base64 naming the command, a digest of the other arguments,
the handler's cursor, and how many items of that batch were already delivered; one that
does not decode, names another command, or came from other arguments (`--q a`, then
`--q b`) exits `2` with `INVALID_CURSOR`. `cursor_check=` validates the handler's own
cursor before the handler runs: a pure function that takes the string and raises
`ParseError` to refuse it, which also exits `2` with `INVALID_CURSOR`. JSON routes take
`limit` as an integer only. In `exec`, `_opts` take `limit` and `cursor`; MCP
tools take them as arguments. `--schema` shows `default_limit`. Streams are not paginated.

## Secrets

A field declared `Flag(secret=True)`, or whose name contains `token`, `secret`, `password`,
`key`, `credential`, `auth`, or `cookie` anywhere (so `author` too), or has a `pass`
segment, never takes its value on the command line (REQ-C-016). A boolean or an enum is never inferred a secret, and `secret=False` opts a field out. The
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

A flag can also read the variables a wrapped service already uses, secret or not:

```python
@dataclass(frozen=True, slots=True)
class Download:
    token: str = Flag(description="Flex Web Service token", env=("IBKR_FLEX_TOKEN",))
    query_id: str = Flag(description="Flex Query ID", env=("IBKR_FLEX_QUERY_ID",))
```

When the flag is not passed (on argv, in `--raw-payload`, an `exec` line, or an MCP call),
it reads `<APP>_<NAME>` and then the declared names in order, so the variable set for this
tool always beats one shared across tools (REQ-F-073); a flag without `env=` reads no
variable. A value read so is validated like a passed one, its error names the variable in
`context.source`, and a secret's stays redacted everywhere. A secret's names join
`secret_env_vars` in the manifest; a plain flag lists its names in `env_vars`,
`<APP>_<NAME>` first and each deprecated one marked, and a required one is listed
`required: false`, as a variable may supply it. The manifest's root `env_vars` lists the
variables that back no flag, such as `<APP>_AUDIT_LOG` and each setting's. `--help`, AGENTS.md,
and the `env-prefix` audit rule know them all, and `EnvName(..., deprecated=Deprecated(...))`
works as on a setting. Flags of different commands may share a name, unless one reads it
as a secret or token and the other as a plain value; two flags of one command, a flag and
a setting, or a name treaty reads itself may not, and that holds for a flag's own
`<APP>_<NAME>` too: a `--state-dir` with `env=` or a `--region` beside a `region` setting
is refused.

## Credentials

Treaty never stores or refreshes a token. The app tells it which scopes the active
credential holds, `None` when no one is logged in, or `treaty.Expired(at=...)` when the
credential has expired:

```python
class Keychain:
    def active_scopes(self, ctx: Ctx) -> Iterable[str] | Expired | None: ...

app = App("authctl", version="1.0.0", credentials=Keychain())

@app.command("repos.list", description="List repositories", danger_level="safe",
             exit_codes=(), requires_auth=True, required_scopes=["repo:read"])
```

A `requires_auth=True` command must list its scopes (REQ-C-029). Before its handler runs,
no credential exits `8` with `UNAUTHENTICATED` and `hint` naming the login command; an
expired one exits `8` with `CREDENTIALS_EXPIRED`, `expires_at`, and `refresh_command`
(the command registered with `refreshes_auth=True`); a missing scope exits `7` with
`PERMISSION_DENIED`, `required_permission`, and `context.missing_scopes` (REQ-F-063).
Scopes beyond the required ones add a `CREDENTIAL_OVER_PRIVILEGED` warning (REQ-O-047). `check-permissions --for <command>`
reports `required_scopes`, `active_scopes`, and `over_privileged`; without `--for` it maps
every gated command to its coverage. The `broad-scope` audit rule flags `admin`, `owner`,
`root`, and `*` scopes the description does not name.

A login command declares `auth="browser"` or `auth="device"` (REQ-C-021). Both get
`--headless` and `--token-env-var NAME` (REQ-O-033) and receive a pre-acquired token as
`ctx.token`, read from `NAME` or the first set variable of `token_env_vars` (`<APP>_TOKEN`,
then any `token_env_vars=` the command adds), and redacted from logs and tracebacks. A
browser login in a headless run (no terminal, `CI`, or `--headless`) without a token exits
`4` with `TOKEN_REQUIRED`, the variables in `context.token_env_vars`, and `auth_methods`.
`--schema` shows `headless_supported` and `token_env_vars`; `ctx.warn(code, message,
**context)` adds a warning to any command's response.

## Async jobs

A command that starts work it does not wait for declares `async_job=True` and returns a
`treaty.Job` (REQ-C-022); the app answers for its jobs through `App(jobs=...)`, an object
with `status(job_id, ctx)` and `cancel(job_id, ctx)` that each return a `Job` or `None`:

```python
app = App("deployctl", version="1.4.0", jobs=Deployments())

@app.command("deploy.start", description="Start a deployment", danger_level="mutating",
             exit_codes=(), async_job=True)
def start(args: Start, ctx: Ctx) -> Job:
    return Job(backend.submit(args.service), "running", effect="created")
```

`data` gets `terminal`, `status_command`, and `cancel_command` next to the `Job` fields, and
`--schema` shows `async: true` and `job_descriptor_schema`. `job status <id>` exits `0` when
the job is complete, `3` (`JOB_RUNNING`, with `poll_interval_ms`) while it runs, `4` when it
failed or was cancelled, and `5` for an unknown id; `job cancel <id>` asks it to stop.

## Config writes

A command that changes a config file declares where (REQ-C-025) and writes through
`ctx.write_config(text)`. `config_write_scope="local"` writes `./.<app>.toml` in the working
directory, or with `--global` the user file `$XDG_CONFIG_HOME/<app>/config.toml`
(`~/.config/<app>/config.toml`); `"global"` writes only the user file and exits `2` without
`--global`. `ctx.config_path` names the file. Every write goes to a temporary file in the
same directory and is renamed over the target, so an interrupted write leaves the old
file; every write takes an advisory lock, so concurrent writers go one after another, and a
global write adds a `GLOBAL_CONFIG_MODIFIED` warning. Under `--config PATH` the command
writes that file instead, and under `--instance-id ID` the user file is
`<config home>/<app>/instances/<ID>/config.toml`. The `config-write-scope` audit rule flags
`config` and `set` commands without the declaration.

## Settings

An app reads its config through one frozen dataclass whose fields all have defaults; a
handler asks for it by annotating a parameter with the class, as with a resource:

```python
@dataclass(frozen=True, slots=True)
class Settings:
    region: str = "us-east-1"
    retries: int = 3

app = App("deployctl", version="1.0.0", settings=Settings)

@app.command("deploy", description="Deploy", danger_level="mutating", exit_codes=())
def deploy(args: DeployArgs, ctx: Ctx, settings: Settings) -> Deployed: ...
```

Each field takes the first value found: `DEPLOYCTL_REGION` in the environment, then the config files, then its default. A field declared `Flag(default=..., description=..., secret=True)` is a secret setting, redacted by `--show-config` and left out of the config hash, whatever its name; a relative `Path` setting is resolved against the run's directory, `--cwd` included. A field may name a class registered with `app.scalar`, after `App(settings=)` as well: the field types are checked when the app is first used (a run, `call`, `manifest`, or `treaty audit`), and a value is checked against the scalar's `pattern` or bounds and parsed by its `parse`. The files are `--config PATH` alone (TOML, or JSON by the
`.json` suffix; a file not written yet reads as empty), or else the project file
`./.deployctl.toml` over the user file. A file may hold `[contexts.<name>]` tables over its
top level, chosen by `--context NAME` or its `current_context` key. `--no-config` reads no
file, and env vars still apply. An unknown key, a wrong type, or bad TOML exits `2` with
`CONFIG_INVALID` naming `context.path` and `context.key`; an unknown context exits `2` with
`CONTEXT_UNKNOWN` and `context.available`. `DEPLOYCTL_CONFIG`, `DEPLOYCTL_CONTEXT`, and
`DEPLOYCTL_INSTANCE_ID` stand in for the flags, as they must for `App.call` and MCP.

A field can also read a variable its users already export, outside the prefix:
`Flag(default=..., description=..., env=("BEANCOUNT_FILE",))` reads the names in order
after `<APP>_<FIELD>` and before the config files. The prefixed name still wins. Such a
name shows in `--help`, in AGENTS.md, and as `env:BEANCOUNT_FILE` in `--show-config`, is
validated as strictly as the prefixed one, with errors naming it, and satisfies the
`env-prefix` audit rule. `EnvName("OLD_NAME", deprecated=Deprecated("1.4.0"))` keeps reading
an old name, with a `DEPRECATED_ENV_VAR` warning naming the replacement: `<APP>_<FIELD>`, or
the declared name `Deprecated(replacement=)` gives. A name is refused at registration when it
is not a variable name, repeats, is the field's own `<APP>_<FIELD>`, or is read for another
field or a framework option.

Every response carries `meta.config_sources` (the files read, highest first; `[]` when
none) and `meta.effective_config_hash` (12 hex of the merged settings), plus
`meta.context` and `meta.instance_id` when set. `tool --show-config` answers with
`effective_config` (secret-named fields `[REDACTED]`), per-key `sources` (`env:VAR`,
`file:/abs/path`, or `default`), and `precedence_order`. The `settings-declared` audit rule
flags handlers that parse a config file with `tomllib` themselves.

## First-run setup

Treaty does no first-run work of its own: reading config creates nothing, and the state
directory appears only when a keyed run needs it. An app whose setup can fail (a directory,
a keypair, a download) passes it as `App(init=Setup())`, an object with
`initialized(ctx) -> bool` and `run(ctx) -> None`. That adds an `init` built-in, which exits
`0` with `already_initialized: true` once set up; until then every other command exits `4`
with `INIT_REQUIRED` and `fix_command: "<app> init"`. An `OSError` from `run` exits `1`
with `INIT_FAILED` and `context.reason`: `permissions`, `network`, `disk`, or `io`. The
`init-isolated` audit rule flags handlers that `mkdir` or write under an
`if not path.exists():` guard.

## Environment variables

Every variable treaty reads carries the app's prefix (`DEPLOYCTL_FORMAT`,
`DEPLOYCTL_MAX_OUTPUT_BYTES`, `DEPLOYCTL_STATE_DIR`, ...), and `--help` lists them under
Environment; in the manifest, a flag's `env_vars` names the variables it reads, the root
`env_vars` the others, and the root `secret_env_vars` each secret setting's. Unprefixed, treaty reads only shared conventions:
`CI`, `NO_COLOR`, `TERM`, `COLUMNS`, `HOME`, `XDG_*`, `GITHUB_ACTIONS`, `JENKINS_URL`, the
proxy and CA bundle variables, and `TOOL_TRACE_ID`. The `env-prefix` audit rule flags handlers that read an unprefixed
variable such as `DEBUG`.

## Validation errors

Phase 1 keeps going past a bad value, an unknown flag, or a refused secret, so one run
reports every argument error (REQ-F-015). The envelope's `error.errors` lists each one with
its `field`, `message`, and `context`; with several, the headline `message` is
`Validation failed: N errors` and `context.fields` names them. A single error keeps its own
message and context and lists itself. Framework flags (`--timeout`, `--idempotency-key`,
a repeat with a different value) and a flag with no value at the end are collected the same
way; only invalid `--raw-payload` JSON stops parsing at once.

A command path that matches nothing exits `2` with `context.available`, the invocations
under the group it was typed in. When a registered name is close, `context.did_you_mean`
lists up to three invocations, best first, and `suggestion` names them: `deployctl rollbak`
and `deployctl deploy.rollback` (the registry key, as agents copy it from the manifest) both
suggest `deployctl deploy rollback`. A name is close within one typo per three characters
typed, a swap of neighbors counting as one, or when it starts with what was typed; two
characters get no guess. `App.call`, `exec`, MCP, and `check-permissions --for` suggest the
dot path the same way on `UNKNOWN_COMMAND`.

## Paths

A field annotated `pathlib.Path` (or `Path | None`, `tuple[Path, ...]`) reaches the handler as a
`Path` and is listed in the manifest with `pattern_type: "filepath"`. Before any handler runs,
on argv, `exec`, and `--raw-payload` alike, the framework rejects the agent hallucination
patterns of REQ-F-045 with exit `2`: any `..` segment, a percent-encoded sequence such as
`%2e%2e` or `%2f`, and null bytes. The error carries `rejected_pattern` in `context` and a
`suggestion` with the decoded or absolute form, so `../out.json` is refused but
`/abs/out.json` passes unchanged. `pattern=` is not allowed on `Path` fields. The audit rule
`path-typed` warns about `str` fields whose name looks like a path. On the way out, every
`Path` in `data` is absolute: a relative one is joined to `meta.cwd`, without resolving
symlinks, so `Path("./src/.toolrc")` is written `/project/src/.toolrc`.

## Decimals

A field annotated `decimal.Decimal` (or `Decimal | None`, `tuple[Decimal, ...]`) reaches the
handler as a `Decimal` parsed from fixed-point text, never through a float, so `--amount
12.30` is `Decimal("12.30")` and is written back as `"12.30"`. The text is an optional `-`,
digits, and an optional fraction: `1e3`, `NaN`, `Infinity`, `.5`, `+1`, and `1,5` exit `2`
naming the flag. `-0` and `-0.00` are zero, so a handler never sees a signed zero. In
`exec` lines and `--raw-payload` the value is a JSON string, or a JSON number read from the
digits as written: `{"amount": 12.30}` is `Decimal("12.30")`. A float from anywhere else,
such as an MCP client's arguments or `app.call(...)`, is refused, since it may already have
lost digits; send a string. The argument's schema is `{"type": "string", "pattern":
"^-?[0-9]+(\\.[0-9]+)?$", "format": "decimal"}`, an output field's the same without
`format`, and the manifest lists the flag as a `string` with that `pattern`. A default is
taken as an argument would be, so `Decimal("-0")` is `0`. An app that registers
`app.scalar(Decimal, ...)` gets its own parsing and schema instead.

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

## Object arguments

A flag can take a structured record: annotate it with a frozen dataclass, `X | None`, or
`tuple[X, ...]` for a list of them. The dataclass's own fields are plain fields, or carry
`Flag(...)` for a description, `pattern`, `pattern_type`, `multiline`, `max_bytes`, or
`secret=False`; they may be any argument type, other objects and lists of objects among them,
up to 8 deep:

```python
@dataclass(frozen=True, slots=True)
class Posting:
    account: str
    number: Decimal
    memo: str | None = None


@dataclass(frozen=True, slots=True)
class AddArgs:
    postings: tuple[Posting, ...] = Flag(default=(), description="Postings")
```

`exec` lines, `--raw-payload`, `app.call(...)`, and MCP carry the value as a JSON object
(`{"postings": [{"account": "cash", "number": "12.30"}]}`); on argv each `--postings` takes
one JSON object, repeated for a list, and `from_stdin=True` reads one object per line. Each
field is checked as a flag's value is (the `Decimal`, `Path`, enum, `Literal`, and
`app.scalar` rules included), an unknown or missing key is refused, and every error is one
entry in `error.errors` at its location, such as `postings[1].number`, exit `2`. The
dataclass's `__post_init__` runs in phase 1 too. `--schema` and the MCP `inputSchema`
carry the object's schema; the manifest lists the flag as an `object` (an `array` for a
list) whose `schema` is one value's (ManifestResponse 3.7), `--help` shows its shape, and
completion offers no values for it. An object travels on argv, so it holds no secret (REQ-C-016): a field under a
secret's name, such as `postings[].token`, or declared `secret=True`, fails registration;
make it a top-level flag, read from `--x-from-env` or `--x-from-file`, or declare
`secret=False` when the name misleads. An object flag cannot be positional, a secret, or a
setting, and a value class meant to travel as one string, such as an id, still needs
`app.scalar(...)`: an unregistered frozen dataclass on a flag is an object.

## Output adapters

A handler may return a class treaty does not know, such as a pydantic model, once the app
registers how that class family is described and written. One registration covers every
subclass of `base`, wherever it sits: the return value, a dataclass field, `list[Model]`,
`tuple[Model, ...]`, or `Model | None`. treaty imports nothing from the library:

```python
from pydantic import BaseModel

app.output_adapter(
    BaseModel,
    schema=lambda cls: cls.model_json_schema(mode="serialization"),
    dump=lambda obj: obj.model_dump(mode="json", by_alias=True),
)
```

`schema(cls)` gives the output schema and `dump(obj)` the value in `data`; the dump passes
`by_alias=True` because `model_json_schema` names a field by its alias. treaty inlines
the schema's `$defs`, writes a fixed tuple's `prefixItems` as draft-07 `items`, and makes it
read as a dataclass's does: every key required and no other key allowed. A dump that leaves
out a key, or writes one a closed schema does not list, fails the run. The stable-output rules are checked on the schema when a command
names the class: a null list or dict is a registration error (REQ-F-074) unless the adapter
passes `none_as_empty=True`, which writes it as `[]` or `{}` and drops `null` from the
schema. A property declares the `Out` options as schema keys, for pydantic through
`Field(json_schema_extra={...})`: `x-sort-key` (the item field an array is sorted by),
`x-ordered`, `x-volatile`, `x-high-entropy` (`True` or `False`), and `x-external`. Each is
optional: an undeclared array is sorted by its items' JSON text and `treaty audit` advises
declaring its order. A `list[Model]` or `tuple[Model, ...]` a command returns or a dataclass
field holds is ordered as a list of dataclasses is, by `sort_key=` or `ordered=True` (or
`Out(...)` on the field), and `stable-order` warns when it declares neither. A
credential-named or `format: password` string is masked unless `--unmask`. A field type
with its own JSON schema, such as a constrained `str`, needs no `app.scalar`. A mutating or
destructive command may return an adapted class whose schema lists `effect` (and
`would_affect`), checked on each run as a dataclass's are; the key may not be
`x-volatile`. Register the adapter before the commands returning the class; two adapters
whose bases overlap, or an adapted class registered as a scalar, are registration errors.

## Args models

A handler's arguments can be a model class other than a dataclass, such as a pydantic
`BaseModel` a shared package already defines, once an args adapter covers it. treaty
imports no model library; the two functions are the contract:

```python
app.args_adapter(
    BaseModel,
    schema=lambda cls: cls.model_json_schema(by_alias=False),
    validate=lambda cls, data: cls.model_validate(data, by_name=True, by_alias=False),
)
```

`schema` returns the class's JSON Schema, and its properties become the flags: `required`,
`default`, `description` (else `title`), string `enum` and `const`, arrays of these,
`format: path` as a `Path`, a number-or-string union as a `Decimal`, `format: password` or
`writeOnly` as a secret, and a `treaty` key for what JSON Schema has no word for, such as
`{"treaty": {"positional": true}}` or `{"treaty": {"short": "q"}}`. A property neither
required nor defaulted, such as a `default_factory`, is left to the model. Phase 1 parses
argv, `--raw-payload`, `exec` lines, `app.call(...)`, and MCP calls as it parses an args
dataclass, then hands the values to `validate` as JSON values; the handler, its resources,
and its rollback receive what it returns. A `ValueError` it raises exits 2 with one
`error.errors` entry per item of its `errors()` list (pydantic's `ValidationError` has one),
at the item's `loc` with its `type` and `input`, never a secret's; one with no `loc`, such
as a `model_validator`'s, names no field. A dry run treaty switches on, such as a
destructive command's preview without `--confirm-destructive`, calls `validate` again in
phase 1 with the model's `dry_run` field on, and a refusal there is answered the same way.
`--schema`, `--help`, the manifest, and completion
come from the same flags. A nested model, a dict, or a union of two types has no flag form
and fails registration, as do two adapters covering one class and an adapter's class
registered with `app.scalar`.

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

@app.command("deploy", description="Deploy a component", danger_level="mutating",
             exit_codes=["NO_PROJECT"])
def deploy(args: DeployArgs, ctx: Ctx, config: Config, project: Project) -> Receipt: ...
```

`acquire` takes the same `(args, ctx)` as a handler plus, optionally, other resources by
annotation, so resources compose. Each class is acquired at most once per run and shared,
in dependency order, and acquisition runs under the command's timeout. A `CliExit` raised
inside `acquire` becomes that exit's envelope and a `ParseError` becomes
`VALIDATION_AFTER_START` (exit `1`), so a missing project fails before any handler runs. A class without a classmethod `acquire`, a
missing `Ctx` annotation, or a dependency cycle is a registration error. Resources are not
part of the manifest: the flags they read, such as `--project`, live on the args dataclass,
typically a `kw_only=True` base class shared by every command. Resources must not change
process state such as the working directory, because `exec` runs many requests in one
process.

A resource that holds something, such as a connection or a temp file, defines
`def release(self) -> None`. When the run ends, by any exit (a result, an error, a
timeout, or a signal), treaty releases each acquired resource, newest first, then calls the
command's `cleanup=` hook (REQ-C-017). The teardown runs once per run, however many exit
paths race, so a hook written to be safe twice is never needed twice. A hook that raises
leaves its traceback on stderr and a `CLEANUP_FAILED` warning naming it; the exit code
stays. After a timeout the handler gets two seconds to finish before its teardown runs
beside it. The `resource-release` audit rule flags a resource class with `close`,
`__exit__`, `terminate`, or `unlink` but no `release`.

## Streaming

A command declared `streaming=True` has a generator handler annotated `Iterator[T]`, and
every yield is one JSONL envelope line with `meta.seq` counting from 1. The stream ends
with a terminal envelope that has `data: null`, `meta.end: true`, and `meta.total`, so an
agent can tell a clean end from a killed process:

```python
@app.command("dashboard.serve", description="Serve the dashboard", streaming=True,
             danger_level="safe", exit_codes=(), cleanup=stop_server)
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
A stream's timeout, the app default unless `timeout=` or `--timeout` says otherwise, limits
the wait for each event, so a stream runs as long as it keeps producing and one that goes
silent ends with `TIMEOUT` (REQ-F-011); `--schema` says `timeout_kind: idle`. Under
`--no-stream` and in `App.call` it is a deadline for the whole stream. Cancellation runs `cleanup=` and the handler's `finally`
blocks, then ends the stream with the normal `CANCELLED` envelope and exit `130` or `143`.
The manifest declares `streaming_default: true` and a `--no-stream` flag (REQ-O-004),
which returns one envelope with every event in `data` and `meta.total`; a failure under
`--no-stream` keeps the events seen so far in `data`. In `exec`, each event line carries
`_line` and `_cmd`. In a text format the renderer gets one event per call.
Summary lines carry `meta.pagination`; `--stream` on a command that cannot stream answers
with one envelope and a `STREAMING_NOT_SUPPORTED` warning.

A stream is `safe` or `mutating`; a `destructive` one is refused at registration, since a
stream cannot ask confirmation for each action. A mutating stream, such as a watch loop
that acts on what it sees, reports what it did event by event: each event's `data` carries
its own `effect`, checked as a single response's is, and the terminal envelope counts them
in `meta.effects` (`{"created": 2, "noop": 1}`), as does `--no-stream`. Its dry-run flag
covers the whole stream: every event reports a `would_*` effect (`would_noop` for one that
changes nothing), and every line carries `meta.dry_run: true`. A stream has no idempotency
replay, so it takes no `--idempotency-key` and an `<APP>_SESSION` repeat runs again; a
mutating stream that fails after a live effect other than `noop` ends with `retryable:
false`, since its events already applied. The audit log writes one entry per run, with the
counts in `effects`.

```python
@app.command("operator.run", description="Act on alerts each pass", streaming=True,
             danger_level="mutating", exit_codes=())
def operate(args: OperatorArgs, ctx: Ctx) -> Iterator[Pass]:
    while True:
        receipt = None if args.dry_run else write_receipt(alerts())
        yield Pass(effect="would_create" if args.dry_run else "created", receipt=receipt)
        time.sleep(args.interval)
```

## Asking for less output

Every command takes these without any code of its own:

- `--fields id,name` keeps those top-level keys of `data` (or of each item of a list);
  `ok`, `error`, `warnings`, and `meta` are never filtered, and `meta.fields` lists the
  names kept (REQ-O-002). `--format id` keeps the id field whatever `--fields` names
- `--token-limit N` cuts `data` to N tokens on item and field boundaries, with
  `meta.truncated`, `meta.token_limit`, and `meta.next_token_offset`; `--token-offset N`
  starts the window there, and `--token-count` runs the command and returns only
  `meta.token_count`. `--tokenizer` picks `approx` (bytes over 4, the default),
  `cl100k_base` or `o200k_base` with `treaty[tiktoken]`, or one from
  `app.tokenizer("name", count=fn)` (REQ-O-049)
- `--format id` writes the primary id alone, one per line, for piping into another
  command's `--flag -`; an output with an `id` field offers it, and `id_field="user_id"`
  names another (REQ-O-005)

A `heartbeat=True` command also takes `--heartbeat-interval SECONDS`, which writes
`[12s] <status>` on stderr from the latest `ctx.progress("...")` call (REQ-O-012).

## Destructive commands

A command with `danger_level="destructive"` must declare a boolean `dry_run` field, or mark
its own boolean flag with `Flag(dry_run=True)` (a wrapped tool's `--check` or `--noop`), and its
output type a `would_affect` field (`would_affect: Affects | None = None`). A dry run returns
`treaty.Affects(summary, resources, count)` there: a line for a person and the identifiers
for a program; a dry run without it exits `1` with `INVALID_EFFECT` (REQ-C-004). Without
`--confirm-destructive` the framework runs it in dry-run mode and exits `2` with error code
`CONFIRMATION_REQUIRED`, whose message carries the summary and whose `data` is the preview.
`--schema` of a destructive command has `requires_confirmation: true` (REQ-O-021).

`safe_default=True` makes the dry run the default instead (REQ-O-048): without `--live` the
command previews and exits `0`, and `--live` applies it: `--live` is the explicit
confirmation, so `--confirm-destructive` is not also needed. `--schema` still shows
`requires_confirmation: true`. Every response of such a command carries `meta.dry_run`,
and a live one `meta.confirmed`; the manifest shows `safe_default: true` and the `--live`
flag.

## Effects and idempotency keys

Mutating and destructive commands return an object with an `effect` field: `created`,
`updated`, `deleted`, or `noop` on a live run, and a `would_*` value such as `would_delete`
on a dry run. Registration fails when the output type cannot carry the field, and a run
that reports the wrong kind of value exits `1` with `INVALID_EFFECT`. A run that reports a
`would_*` effect also carries `meta.dry_run: true`, so an agent knows nothing changed
without reading `data`.

A mutating command whose own flag runs it, such as `--yes`, marks it
`yes: bool = Flag(confirm=True, description="Apply it")`: without the flag the run is a dry
run under the same contract (a `would_*` effect, `meta.dry_run: true`, not stored under an
idempotency key), and with it the command runs. The flag keeps its name, its default is
`False`, and a missing value is a preview on every input path: argv, `--raw-payload`,
`exec` lines, and `App.call` and MCP. `exec --dry-run` previews even a line that passes
it. The manifest names the flag in the command's `confirm_flag` (ManifestResponse 3.14),
and the audit log records the preview with `dry_run: true`. A safe or destructive command, a
non-boolean field, `env=` (it is passed on each run, never read from a variable), or a
command that also declares a dry-run switch is a `RegistrationError`; a destructive command previews through `dry_run` and
`--confirm-destructive`, or `safe_default=True`.

The framework gives those commands `--idempotency-key` (also `idempotency_key` in `exec`
lines and `--raw-payload`). A successful live run is stored under the key; repeating the
call returns the stored `data` with `effect: "noop"` and `meta.idempotency_hit: true`
without running the handler, and reusing the key with different arguments exits `6` with
`IDEMPOTENCY_KEY_REUSED`. Failures and dry runs are never stored, a concurrent retry waits
for the first call, and records expire after 24 hours. Records live in
`App(state_dir=...)`, else `$<APP>_STATE_DIR`, else `$XDG_STATE_HOME/treaty/<app>`,
else `~/.local/state/treaty/<app>`. Handlers read the key as `ctx.idempotency_key` to pass
it on to an upstream API.

Without a key, repeats run again, unless `$<APP>_SESSION` names an agent session: then
the key is derived from the session, the command, and its arguments, reported in
`meta.idempotency_key`, and a repeat in the same session is a `noop` (REQ-C-007).

## Multi-step commands

A command whose work is ordered steps declares them, and its handler calls `ctx.step`
before each one:

```python
@app.command("migrate", description="Migrate the database", danger_level="mutating",
             exit_codes=["DISK_FULL"], steps=["backup", "apply_schema", "migrate_data"],
             resumable=True, rollback=restore_backup)
def migrate(args: MigrateArgs, ctx: Ctx) -> Migrated:
    if ctx.step("backup"):
        backup_database()
    if ctx.step("apply_schema"):
        apply_schema_changes()
    if ctx.step("migrate_data"):
        migrate_data()
    return Migrated(effect="updated")
```

`ctx.step(name)` completes the step in progress and starts `name`; the last step completes
when the handler returns, and a step that is undeclared, repeated, or out of order exits
`1` with `INVALID_STEP`. The manifest lists `steps`, and every response's `data` carries
`completed_steps`, `failed_step`, and `skipped_steps`, on success, failure, timeout, and
signal alike (REQ-C-008). A failure after a completed step exits `3`, `PARTIAL_FAILURE`,
keeping the handler's `error.code`, with `data.partial: true`; a timeout or signal keeps
its own exit. Each step logs `step started` and `step completed` to stderr, and heartbeat
lines name the step in progress.

`resumable=True` adds `--resume-from STEP`: `ctx.step` returns False for the steps before
it, which the handler skips, and they are listed in `skipped_steps`. A failure's
`data.resume_from` is the value to pass; an undeclared step exits `2` (REQ-O-010). The
`resume-guard` audit rule flags a resumable handler that ignores what `ctx.step` returns.
`rollback=restore_backup` adds `--rollback-on-failure`: when a step fails, treaty calls
`restore_backup(args, ctx, completed)` with the completed steps, newest first, before the
teardown, and `data.rollback_status` is `completed`, `failed` (with `rollback_error`), or
`not_attempted` (REQ-O-011). `--schema` says `resumable` and `rollback_available`.

A command that works through a batch of items returns `treaty.Batch[T]`, one `Item` per
item with its value or its error:

```python
def send(args: SendArgs, ctx: Ctx) -> Batch[Sent]:
    items = []
    for user in args.users:
        try:
            items.append(Item(user, deliver(user)))
        except Undeliverable as exc:
            items.append(Item(user, error=Exit.INVALID_EMAIL(str(exc))))
    return Batch(items)
```

`data` is `summary` (`total`, `succeeded`, `failed`) and `results`, one per item in the
handler's order: `{"id": 1, "ok": true, ...value}` or `{"id": 3, "ok": false, "error":
{"code", "message", "retryable"}}`, so a caller retries only the failed items (REQ-C-009).
An item's error is a `treaty.ItemError(code, message, retryable)` or a `treaty.Exit`,
whose `retryable` comes from its exit code. Any failed item exits `3` with
`PARTIAL_FAILURE`, "2 of 5 items failed", `data` kept and `data.partial` true when some
succeeded. A mutating batch checks each item's `effect` and reports one for the batch.

## Locks

`ctx.lock(name)` holds a lock shared by every run of the app for a `with` block:

```python
with ctx.lock("deploy", wait=10, retry_after_ms=2000):
    push(release)
```

A run that cannot take it within `wait` seconds (default: what is left of the timeout)
exits `4` with `LOCK_HELD`, `retryable: true`, `retry_after_ms`, and the holder's
`holder_pid`, `holder_age_ms`, and `lock_file` in `context` (REQ-F-033). The lock is an
advisory file lock under `locks/` of the state directory, so the operating system releases
it when the holder exits, even on SIGTERM or SIGKILL. The `lock-declared` audit rule flags
handlers that call `fcntl.flock` or a `FileLock` themselves.

## Cancellation

SIGINT and SIGTERM produce a `CANCELLED` envelope with exit `130` or `143`, tear the run
down first (resources' `release`, then `cleanup=`), and a second signal during cleanup exits at once
without a second write. A handler's own `except Exception` cannot swallow the signal, and a
retry waiting for an idempotency key is interrupted too. A signal that arrives after the
handler returned is held: the finished result is written with its own exit code, since
the work it reports did happen. An `exec` plan stops at the first signal, even with
`--ignore-errors`, and exits `130` or `143`. A reader that closes stdout early
(`tool logs | head -1`) ends the run silently: the teardown runs, nothing more is
written, and the exit is `0` once a complete envelope or event reached the reader, since it
got what it wanted (REQ-F-014), or `141` (`OUTPUT_CLOSED`) when it left before any. All three
signal codes appear in every command's `exit_codes` map.

## Platforms

treaty runs on Linux, macOS, and Windows; CI runs the suite and a scaffolded project on
all three. Where the operating system lacks a mechanism, the behavior differs:

| Behavior | Linux and macOS | Windows |
|----------|-----------------|---------|
| Ctrl+C in a console | `SIGINT`: teardown, `CANCELLED`, exit `130` | The same |
| Termination by another process | `SIGTERM`: teardown, `CANCELLED`, exit `143` | `TerminateProcess` (`taskkill /F`, `os.kill`) ends the process at once: no teardown, no envelope |
| Reader closes stdout early | Exit `0` or `141` (`OUTPUT_CLOSED`) | The same, detected from the broken pipe since there is no `SIGPIPE` |
| Stopping `ctx.run` children | `SIGTERM` to the process group, then `SIGKILL` | Only the child is stopped, not its grandchildren |
| `ctx.spawn` background processes | New session; `cleanup` stops a live process group | Detached process; a pid is kept until its deadline, since probing it would kill it |
| `ctx.pipeline` stage ended by `SIGPIPE` | Not a failure when a later stage succeeded | No such signal; every non-zero stage fails |
| Locks (`ctx.lock`, config writes, idempotency) | `flock` | `msvcrt.locking`; an idle lock file is removed only when no process holds it open |
| Private temp dirs and files | Mode `0700` and `0600` | No permission bits; the user profile's ACLs apply |
| Headless detection | No `DISPLAY` or `WAYLAND_DISPLAY` on Linux and the BSDs, or over SSH | A console session is never headless for lack of a display |
| Output line endings | LF | LF: `App.main` never writes CRLF |
| Child locale (`ctx.run` sets the C locale) | `LANG=C`, `LC_MESSAGES=C`, `LC_NUMERIC=C`, and `LC_CTYPE=C.UTF-8` or `C` | `LC_ALL=C` and `LC_NUMERIC=C`; native children take the locale from the user profile and ignore them |
| Venv `python.exe` children | The pid treaty tracks is the interpreter | The pid treaty tracks is the venv launcher, which starts the interpreter as its child |
| `cleanup_command` in `data.cleanup` | `rm -f ...` | `del /f /q ...` |
| `platform=` | `sys.platform` values | `win32` |

The signal tests skip on Windows because the test harness cannot deliver `SIGINT` or
`SIGTERM` to a running child there; the permission-bit and `/bin/sh` tests skip for the
same kind of reason.

## Response meta

`data` is safe to cache and diff: two runs of the same call with the same state return
byte-identical `data`. Everything that changes per call lives in `meta`, which is volatile
by definition and never part of a diff: `request_id`, `duration_ms`, `timestamp` (ISO 8601
UTC), `command` (the manifest key, or the app name when no command resolved),
`schema_version`, `tool_version` (the `App(version=)` in semver: a PEP 440 release
such as `importlib.metadata.version` returns, `1.0.0rc1`, is given as `1.0.0-rc.1`,
`1.0.0.dev0` as `1.0.0-dev.0`, and `1.0.0.post1` as `1.0.0+post.1`; also
what `--version` prints), and `cwd` (as `pwd` prints it). `trace_id`, `project_root`, and
`retries` appear only when they apply, never as null. `treaty audit` flags output fields
that break this, such as a `fetched_at` (rule `volatile-data`).

```python
@app.command(
    "lint",
    description="Lint the project",
    danger_level="safe",
    exit_codes=("UNAVAILABLE",),
    schema_version="2.1",           # MAJOR.MINOR of the output contract
    compat={"1.4": to_v1},          # --schema-version 1 answers in the old shape
    project_root=(".git",),         # ctx.project_root and meta.project_root
    retry=Retry(retries=3, delay_ms=500),  # ctx.retry, --retries, --retry-delay
)
```

- **Schema versions**: a breaking change to the output bumps the major, an additive one the
  minor. `treaty schema-lock myapp.cli:app` records every command's version and output
  schema in `treaty-schema.lock`; the audit's `schema-version` rule then fails a change
  without the matching bump. `--schema-version MAJOR` selects a `compat=` shim, warns
  `SCHEMA_DEPRECATED`, and exits 2 with `SCHEMA_VERSION_UNSUPPORTED` for a major the
  command does not serve; `<cmd> --schema` shows `schema_version` and `min_schema_version`
- **Schema changelog**: `App(schema_changelog=Path(__file__).parent / "schema-changelog.json")`
  adds `changelog`, the versions newest first with `date`, `breaking`, and the `added`,
  `removed`, and `changed` field paths (`deploy.flags.target`, `deploy.output.url`,
  `deploy.exit_codes.10`); `--since 1.0.0` keeps the newer ones (REQ-O-029).
  `treaty changelog-add myapp.cli:app` diffs the `<app>.manifest.json` snapshot beside the
  file against the live manifest, marks the entry breaking when anything was removed or
  retyped or a required flag appeared, and updates the snapshot; the `schema-changelog`
  audit rule warns when the live manifest is not recorded
- **Trace**: `TOOL_TRACE_ID` becomes `meta.trace_id`, is inherited by every `ctx.run`
  child, and ends every framework line on stderr (`trace=<id>`, or a `trace_id` key in
  JSON). A value over 256 characters or with control characters exits 2
  (`TRACE_ID_INVALID`)
- **Retries**: `ctx.retry(fn)` calls again on `Retry.on` exceptions, never past the
  timeout; `meta.retries` counts the retries, and running out exits with
  `Retry.exhausted` (default `UNAVAILABLE`, which the command declares) with
  `retryable: false` and `retries_exhausted`, so an agent does not retry on top.
  `--retries 0` fails on the first error
- **Backoff**: `Retry(backoff=2.0)` doubles each wait from `delay_ms` (which
  `--retry-delay` sets), `max_delay_ms` caps one wait, and `jitter=0.1` moves each by up
  to a tenth either way. A raised error's `retry_after_ms`, as a 503's `Retry-After` does
  through `ctx.http`, lengthens the computed wait, and one over 60 s ends the run with
  that error instead. `retry_if=lambda r: r.pending` retries a returned value too, for a
  job API that answers "not ready yet" with a 200

## Output data

`data` is written the same way for the same result, so it can be hashed and diffed:

- **Arrays are sorted**: strings by code point, numbers ascending, arrays of objects by a
  declared key, else by each item's JSON text. `sort_key="id"` on a command orders its
  output list (and a list command sorts before paging, so pages follow one order);
  `treaty.Out(sort_key="id")` does the same for a field. `ordered=True` and
  `Out(ordered=True)` keep the handler's order, for a ranking, and that of every array
  inside untyped content such as a `list[dict[str, object]]` from `model_dump()`; the
  schema says `"x-ordered": true`. Fixed tuples keep their order. Rule `stable-order`,
  a warning for an array of objects with neither, so `--strict` fails on it
- **Every key, every time**: an output dataclass writes all its fields, so the output
  schema lists each as `required`; `X | None` is nullable. An empty collection is `[]` or
  `{}`, never `null`, so `list[T] | None` in an output type fails registration; write
  `tags: list[str] = treaty.Out(default_factory=list)`. A key that exists only in some
  versions belongs to a new `schema_version`. `""` means empty and `null` means unset;
  treaty cannot tell which you meant, so keep them apart
- **Binary**: `bytes`, or `treaty.Binary(data, content_type="image/png")`, becomes
  `{"type": "binary", "encoding": "base64", "value": ..., "size_bytes": N,
  "content_type": ...}`; plain mode prints `<binary N bytes image/png>`. Rule
  `binary-output`
- **`--stable-output`** (`stable_output: true` in `exec` and MCP) makes stdout
  byte-identical for identical calls: `meta` leaves out `request_id`, `timestamp`, and
  `retries`, `duration_ms` is 0, fields declared `Out(volatile=True)` are dropped, and no
  heartbeat lines are written
- **LF only**: `App.main` writes `\n` line endings on every platform, Windows included

```python
@dataclass(frozen=True, slots=True)
class Report:
    users: list[User] = Out(sort_key="id")
    top: list[str] = Out(ordered=True)
    fetched_at: str = Out(default="", volatile=True)
```

## Output security

`data` is safe to put in an agent's context by default:

- **Masked**: a string that decodes as a JWT becomes `[JWT: sub=user_123,
  exp=2024-03-11T15:00:00Z]`, a base64 blob `[BASE64: 192 bytes]`, and a field whose
  name ends in a credential word (`access_token`, `client_secret`, `api_key`) `[KEY:
  ghp_abc1...]`; a `HIGH_ENTROPY_MASKED` warning lists the paths. Git SHAs, hex digests,
  paths, versions, and host names are left alone. `--unmask` returns the raw values; no
  environment variable, config file, exec line, or MCP argument can set it.
  `Out(high_entropy=True)` always masks a field, `Out(high_entropy=False)` never (a
  content hash to compare). Rule `high-entropy`
- **Tagged**: `external=True` on a command whose `data` comes from outside the tool (a
  file, an API response), or `Out(external=True)` on the field that holds it, adds
  `"_source": "external", "_trusted": false` to the top of `data`, or to each object when
  `data` is a list, and an `UNTRUSTED_CONTENT` warning. A field marked
  `Out(external=True)` tags `data` as a whole whenever the field is not `null`, not each
  object inside the field. A `Batch` is protected by its item type, so a field marked on
  the item type tags the batch's `data`, and a batch keeps its tags when some items failed
  (exit `3`), since the items that worked are still in `data`. A failure's `data`,
  `raise Exit.X(..., data=...)`, is tagged on an `external=True` command as a success's
  is. `--no-injection-protection` drops the tags, also those of `error.context`, sets
  `meta.injection_protection: false`, and reports its use on stderr. Rule `external-data`
  warns when a network or child-process command declares neither; `external=False` on the
  command says it returns only values it computed
- **External context**: `treaty.External(value)` marks a top-level value of a failure's
  `error.context` as content from outside the tool, such as a play log a failed child
  wrote: `raise Exit.PLAYBOOK_FAILED(msg, context={"output": External(tail)})`. The
  marked values are masked as external `data` is, the other context values are left as
  they are, and `"_source": "external", "_trusted": false` go to the top of
  `error.context` with an `UNTRUSTED_CONTENT` warning. `SUBPROCESS_FAILED` marks its
  `context.stderr` itself. `External` anywhere else, nested in a context value or in
  `data`, is `INVALID_EXIT`. Rule `external-data` also warns when a `context=` value is
  built from a `ctx.run` result's `stdout` or `stderr` without `External`, whatever the
  command declares

```python
@dataclass(frozen=True, slots=True)
class Page:
    url: str
    body: str = Out(external=True)
    etag: str = Out(high_entropy=False)
```

## Raw payloads

Declare `supports_raw_payload=True` and the command accepts `--raw-payload '{"name": "x"}'`
as an alternative to individual flags. The payload is checked against the same field types as
`exec` lines, and mixing it with individual flags exits `2`. It and `exec` lines forgive
what agents write: trailing commas, `//` and `/* */` comments, single quotes, and unquoted
keys parse to the same value as strict JSON. Anything worse exits `2` with `INVALID_JSON`,
and `error.corrected_input` holds the repaired JSON when there is one, such as
`{"env": "prod"}` for `{env prod}`.

## Schemas

`tool <cmd> --schema` prints the command's manifest entry plus `parameters` and a draft-07
`output_schema` derived from the handler's return annotation. Commands with
`supports_raw_payload=True` also get `raw_payload_schema`, the JSON Schema of their args
dataclass. `tool --schema` prints the same manifest as `tool manifest`: codes every
command shares sit once in the root `exit_codes`, and each entry lists only its own
additions (a valid ManifestResponse, so without `parameters` or `raw_payload_schema`);
`tool <group> --schema` prints one group's subtree; `--print-schema` is an alias.
`tool <cmd> --output-schema` prints only the JSON Schema of the command's `data`. The
output is JSON in every mode.
Piped `--help` writes its text to stderr and prints only a pointer on stdout:
`{"data": null, "meta": {"help": true, "schema_ref": "deploy --schema"}}`.

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
declares: `timeout`, `idempotency_key`, `confirm_destructive`, `retries`, `retry_delay`,
and `schema_version`. The output schema is
the response envelope around the command's `output_schema`, and every result carries the
envelope as `structuredContent` and as JSON text; `isError` mirrors `ok`. Calls go through
`App.call`, the same path as an `exec` line, so an unconfirmed destructive tool call
returns `CONFIRMATION_REQUIRED` with its dry-run preview, idempotency keys, timeouts,
effect validation, and output caps all apply, and a streaming command returns its buffered
envelope. Tool annotations map `safe` to read-only and idempotent, `idempotent=True` to
idempotent too, `destructive` to
destructive, and `has_network_io` to open-world. `App.call(path, arguments)` is public
for other in-process adapters.

`treaty-mcp deployctl:app --list-tools` prints the tools as JSON with `cli_version`, no
`mcp` package needed; commit it, and `deployctl mcp-validate --mcp-schema-file mcp.json`
in CI exits `1` with `SCHEMA_DRIFT_DETECTED` when the commands drifted from it: `added`
and `removed` fields, `changed` types, and `missing_from_mcp` commands (REQ-O-035).

## Conformance

`conformance/deployctl.json` is a profile for the spec's deterministic kit. With the spec checked
out as a sibling directory:

```bash
uv run --project ../cli-agent-ergonomics ../cli-agent-ergonomics/conformance/run.py conformance/deployctl.json
```

The example CLI passes all twelve checks across levels 1 to 3. The same run is a pytest test
that skips when the spec checkout is absent.

## Start a project

Run it beside a `cli-agent-ergonomics` checkout, which is the spec for the kit:

```bash
cd ..                                  # the directory holding cli-agent-ergonomics/
uvx treaty init shop-tool
cd shop-tool && uv sync && uv run pytest
uv run treaty conformance shop_tool.cli:app --run
```

The new project depends on `treaty` from PyPI; `--treaty-source /path/to/treaty` pins a local
checkout instead.

To move an existing CLI instead, `treaty scaffold-from typer|click|argparse module:obj`
imports it, walks its command tree, and writes a treaty module: an args dataclass per
command, with group options on base classes, and a handler that returns its arguments with
`effect: "noop"` until you give it a body. Every command starts as `mutating` with no exit
codes, which `treaty audit` reports until you declare them. `--out FILE` writes the module
(exit `6` with `CONFLICT` over an existing file without `--force`); without it the module is
in `data.source`. The [migration
chapter](https://github.com/romamo/treaty/blob/main/docs/tutorial/B-migrate/click-typer.md#scaffold-the-commands-of-a-large-cli)
walks through it.

`init` scaffolds a package with one command per danger level, typed outputs, declared exit
codes, a test using `app.run()`, and a conformance profile. `conformance` derives probes
from each command's first example and danger level, writes the profile, and with `--run`
executes the spec kit, exiting with `CONFORMANCE_FAILED` when checks fail. An existing
profile that differs from the generated one as JSON, such as one with hand-written probes,
is left alone: the command exits `6` with `CONFLICT`, naming the changed keys and probes,
and `--force` replaces it. An equal profile is not rewritten (`effect: noop`). The kit is found
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

Ordered rules (`treaty rules` lists them) check the registry and print the next steps with a
fix using your own names: missing examples, danger levels that contradict command names,
mutating commands without their own exit codes, exit codes a handler raises but does not
declare, retryable codes on non-idempotent commands,
untyped outputs, undeclared network I/O, path-like fields not typed `Path`, wide mutating
commands without `--raw-payload`, missing cleanup hooks, blanket scopes, login commands
without `auth=`, commands that start work without returning a job, config writes without a
scope, per-call values in output data, output schema changes without a `schema_version`
bump, handlers that find a project root or retry by hand, retry waits longer than the
timeout, mutating commands left on the default timeout, and a missing conformance
profile. The next steps come errors first, then warnings, then advice, in rule order within
each, so what fails `--strict` is never behind advice. `--all` lists
everything, `--strict` exits 79 (`AUDIT_FAILED`) on any warning so CI can gate on it, and
piping the output gives an envelope an agent can act on. Rules see declarations only; the
conformance kit covers runtime behaviour. A target that fails to register (a
`RegistrationError` or `SyntaxError` at import) exits 4 (`APP_IMPORT_FAILED`) with the
message and no traceback, on `audit` and every other command that loads a `module:app`
target.

Rules that read source follow each handler into the functions of its own code it calls, by
plain name (`fetch(...)`) or through modules and classes (`helpers.enter(...)`,
`Store.load(...)`); a finding there ends with `found via helpers.enter (helpers.py:6)`.
The handler's module is followed however deep, and its other first-party modules 3 calls
deep: the handler's top-level package, or, when the app is a top-level module such as
`mycli.py`, files under its directory. treaty, the standard library, and site-packages are
never followed, nor are methods of objects or callbacks, which the report's `scope` says.
The `declared-exits` rule also follows a method called on a parameter annotated with the
args class or a resource class (`store.load()`), and the methods that one calls on `self`.

## Stability

treaty follows semantic versioning from 1.0. The frozen surface is listed in
[`docs/api.md`](https://github.com/romamo/treaty/blob/main/docs/api.md) and snapshotted by `tests/test_public_api.py`: every name
exported from `treaty`, every keyword of `App`, `App.command`, `Group.command`, `Flag`,
`Arg`, and `Out`, the public fields and methods of `Ctx`, every envelope and manifest key,
every framework exit code and error code, and every environment variable name. Modules
named `treaty._*` are private and may change in any release.

- **Patch** releases fix bugs. A fix may make treaty refuse something it wrongly accepted,
  such as a malformed declaration, when accepting it broke the contract
- **Minor** releases add: new keywords, built-ins, audit rules, error codes, and optional
  envelope or manifest keys. An agent that ignores unknown keys keeps working. A new
  built-in yields to an app command of the same name, so no app breaks
- **Major** releases remove or change meaning. Nothing is removed without first being
  deprecated for at least one minor release

A deprecated treaty keyword or name keeps working through its deprecation window and says
so when the app registers, with a `DEPRECATED_USAGE` warning naming the replacement, and
`treaty audit` lists each use with the fix. Nothing is deprecated yet, so that check lands
with the first deprecation after 1.0, built on the same `treaty.Deprecated` metadata. Apps retire their own commands and flags the same way, with
`deprecated=treaty.Deprecated("1.4.0", replacement=...)`, `App.redirect`, and
`treaty audit --baseline` (see [Renamed commands](#renamed-commands)).

treaty 1.0 claims CLI Agent Spec Level 2 conformance; the Level 3 score is in
[`COMPLIANCE.md`](https://github.com/romamo/treaty/blob/main/COMPLIANCE.md). Changes are listed in [`CHANGELOG.md`](https://github.com/romamo/treaty/blob/main/CHANGELOG.md),
and [`docs/guide.md`](https://github.com/romamo/treaty/blob/main/docs/guide.md) covers the design calls the audit cannot make.

## Development

```bash
uv sync
uv run pytest
uv run mypy src
uv run ruff check src tests
```

The manifest test validates against the spec schemas in the sibling
`cli-agent-ergonomics` checkout; set `TREATY_SPEC_DIR` to point elsewhere.
