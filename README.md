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
plus `GENERAL_ERROR`, `ARG_ERROR`, and `TIMEOUT` everywhere and `CONFLICT` and
`PRECONDITION` on mutating commands. Anything else, including framework names such as
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

## Built-ins

Every app gets `manifest`, `version`, and `exec` (disable with `App(..., enable_exec=False)`);
`App(credentials=...)` adds `check-permissions` (see Credentials) and `App(jobs=...)` adds
`job status` and `job cancel` (see Async jobs).
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

Any command can read a payload the same way: `stdin_input=True` adds `--input-file` and puts
the text in `ctx.stdin_text` before the handler runs; over the cap (`<APP>_MAX_STDIN_BYTES`
also sets it) the run exits `2` with `STDIN_TOO_LARGE` and a `hint` naming `--input-file`.
In `exec` and MCP, where stdin is taken, such a command needs `input_file`.

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
words resolve to in `context.command`, and puts the corrected order in `suggestion`. Plain
mode prints every error's suggestion as a final `hint:` line on stderr.

## Output formats

`--format` takes `json`, `jsonl`, `plain`, `tsv`, and any format the app registers. With no flag,
`TREATY_FORMAT` decides; without that, the format is `json` when stdout is not a terminal
or `CI` is set, and `plain` otherwise.

`json` writes the full response envelope, one compact line per envelope; it is the contract
agents read and takes no renderer. `jsonl` is the same output under the name line-oriented
readers ask for. Every other format writes the result data as text and errors as prose on
stderr. `tsv` is built in: a header row, then one row per item (nested values as compact
JSON); `treaty.table(",")` is the same renderer for CSV, `app.format(Format.CSV,
render=table(","))`.
A renderer receives `data` as JSON values (dicts and lists, after secret redaction) and
returns the text. Formats are `Format` members, never strings:

```python
from treaty import App, Format

app = App("hello", version="0.1")
app.format(Format.CSV, render=render_csv)  # offers --format csv to every command


@app.command(
    "greet",
    description="Say hello",
    danger_level="safe",
    exit_codes=(),
    renderers={Format.PLAIN: render_greet},
)
```

A command's `renderers=` overrides the app's renderer for that format. `app.format()` must
come before the commands that override it, and a command can only override a format the
app offers. `Format` lists every format treaty knows (`plain`, `json`, `jsonl`, `csv`,
`tsv`, `yaml`, `markdown`); an app offers `plain`, `json`, `jsonl`, `tsv`, and the ones it
registers, and the manifest and `--help` list exactly those. Any other value exits `2`
listing them, before anything runs or any file is written.

A command declared `output_file=True` takes `--output PATH`: the result goes to the file in
the `--format` representation, and stdout gets the JSON envelope with `data: {"path": ...,
"bytes": N}`. A failed run writes no file. `--output json` (any format name) exits `2`
suggesting `--format json`; `--output` never selects a representation.

Without a renderer, `plain` prints flat lines, one item each: `key: value`, with dotted
paths for nested values (`release.tag: 1.3.9`) and line breaks inside strings escaped. An
array prints each element as its own block, the way a stream prints its events.
`app.format(Format.PLAIN, render=...)` replaces those lines for the whole app.
`manifest` and `--schema` stay JSON in every text format, since they are read by programs.

Breaking after 0.0.4: `renderers={Format.PLAIN: ...}` replaces `plain=`, which is no
longer accepted, and `Format` replaces `OutputMode`.

Breaking after 0.0.3: `plain` replaces `human`, and `plain=` replaces `human=`. `human` is no
longer accepted anywhere; `--format human` exits `2` listing the allowed values.

## Timeouts

Every handler runs under a wall-clock limit: `App(default_timeout=60)` app-wide,
`@app.command(..., timeout=5)` per command, and `--timeout` on any command declaring
`has_network_io=True` and on every streaming command (`--timeout 0` disables it; at most
one year). A stream buffered in-process (`App.call`, MCP) always has a deadline: the
caller's `timeout`, else the app default; `0` is refused there. On expiry the
framework writes a `TIMEOUT` envelope, exits `10`, and records `meta.timeout_ms` on every
response. Handlers read `ctx.timeout` to pass the same deadline to their network calls;
the `network-timeout` audit rule flags `urlopen`, `http.client` connections,
`socket.create_connection`, `requests`, and `httpx` calls without `timeout=` in network
commands (REQ-C-012). An
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
gets a `THIRD_PARTY_STDOUT` warning with the byte count; writes straight to file
descriptor 1 (C extensions, child processes) are not caught. Handlers log with
`ctx.log("connecting", host=host)`: one line on stderr, a JSON object with `level`,
`message`, and `fields` in JSON mode and `message key=value` otherwise. Declared secrets
and fields named like credentials (`token`, `password`, `API_KEY`, `DB_PASS`,
`Authorization`, `Cookie`, at any depth) print as `[REDACTED]` (REQ-F-051).

In JSON mode every string is cleaned before it is written: ANSI escape sequences and
carriage returns are removed, and null bytes and lone surrogates become U+FFFD (REQ-F-007,
REQ-F-016). Plain mode prints text as returned. `ctx.color` tells a renderer whether it may
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
retry step. The audit rule `exit-code-suggestion` flags retryable codes without one.

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
  shows it (`ctx.run("git log")`, an f-string, `shell=`), exit `1` when it happens at run
  time. The `no-shell` audit rule flags `os.system`, `os.popen`, and `shell=True`
- Children read `/dev/null` unless given `input=`, and get `NO_COLOR=1`, `PAGER=cat`,
  `GIT_PAGER=cat`, `MANPAGER=cat`, `LESS=-F -X -R`, and an empty `MORE`; off a terminal
  also `EDITOR`, `VISUAL`, and `GIT_EDITOR` set to `true`, so an editor exits at once
  (REQ-F-046, REQ-F-055). Grandchildren inherit them; `env=` overrides single variables
- A non-zero exit raises `SUBPROCESS_FAILED` (exit `1`) with `argv`, `returncode`,
  `stage`, and the last 4 KiB of stderr in `context`; `check=False` returns a `Completed`
  instead. In a pipeline any failing stage fails the whole, the first one named
  (REQ-F-065). `ctx.pipeline` checks each stage like `set -o pipefail`, so a stage killed
  by SIGPIPE (`yes | head -1`) fails too
- `timeout=` defaults to what is left of the command's timeout; running out stops the
  child and raises `TIMEOUT`. Each child starts in its own session, so it cannot open the
  terminal, and a signal or timeout sends SIGTERM to its process group, then SIGKILL
  after 2 seconds, before the `CANCELLED` or `TIMEOUT` envelope is written (REQ-F-031)

`App.main()` writes the same pager and, off a terminal, editor settings into
`os.environ`, so programs started without `ctx.run` inherit them too.

A command that opens a browser declares `gui_operations=["browser_open"]` and returns an
`open_url: str | None` field. `ctx.open_url(url)` opens it and returns `True`, except in a
headless run (no terminal on stdin and stdout, `CI`, or no `DISPLAY` or `WAYLAND_DISPLAY`
on Linux or over SSH): then nothing opens, the URL lands in `data.open_url`, and every
envelope of the run has `meta.headless: true` (REQ-F-057). `ctx.headless` tells the
handler.

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
- Off a terminal, `input()` and `sys.stdin.readline()` in a handler exit `4` with
  `INTERACTIVE_BLOCKED`, even inside `except Exception` (REQ-F-047); `sys.stdin.read()`
  and line iteration still read piped data

Calling `ctx.prompt`, `ctx.confirm`, or `ctx.edit` without the declaration is a
`RegistrationError` when the handler's source shows it. The manifest lists
`interactive`, `requires_editor`, and `non_interactive_alternatives`, and exit `4` for
these commands. Running with no arguments prints help and exits `0`; treaty has no REPL.

## Output size

JSON output is capped at 1 MiB per envelope: `App(max_output_bytes=...)` app-wide,
`TREATY_MAX_OUTPUT_BYTES` or `<APP>_MAX_OUTPUT_BYTES` in the environment (the tool's own
variable wins), or the global `--max-output` flag, in increasing precedence. Past the cap
the framework follows whichever child holds most of the bytes and cuts the list, object, or
string where no child dominates to the longest prefix that fits. `meta` gets `truncated`,
`total_bytes`, and a `truncation_hint` that is a command to run as given (plus
`total_count` and `returned_count` when `data` is a list), and each cut adds a
`FIELD_TRUNCATED` warning naming the field. For a list command whose page was cut the hint
is the next page, `--limit <kept> --cursor <token>`, and `meta.pagination` points there
too; otherwise it is the same command with the `--max-output` that returns everything.
Plain mode is not capped.

## Lists

A command declared `paginated=True` returns `list[T]` or `treaty.Page[T]` and gets
`--limit` (default 20, `default_limit=` per command, `0` for every item) and `--cursor`.
Every successful response carries `meta.pagination` with `total`, `returned`, `truncated`,
`has_more`, and `next_cursor`; pass `next_cursor` as `--cursor` for the next page:

```python
@app.command("releases.list", description="List releases", danger_level="safe",
             exit_codes=(), paginated=True)
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
Its own cursor is URL-safe base64 naming the command, the handler's cursor, and how many
items of that batch were already delivered; one that does not decode or names another
command exits `2` with `INVALID_CURSOR`. In `exec`, `_opts` take `limit` and `cursor`; MCP
tools take them as arguments. `--schema` shows `default_limit`, and the `paginated-list`
audit rule flags list outputs without `paginated=True`. Streams are not paginated.

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

## Credentials

Treaty never stores or refreshes a token. The app tells it which scopes the active
credential holds, or `None` when no one is logged in:

```python
class Keychain:
    def active_scopes(self, ctx: Ctx) -> Iterable[str] | None: ...

app = App("authctl", version="1.0.0", credentials=Keychain())

@app.command("repos.list", description="List repositories", danger_level="safe",
             exit_codes=(), requires_auth=True, required_scopes=["repo:read"])
```

A `requires_auth=True` command must list its scopes (REQ-C-029). Before its handler runs,
no credential exits `8` (`AUTH_REQUIRED`), a missing scope exits `7` with
`INSUFFICIENT_SCOPES` and `context.missing_scopes`, and scopes beyond the required ones add
a `CREDENTIAL_OVER_PRIVILEGED` warning (REQ-O-047). `check-permissions --for <command>`
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
file; a global write also takes an advisory lock and adds a `GLOBAL_CONFIG_MODIFIED`
warning. The `config-write-scope` audit rule flags `config` and `set` commands without the
declaration.

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
`_line` and `_cmd`. Streaming commands must be `safe`: the effect and idempotency
contracts describe one response. In a text format the renderer gets one event per call.

## Destructive commands

A command with `danger_level="destructive"` must declare a boolean `dry_run` field, and its
output type a `would_affect` field (`would_affect: Affects | None = None`). A dry run returns
`treaty.Affects(summary, resources, count)` there: a line for a person and the identifiers
for a program; a dry run without it exits `1` with `INVALID_EFFECT` (REQ-C-004). Without
`--confirm-destructive` the framework runs it in dry-run mode and exits `2` with error code
`CONFIRMATION_REQUIRED`, whose message carries the summary and whose `data` is the preview.
`--schema` of a destructive command has `requires_confirmation: true` (REQ-O-021).

`safe_default=True` makes the dry run the default instead (REQ-O-048): without `--live` the
command previews and exits `0`, and `--live --confirm-destructive` applies it (`--live`
alone still exits `2`). Every response of such a command carries `meta.dry_run`, and a
live one `meta.confirmed`; the manifest shows `safe_default: true` and the `--live` flag.

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
(`tool logs | head -1`) ends the run silently: the cleanup hook runs, nothing more is
written, and the exit is `0` once a complete envelope or event reached the reader, since it
got what it wanted (REQ-F-014), or `141` (`OUTPUT_CLOSED`) when it left before any. All three
signal codes appear in every command's `exit_codes` map.

## Raw payloads

Declare `supports_raw_payload=True` and the command accepts `--raw-payload '{"name": "x"}'`
as an alternative to individual flags. The payload is checked against the same field types as
`exec` lines, and mixing it with individual flags exits `2`.

## Schemas

`tool <cmd> --schema` prints the command's manifest entry plus `parameters` and a draft-07
`output_schema` derived from the handler's return annotation. Commands with
`supports_raw_payload=True` also get `raw_payload_schema`, the JSON Schema of their args
dataclass. `tool --schema` prints the same manifest as `tool manifest`: codes every
command shares sit once in the root `exit_codes`, and each entry lists only its own
additions (a valid ManifestResponse, so without `parameters` or `raw_payload_schema`);
`tool <group> --schema` prints one group's subtree. The output is JSON in every mode.
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

Run it beside a `cli-agent-ergonomics` checkout, which is the spec for the kit:

```bash
cd ..                                  # the directory holding cli-agent-ergonomics/
uvx treaty init shop-tool
cd shop-tool && uv sync && uv run pytest
uv run treaty conformance shop_tool.cli:app --run
```

The new project depends on `treaty` from PyPI; `--treaty-source /path/to/treaty` pins a local
checkout instead.

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

Ordered rules (`treaty rules` lists them) check the registry and print the next steps with a
fix using your own names: missing examples, danger levels that contradict command names,
mutating commands without their own exit codes, retryable codes on non-idempotent commands,
untyped outputs, undeclared network I/O, path-like fields not typed `Path`, wide mutating
commands without `--raw-payload`, missing cleanup hooks, blanket scopes, login commands
without `auth=`, commands that start work without returning a job, config writes without a
scope, and a missing conformance profile. `--all` lists
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
