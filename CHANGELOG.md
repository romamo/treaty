# Changelog

All notable changes to treaty. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and treaty follows
[Semantic Versioning](https://semver.org/) from 1.0 (see "Stability" in the README).
Before 1.0 any release could break an app; the "Breaking" sections say where.

Apps built on treaty keep their own, structured schema changelog with
`App(schema_changelog=)` and `treaty changelog-add`; this file is treaty's.

## [Unreleased]

## [1.0.0rc27] - 2026-10-03

The 27th 1.0 release candidate: 1 fix.

### Fixed

- A declared secret split by a terminal escape, such as `hunte\x1b[0mr2`, no longer
  reaches stderr or the envelope whole (#277): it was redacted while the escape still hid
  it, and became whole once the escape was cleaned away, or showed whole on a terminal
  that kept it. The `--heartbeat-interval` status, `ctx.log` and `ctx.progress` lines in
  every format, `ctx.warn` warnings, and a crashed handler's message and traceback were
  affected. Redaction now also looks at the text without its escapes,
  and where that finds a secret, the text goes without them; the heartbeat status is
  redacted, cleaned, and redacted again before its 200-character cut, and a traceback
  is redacted before its escapes are written out as `\x1b`. A refused setting's message
  is redacted the same way, the longest secret first. Cleaning takes an escape a
  terminal hides whole out whole: the charset reset `\x1b(B` that `tput sgr0` writes,
  `\x1b7`, and a DCS, SOS, PM, or APC string left their rest as text, and a secret one
  split reached a host's stream whole through `App.call`

## [1.0.0rc26] - 2026-10-03

The 26th 1.0 release candidate: 2 fixes.

### Fixed

- What reaches descriptor 1 as the process exits, a handler thread abandoned at its
  timeout still alive, is no longer lost (#271): a host's exit hook that runs after
  treaty's, or the interpreter's last flush of `sys.__stdout__`, wrote to the pipe after
  its reader had stopped with the interpreter. Treaty's exit hook, now registered as
  treaty is imported so it runs after every hook registered later, turns descriptor 1 to
  a spool file and passes what reaches it on to stderr, redacted, as the last held thread
  ends or at the interpreter's last flush; once no held thread lives, descriptor 1 is
  stdout again. It never leads to stdout while one does

- A declared secret that the `THIRD_PARTY_STDOUT` warning's 4096-character cut split no
  longer leaves its first part in the warning, its `-vv` trace, or stderr (#274): the
  printed text was cut before it was redacted, so the secret was no longer whole. The
  text is now kept past the cut, redacted whole, and only then cut; where even that text
  was cut, its end, which may hold the start of a secret, goes first.
  `SHELL_STRING_PROHIBITED` quotes the shell string whole in `context.argv`, rather than
  its first 200 characters, for the same reason

## [1.0.0rc25] - 2026-10-03

The 25th 1.0 release candidate: 2 fixes.

### Fixed

- `--debug` with a `logging.handlers.QueueListener` whose handler writes to `sys.stdout`
  no longer floods stderr (#268): the listener's thread wrote each `stdout write` trace
  record back to stdout, which traced it again, tens of thousands of lines a second. A
  write made while a handler emits one of treaty's own trace records now goes to stderr
  as written, on any thread; the record's own text is still traced once, redacted
- As the process exits with a handler thread abandoned at its timeout still alive, the
  last flush of `sys.stdout` no longer risks blocking the exit (#268): descriptor 1 is
  then a pipe whose reader stops as the interpreter finalizes, and on Windows its 4 KiB
  buffer could not hold the flush. The pipe now holds 1 MiB on Windows and Linux

## [1.0.0rc24] - 2026-10-03

The 24th 1.0 release candidate: 4 fixes.

### Fixed

- A secret used as a mapping key is redacted as it is as a value (#262): a string or
  numeric secret as a key in `Exit` context, a warning's context, a `ctx.log` field, the
  audit log's arguments, or an `exec_fallback` line's error context and data came back
  raw, and now reads `[REDACTED]` (or with the secret's text replaced inside it); a key of
  another type, such as a tuple, is read by the text JSON prints it as. Two keys
  that redact to the same text both stay, the later one as `[REDACTED]#2`, `#3`, and so
  on. Command `data` is still not redacted (REQ-F-034)
- `--debug` with a log handler that writes to `sys.stdout`, such as
  `logging.StreamHandler(sys.stdout)` added in a handler, no longer recurses until
  `RecursionError` (#263): the handler's copy of a `stdout write` trace record goes to
  stderr once, redacted, instead of being traced again
- What a handler thread abandoned at its timeout writes to descriptor 1 as the process
  exits, after `App.main()` has written its envelope, reaches stderr redacted instead of
  stdout in the clear (#263): descriptor 1 stays a pipe to stderr until the last such
  thread ends, and what the thread wrote is passed on before its secrets are forgotten
- Under `App.call`, a secret a handler prints split across two writes no longer reaches
  the host's stream unredacted (#261): what the call's threads print, on the calling
  thread, a handler worker, or a handler thread held past its timeout, is passed on a line
  at a time, each thread's held apart, as printed text is under `main()` and `exec`. A line
  without its end waits for the end (a carriage return too), for the call to return, or
  for a held thread to end; past 64 Ki characters it is passed on but for its tail. The
  text keeps its escapes as before

## [1.0.0rc23] - 2026-10-02

The 23th 1.0 release candidate: 2 additions and 2 fixes.

### Added

- `App(mcp=McpServe(args=ServeArgs, setup=..., exit_codes=...))` adds the `mcp serve`
  built-in (#239): an app serves its commands as MCP tools over stdio as one of its own
  commands, with startup flags parsed, validated, and listed in `--schema` like any
  command's, and a `setup(args, ctx, *resources)` that runs once before serving. A failure
  before serving answers with the usual envelope and exit code on stderr, with nothing on
  stdout; once serving, stdout and stdin carry only the protocol, and a stray `print()` or
  write to descriptor 1 still goes to stderr, and a handler or child reading stdin reads
  an empty stream. Closing stdin or stdout, `SIGINT`, or `SIGTERM` stops the server with
  exit 0 and one audit log entry for the run. The manifest marks the
  command's stdout as a protocol in its description; `exec` and `App.call` refuse it with
  `NEEDS_STDIO`

- `McpServe(tools=provide, instructions=...)` serves tools from runtime data beside the
  command tools (#240): `provide(args, ctx)` runs once as serving starts and returns
  `treaty.McpTool` values, each with a name, a description, an input JSON Schema that
  every call's arguments are checked against, hints from `danger_level` or explicit
  `read_only` and `destructive`, and a handler whose result is enveloped and checked
  against its output schema like a command's. A tool advertised destructive
  (`danger_level="destructive"` or `destructive=True`) runs only with
  `confirm_destructive: true`, which its input schema gains; without it the call exits 2
  with `CONFIRMATION_REQUIRED` and nothing runs. A property marked `writeOnly`,
  `format: "password"`, or `x-secret` is a secret wherever the schema reaches it (nested
  properties, array items, `additionalProperties`, `patternProperties`, a local `$ref`, or
  a branch of `allOf`, `anyOf` and `oneOf`), redacted in the audit log, validation errors,
  and whatever the call writes. A name clash, an invalid schema, or empty
  instructions are refused before serving. `instructions=` takes text or a function of the
  startup arguments.
  `mcp serve --list-tools` prints the tool list, provided tools included, and
  `mcp-validate --serve-args` compares them

### Fixed

- A numeric secret, such as an `int` secret flag, is redacted by value, not only inside
  strings: echoed as a number in `Exit` context, a warning's context, a `ctx.log` field,
  the audit log's arguments, or an `exec_fallback` line's data, it came back raw, and now
  reads `[REDACTED]`. An equal number matches (987654.0 is the secret 987654), and so does
  one whose spelling holds it, as a string's does (-987654, 9876540); a number
  spelled in fewer than 4 characters stays, as a short string secret does, and a bool is
  never one

- Printed text, from `print()` or `sys.stdout.write`, reaches stderr a line at a time,
  redacted whole, as descriptor 1's text does: a declared secret split across two writes
  went to stderr in the clear. A partial line now waits for its newline or carriage
  return, the next envelope, or the run's end, so a `ctx.log` line written in between
  comes first. Each line of a multi-line secret, such as a PEM key, with at least 4
  characters besides its indentation, is redacted on its own too, so the key written a
  line at a time by `print`, `os.write(1, ...)`, or a child process no longer reaches
  stderr line by line (#256)

## [1.0.0rc22] - 2026-10-02

The 22th 1.0 release candidate: 2 additions, 3 changes, 1 fix, and 1 breaking change. Not
additive over rc21: see Breaking.

### Breaking

- Per-command `--schema` no longer has `requires_groups`: its `requires` lists a command's
  `RequiresAny` and `RequiresOne` rules as `{"any_of": [...]}` and `{"one_of": [...]}`
  among its other rules, as the manifest does, so a consumer reads `requires` alone

### Added

- `ctx.network.timeout(own)` and `ctx.network.fits(seconds)` hand a client of the
  handler's own, such as a library's `requests.Session`, the command's deadline:
  `timeout=ctx.network.timeout(30)` is `min(30, ctx.remaining)`, exiting 10 `TIMEOUT`
  when no time is left, and `fits` says whether one more attempt, its backoff included,
  still ends in time, so a retry budget stops and the handler answers with the failure
  it got instead of `TIMEOUT`. Both take seconds or a `treaty.Timeout`. The README shows
  the recipe for `requests`, `httpx`, and a urllib3 `Retry` (#237)

- The `network-timeout` audit rule advises on a `has_network_io=True` command whose
  handler reaches none of `ctx.http`, `ctx.remaining`, `ctx.timeout`,
  `ctx.network.timeout()`, `ctx.network.fits()`, and `ctx.run`: a client of its own may
  wait longer than `--timeout` allows, with the fix `timeout=ctx.network.timeout(30)` per
  call (#237)

### Changed

- `ctx.remaining` ends a reserve before the command's hard limit: a tenth of the timeout,
  at least 100 ms and at most 2 s, and never more than half of it. `ctx.http`, `ctx.run`,
  `ctx.pipeline`, `ctx.lock`, `ctx.retry`, and an async handler's cancellation all stop at
  that deadline too, so a handler that catches a call cut at its deadline still has time
  to return its partial result before exit 10 `TIMEOUT`. `ctx.remaining` reads that much
  less than before and `ctx.expired` turns true that much sooner; the hard limit and
  `meta.timeout_ms` are unchanged

- The README and the conditional rules' docstrings say a boolean flag counts toward
  `RequiresAny`, `RequiresOne`, and `Excludes` only when true: an explicit `--no-x`, or
  `false` in `--raw-payload`, is the same as leaving the flag out. The behaviour is
  unchanged, and a test pins it (#241)

### Fixed

- A `ctx.http` request ends at the deadline as a whole, not only each socket read: a server
  that sent its answer a little at a time kept a request running seconds past the time
  left, so a handler watching `ctx.remaining` lost its partial result to `TIMEOUT`. The
  connection is now shut at the deadline and the request fails with exit 10 `TIMEOUT`, also
  when a body without a length would have looked complete

- A declared secret written straight to descriptor 1, by C code, `os.write(1, ...)`, or a
  child process that inherited it, reaches stderr redacted, as printed text does; it went
  there in the clear. The interceptor redacts a line at a time, so a secret split across
  two writes or two reads of the pipe is still caught, and passes on a line without its end
  when the next envelope is written or the run ends, while the run's secrets are still
  known; bytes that are not UTF-8 pass through unchanged around it (#254)

## [1.0.0rc21] - 2026-10-02

The 21th 1.0 release candidate: 1 fix.

### Fixed

- The long-running work chapter's `import-all` starts a feed only when the time left is at
  least twice the slowest feed so far took, not just the last one's duration, which started
  a feed with no room for it to run slower: on a loaded machine that feed ran into the
  limit and the run exited 10 with `TIMEOUT`, losing the result of the feeds already
  imported. The chapter explains the margin, and its tests time one feed on the runner and
  set their limits from it, so a loaded runner's first feed does not outlast a limit fixed
  in seconds

## [1.0.0rc20] - 2026-10-02

The twentieth 1.0 release candidate: 2 changes.

### Changed

- The manifest's `requires` lists a command's `RequiresAny` and `RequiresOne` rules as the
  `ConditionalRule` shapes `{"any_of": [...]}` and `{"one_of": [...]}` (ManifestResponse
  3.2), in declaration order among its other rules, so an agent reading the manifest alone
  sees that one of the group's flags is required. `--schema`'s `requires` lists them too,
  and keeps `requires_groups`; a manifest without such rules is unchanged
- An `Excludes` between two flags of one `RequiresOne` fails registration: the `one_of`
  already forbids the pair and replaces the pairwise rule (REQ-C-026), so the manifest
  never lists both and a call is never refused twice for one mistake

## [1.0.0rc19] - 2026-10-02

The nineteenth 1.0 release candidate: 1 change and 1 fix.

### Changed

- The manifest is ManifestResponse 3.15, the version CI's spec pin defines, and states in
  keys what it said in descriptions: `idempotent: true` (3.15), `confirm_flag` naming a
  `Flag(confirm=True)` (3.14), `stderr: "child_log"` (3.11), and a passthrough command's
  `arguments: "passthrough"` with `help_argv` from `help_command=` (3.9), each in place of
  its sentence in the command's or the flag's description. A passthrough entry's `flags` is
  empty, since its flags go before the path as global options do; `--schema` still lists
  them, and `treaty audit --baseline` does not report them removed. `output_file` is
  `"envelope"` on a passthrough command and `"handler"` where the app's own `output` field
  takes the path (3.6), and `output_file_base` is `project_root` or `resource` when a
  relative `--output` does not land in the working directory (3.7). An object flag is
  `type: "object"` (an `array` of them stays `array`) with the value's `schema`, in place
  of `string` and its shape in the description (3.7). `stdin` declares the mode of every
  command that reads stdin, `buffered`, `lines`, or `records` with its `record_schema`, the
  `exec` built-in's `buffered` included, with `max_bytes` or `max_line_bytes` when the app
  changed the cap (3.8). The root `format` flag's `media_types` names what each value
  outside the spec's table writes: an `app.format(media_type=)` and treaty's own `ndjson`,
  `csv`, `yaml`, and `markdown`; a command's `output_media_types` names what its own formats
  write, in place of the "--format html writes text/html" sentences (3.12). A secret setting
  is in the root `secret_env_vars` with its declared names (3.13). An app without these
  features sees `schema_version`, the etag, the `format` flag's `{"ndjson":
  "application/x-ndjson"}`, and `exec`'s `stdin` change. The MCP `idempotentHint` is true on
  an `idempotent=True` command too, not only a `safe` one. `child_log=True` on a passthrough
  command, and an `app.format` or `FormatRenderer` media type for `plain` or `tsv` other
  than the spec's, are now a `RegistrationError` (#231)

### Fixed

- A `stdin_records=` command ends its input at REQ-O-004's `{"_summary": true, ...}` line
  after bare records, as the manifest's `stdin` mode `records` tells an agent to feed it,
  instead of failing that line with `RECORD_INVALID`. A passthrough command that also sets
  `output_file=` is `output_file: "envelope"` in the manifest, as its `--output` file holds
  the envelope, not `"formatted"`. `treaty changelog-add` no longer records an upgrade to
  this manifest as breaking for a passthrough command's flags, now listed before its path,
  or an object flag's new `object` type, which takes the same argv token. A secret field
  that shares a secret setting's `<APP>_<NAME>` no longer repeats it in the command's
  `secret_env_vars`, as the root `secret_env_vars` lists it (#231)

## [1.0.0rc18] - 2026-10-02

The eighteenth 1.0 release candidate: 1 change and 1 fix.

### Changed

- `@app.command(..., idempotent=True)` registers on a `safe` or `destructive` command, as
  REQ-C-002 requires, instead of raising `RegistrationError`. On a `safe` command it is
  redundant and accepted without warning; on any danger level the manifest description
  ends with the idempotent note, and the declared exit codes are unchanged. The
  `retryable` audit rule passes an idempotent destructive command's retryable codes as it
  does a mutating one's, and its fix on a destructive command now names `idempotent=True`
  too (#226)
### Fixed

- `cleanup` no longer removes an `output`, `credential`, or `config` path that its
  declaration reaches through a symlink it never removes through, such as a
  `{project_root}/tmp/dashboard/` output whose `<project>/tmp` links to a scratch directory
  another command declares as `temp`: the match still joins the kept set by its resolved
  path. A path a `temp`, `cache`, or `log` glob spells in another case on a
  case-insensitive filesystem is compared by file identity too, so it no longer slips
  past the kept check (#227)

## [1.0.0rc17] - 2026-10-02

The seventeenth 1.0 release candidate: 2 additions.

### Added

- `SideEffect(path, "output")` declares a location a command writes as its product, such
  as a rendered dashboard under `{project_root}/tmp/dashboard/`, including its default when
  `--output` is absent; a per-call `--output` path stays declared by `output_file=`. The
  manifest emits `type: "output"` (ManifestResponse 3.10), `status --show-side-effects`
  lists it with its size, and `cleanup` never removes it under any `--scope`: nor a
  `temp`, `cache`, or `log` path, or a handed-out output file, that is the output path,
  holds it, or lies inside it (`CLEANUP_KEPT`). `ttl_seconds` or `clearable_with` on an
  `output` side effect is a `RegistrationError`. A `safe` command whose only writes are
  `cache`, `log`, `temp`, or `output` paths stays `safe` and passes the `fs-side-effects`
  audit rule, whose fix now names the `output` kind too (#184)
- A streaming command can be `danger_level="mutating"`, for a watch loop that acts on what
  it sees. Each event carries its own `effect`, checked as a single response's is: an
  event without one, with an unknown value, or with a live value in a dry run ends the
  stream with `INVALID_EFFECT` before it is written. The terminal envelope counts the
  events per effect in `meta.effects` (`{"created": 2, "noop": 1}`), as do `--no-stream`,
  `exec`, `App.call`, and MCP. The command's dry-run flag covers the whole stream: every
  event reports a `would_*` effect and every line, the error envelope included, carries
  `meta.dry_run: true`. A stream has no idempotency replay, so it gets no
  `--idempotency-key`, no implicit `CONFLICT` exit code, and no `<APP>_SESSION` dedup; a
  field named `idempotency_key` on one is refused at registration. A failure after a live
  effect other than `noop` is `retryable: false`, and the audit log writes one entry per
  run with its counts in `effects`. A `destructive` stream, and a streaming command with
  `config_write_scope=`, are still refused at registration. Implements cli-agent-spec
  ResponseEnvelope 2.2 and AuditLogEntry 1.1 (#175)

## [1.0.0rc16] - 2026-10-02

The sixteenth 1.0 release candidate: 1 fix.

### Fixed

- Security: a handler's own `print()` or `sys.stderr` write during `App.call` no longer
  reaches the host's streams with its secrets in it, on the calling thread, on the worker
  a timeout runs it on, or in the window between its timeout and the call's return. While
  an `App.call` runs, `sys.stdout` and `sys.stderr` redact what the call's threads write of
  every live run's and handler thread's secrets, and keep it on the stream it was written
  to; `App.call`'s envelope is unchanged. Other threads' writes, and bytes written through
  `.buffer`, pass through untouched, `app.run`'s own stand-in keeps `sys.stdout`, and the
  streams are restored as the call returns, unless the host replaced one meanwhile. A
  worker that outlives its timeout hands over to the late wrapping of #135. Under
  `treaty-mcp`, whose `sys.stdout` is stderr, a handler's print is now redacted there
  (#141)

## [1.0.0rc15] - 2026-10-02

The fifteenth 1.0 release candidate: 1 change and 1 fix.

### Changed

- A command's manifest `output_formats` lists every format it takes beyond `json`,
  `jsonl`, `tsv`, `plain`, and `ndjson`: `id` where it has an `id_field`, the formats it
  inherits from `app.format(...)`, then its own. An agent assumes no format the entry
  leaves out, so a command of an app registering `csv` read as one that cannot write it.
  The manifests and `--schema` of apps that register a format change: each entry gains
  those names; other apps' manifests are unchanged. `<APP>_FORMAT=id` is passed over by a
  command without an `id_field`, which answers in its default instead of exiting `2`, as
  for another command's own format; `--format id` there still exits `2`, and neither its
  `--help` nor its shell completion offers it (#216)
### Fixed

- The `declared-exits` audit rule follows a method called on a parameter annotated with
  the args class or a resource class, such as `store.load()`, and the methods it calls on
  its own `self`, so the tutorial's `STORE_CORRUPT`, raised in `Store.load`, is found when
  a command does not declare it, instead of failing only at run time as
  `UNDECLARED_EXIT_CODE`. A method on an object of unknown class, or on a parameter the
  function binds again (a loop target, a nested function's parameter), is not followed
  (#218)

## [1.0.0rc14] - 2026-10-02

The fourteenth 1.0 release candidate: 3 additions.

### Added

- The `declared-exits` audit rule warns for each exit code a command's handler raises
  without declaring it, which passed `treaty audit --strict` and failed only at run time
  as `UNDECLARED_EXIT_CODE`. It reads `Exit.NAME` and `CliExit(ExitCodeName("NAME"))` in
  the handler, the first-party functions it calls, and its resources' `acquire`, names the
  file and line, and skips the codes a command gets without declaring them; a declared
  code never raised stays silent. An exit in the args class's `__post_init__` is reported
  declared or not, as the run reports it as `HANDLER_CRASHED`; the fix is a `ParseError`
  (#211)

- `@app.command(..., idempotent=True)` declares that a repeat of a mutating command with
  the same arguments leaves the same state: the `retryable` audit rule passes its
  retryable codes, and its manifest description says so, as the spec's CommandEntry has
  no key for it. The rule's fix names the option instead of asking to "confirm" the
  command is idempotent. A safe or destructive command refuses it, and it changes no run:
  `--idempotency-key` still replays the first result (#210)

- A command's `renderers=` can name a format the app does not register, such as
  `renderers={"html": render_page}`: that command alone offers it. Its manifest entry
  lists it in `output_formats`, its `--help` and completion offer it, and the root
  `--format` values, root `--help`, and other commands leave it out. `--format html` on
  another command exits `2` listing that command's formats; `<APP>_FORMAT=html` is passed
  over there, which answers in its default instead of failing. `treaty.FormatRenderer(render,
  media_type="text/html")` states what a command's renderer writes, in its manifest
  description, as the output_formats list has no room for it. A command's renderer still
  wins over the app's for a name both declare (#209)

## [1.0.0rc13] - 2026-10-02

The thirteenth 1.0 release candidate: 2 additions and 3 fixes.

### Added

- A mutating command can preview unless its own confirmation flag is passed:
  `yes: bool = Flag(confirm=True, description=...)`. Without `--yes` the run is a dry run
  under the `--dry-run` contract: a `would_*` effect, `meta.dry_run: true`, and nothing
  stored under an idempotency key; with it the command runs, and the flag keeps its name.
  A missing value is a preview on argv, `--raw-payload`, `exec`, and `App.call` and MCP,
  and `exec --dry-run` previews even a line that passes it. The flag's manifest
  description says the command previews without it, and generated skills say so too. A
  confirmation on a safe or destructive command, on a non-boolean field, with a default
  other than `False`, with `env=` (a variable left set would confirm every run), or
  beside a `dry_run` switch is a `RegistrationError`. The audit log records a preview's
  `args` with `dry_run: true` (#197)

- A `SideEffect` path may start at the project: `SideEffect("{project_root}/tmp/dashboard/",
  "cache")` on a command declaring `project_root=` markers, so a safe command that writes
  its regenerated reports under the project can declare them and pass `fs-side-effects`.
  `cleanup` and `status` resolve the project from their own cwd up; with no marker found
  (one at `/`, in the home directory, or above it counts as none) they leave its paths alone with a `PROJECT_ROOT_NOT_FOUND` warning instead of guessing
  the cwd, and `cleanup` never follows a symlink out of the project. A project path on a
  command without markers, or one reaching out with `..`, is a `RegistrationError`. The
  manifest carries the template as declared (#184)

### Fixed

- Commands that run children at the same time from several threads no longer crash on
  Windows with `PermissionError: [WinError 5] Access is denied` rewriting the session's
  `children.pids`, and no longer lose a child's pid from it on any platform: the
  rewrites of the file take turns, and a rename another process blocks for a moment is
  retried (#213)

- `--help` shows a control character, terminal escape, or Unicode bidi override in the
  app's own text (the app, group, command, and flag descriptions, and the examples) as its
  escape, such as `\x1b` or `\u202e`, as plain output does, instead of writing it to the
  terminal; newlines and tabs keep their layout. The zsh completion menu's descriptions
  are escaped the same way (#203)

- Plain output no longer prints the trust tags of external content as data: the data
  block of an `external=True` command, and the stderr error block of a context with
  `treaty.External`, show one `(external content, untrusted)` line instead of
  `_source: external` and `_trusted: false`, in a table too, and so does a plain
  `--output` file. JSON, jsonl, ndjson, and tsv keep the tags, and a field named
  `_source` on a command that is not external still prints. A boolean in the stderr error
  block reads `true` or `false`, as in the rest of plain output, not Python's `True` or
  `False` (#198)

## [1.0.0rc12] - 2026-10-01

The twelfth 1.0 release candidate: 1 change, 7 fixes, 5 additions, and 3 breaking changes.
Not additive over rc11: see Breaking.

### Changed

- The README says a passthrough command's delegated parser follows its own colour rules:
  Python 3.14's argparse colours its help when `FORCE_COLOR` is set, even off a terminal,
  and `NO_COLOR` turns that off (#170)
### Fixed

- An `app.output_adapter` type raised as a failure's data, `raise Exit.X(..., data=obj)`,
  keeps the array order its schema declares with `x-ordered`, nested arrays included, as it
  does when returned; before, every array in it was re-sorted. A dataclass keeps its
  `Out(ordered=True)` order as before, and a plain dict is still sorted (#181)
### Fixed

- A `mutating` or `destructive` command may return a class an `app.output_adapter` writes,
  when the adapter's schema lists `effect` (and `would_affect` for a destructive command):
  registration failed with "must return an object with an 'effect' field" whatever the
  schema said. Each run checks the dumped object's `effect` and `would_affect` as it checks a
  dataclass's, so `would_*` dry runs, `meta.dry_run`, the destructive preview, and the
  idempotent `noop` replay hold. An adapter schema that lists the key as `x-volatile`, which
  `--stable-output` leaves out, is a `RegistrationError` saying so; the same holds for
  `open_url`, `background_pid`, and `cleanup_command` (#183)
### Fixed

- The `stable-order` audit rule warns about a `list[X]` or `tuple[X, ...]` of a class an
  `app.output_adapter` writes, returned by a command with neither `sort_key=` nor
  `ordered=True` or held in a dataclass field with no `Out(sort_key=...)` or
  `Out(ordered=True)`, as it does for dataclasses: treaty re-sorts such an array by its
  items' JSON text, so a ranking such as search results lost its order with a clean audit.
  The fix names `sort_key=` for a stable listing and `ordered=True` for a ranking; a
  dataclass array's fix now says the same. `treaty audit --strict` may now flag an adapted
  list whose order is not declared, including one nested in a dataclass field or a dict;
  `ordered=True` or `sort_key=` on the command (or `treaty.Out(...)` on the field) clears
  it (#182)
### Fixed

- The `recursive-traversal` audit rule no longer warns that a circular symlink can loop
  `Path.rglob()`, `Path.walk()`, `os.walk()`, or `os.fwalk()`, which do not enter a
  symlinked directory unless asked, nor `shutil.rmtree()`, which never does. The loop
  warning stays for a call passing `recurse_symlinks=`, `follow_symlinks=`, or
  `followlinks=` as anything but a literal false (by name, by position, or in a `**`
  mapping), and for `glob(..., recursive=True)` and `shutil.copytree()`. A walk that cannot
  loop is advice instead: no `--max-depth` bounds it. `os.walk` is recognized under an alias such as
  `from os import walk as w`, and a module-level `walk` that is not a directory walk, such
  as `ast.walk`, is no longer flagged (#180)
### Fixed

- Plain output, tables, TSV and CSV cells, stderr error lines, log lines, and printed text
  show the Unicode bidirectional embeddings, overrides, and isolates (U+202A to U+202E,
  U+2066 to U+2069) as escapes such as `\u202e`, as they show C0 and C1 controls. Printed
  raw, one reordered how the rest of its line displays, so a crafted value could make a
  row read differently from what it holds; a table counted them as no width. The LRM, RLM,
  and ALM marks stay text, since right-to-left prose uses them, and JSON keeps all of them
  as data (#177)
### Added

- `ctx.run(argv, stream="always")` writes a long child's lines to stderr as plain text as
  they arrive, in any `--format` and verbosity, where `stream=True` makes them `ctx.log`
  lines that an agent off a terminal never sees, and sees as JSON log records under `-v`.
  The lines are redacted and escape-cleaned as `ctx.log`'s are, each written whole when two
  children stream at once; `--quiet` silences them, `App.call` and MCP drop them, and
  `Completed` keeps its 4096-character tails. The command declares
  `@app.command(..., child_log=True)`, without which `stream="always"` raises
  `RegistrationError`, and its manifest description then says stderr carries the child's
  log, since the spec's CommandEntry has no key for it. `stream=True` is unchanged (#173)
### Added

- `ctx.network`, a `treaty.NetworkSettings`, hands a `has_network_io=True` handler's own
  HTTP client, such as a library's `requests.Session`, the settings `ctx.http` goes out
  with, so `--proxy` and `--no-proxy` reach it too: `proxies`, a `requests`-style
  `{"http": ..., "https": ...}` mapping (`--proxy` for both, else `HTTP_PROXY` and
  `HTTPS_PROXY` in either case, empty under `--no-proxy`); `proxy_for(url)`, the proxy
  `ctx.http` would use for that URL with `NO_PROXY` applied; and `ca_bundle`, the `Path` of
  `REQUESTS_CA_BUNDLE` or `SSL_CERT_FILE`. A proxy URL keeps its credentials for the
  client, and the object's repr removes them. Reading `ctx.network` in a command without
  `has_network_io=True` is a `RegistrationError`, as `ctx.http` is. The `http-client` audit
  rule now advises on a network command whose handler reaches none of `ctx.http`,
  `ctx.network`, and `ctx.run`, since `--proxy` would not reach a client of its own (#171)

### Fixed

- A malformed proxy setting no longer shows its password in the `PROXY_INVALID` or
  `--proxy` error: one written without a scheme (`user:pass@host:port`), or whose password
  holds a `/`, `?`, or `#`, now loses everything up to its last `@`
- `NO_PROXY` matches an IPv6 host, bare (`::1`) or in brackets (`[::1]`, `[::1]:8080`),
  and a `host:port` entry matches a URL that names no port when the port is the scheme's
  own, as `example.com:443` does `https://example.com/`; `ctx.http` and `ctx.network` both
  follow it
### Breaking

- `SUBPROCESS_FAILED` marks its `context.stderr` as content from outside the tool:
  `error.context` gets `"_source": "external", "_trusted": false`, a stderr that is a
  high-entropy value is masked, and a handler that catches the `CliExit` from `ctx.run`
  finds `treaty.External` in `exc.context["stderr"]`, the text in its `.value` (#174)
- A failure's `data` on an `external=True` command, from `raise Exit.X(..., data=...)`, a
  destructive preview, or a partial run of steps, is tagged
  `"_source": "external", "_trusted": false` with an `UNTRUSTED_CONTENT` warning, as a
  success's is; before, only a success's `data` and a batch's were (#174)

### Added

- `treaty.External(value)` marks a top-level value of a failure's `error.context` as
  content from outside the tool, such as the play log of a failed child:
  `raise Exit.PLAYBOOK_FAILED(msg, context={"output": External(tail)})`. A marked value is
  masked as external `data` is (`HIGH_ENTROPY_MASKED` names `error.context.<key>`), the
  other context values are left alone, and `error.context` gets
  `"_source": "external", "_trusted": false` with an `UNTRUSTED_CONTENT` warning, unless
  `--no-injection-protection`. `External` nested in a context value or in `data`, or in
  the context of an `ARG_ERROR`, a `ParseError`, or an `InputRequired`, is `INVALID_EXIT`,
  so its text never reaches the agent unmasked. The `external-data` audit rule also warns when a
  `context=` value is built from a `ctx.run` result's `stdout` or `stderr` without
  `External`, whatever the command declares (#174)
### Breaking

- A plain flag that declares `Flag(env=(...))` reads its own `<APP>_<NAME>` first, then the
  declared names in order, as a secret and a setting already do: with both
  `CLOUDFALL_PROJECT` and `DEPLOYCTL_PROJECT` set, `--project` takes `DEPLOYCTL_PROJECT`,
  where rc11 read only `CLOUDFALL_PROJECT`. The spec's REQ-F-073 allows a variable outside
  the prefix only after the prefixed name. A flag without `env=` still reads no variable.
  The flag's `<APP>_<NAME>` is now a variable it reads, so a registration where a setting,
  a framework option, or another command's secret or token reads it too is a
  `RegistrationError`, as is naming it in `env=`; a deprecated name's warning names it as
  the replacement instead of the flag (#178)

### Added

- The manifest is ManifestResponse 3.5. A flag that reads variables lists them in
  `env_vars`, `{name, deprecated?}` in precedence order with `<APP>_<NAME>` first, in place
  of the "(read from $A or $B when not passed)" prose in its description; `--format`,
  `--max-output`, `--config`, `--context`, `--instance-id`, and `--no-update-check` list
  their `<APP>_*` variable too. The new root `env_vars` lists, each with a `description`, the
  variables that back no flag: `<APP>_MAX_STDIN_BYTES`, `<APP>_STATE_DIR`,
  `<APP>_SESSION`, `<APP>_AUDIT_LOG`, and each plain setting's `<APP>_<NAME>` and declared
  names. A secret setting is left out, since root `env_vars` holds no secret. `--help` and
  AGENTS.md list a plain flag's `<APP>_<NAME>` (#178)
- `app.format("html", render=..., media_type="text/html")` offers a format `Format` does not
  list. The name, lowercase letters and digits with words joined by `-` or `_`, joins
  `--format`'s values, `<APP>_FORMAT`, the manifest's enum, `--help`, and completion, and a
  command's `renderers={"html": ...}` overrides it. It runs as `plain` does: errors go to
  stderr as prose, `--output` writes the rendered text, and `--output html` is refused as a
  format name. `media_type` is stated in the `--format` description, as `FlagEntry` has no
  key for it. A string naming a `Format` member is that member, and `json`, `jsonl`,
  `ndjson`, and `id` still take no renderer. The new `treaty.FormatName` is one `--format`
  value: `App.formats` holds them, and a member's equals and hashes like the member, so
  `Format.CSV in app.formats` still holds. The new `ctx.format_name` is the value the CLI
  caller asked for (`jsonl` too, which runs as `json`; `json` for an `exec` line or
  `App.call`), so a handler tells `html` from `plain`, whose `ctx.mode` is
  `Format.PLAIN` for both. `--output` is argv only (an `exec` line and `App.call`
  refuse an `output` key), so the refusal of `--output html` covers every path that
  writes a file (#179)

## [1.0.0rc11] - 2026-10-01

The eleventh 1.0 release candidate: 4 additions and 1 change.

### Added

- A settings field can keep a variable its users already export, outside the app's prefix:
  `Flag(default=..., description=..., env=("BEANCOUNT_FILE",))` reads the declared names in
  order after `<APP>_<FIELD>` and before the config files. `--show-config` reports the
  source as `env:BEANCOUNT_FILE`, `--help` and AGENTS.md list the names, a bad value is
  `CONFIG_INVALID` naming the variable it came from, and the `env-prefix` audit rule
  accepts them. The new `EnvName("OLD", deprecated=Deprecated("1.4.0"))` keeps reading an
  old name with a `DEPRECATED_ENV_VAR` warning naming the variable to use instead; a name is
  not deprecated unless declared so. A name that is not a variable name, repeats, is the
  field's own `<APP>_<FIELD>`, or is read for another field or a framework option is a
  `RegistrationError`, as is a plain setting naming a variable a command reads as a secret
  or token, which `--show-config` would print (#7)
- A command's flag takes the same `env=(...)`, so it reads the variable a wrapped service
  already uses, such as `IBKR_FLEX_TOKEN`, when it is not passed, on every input path:
  argv, `--raw-payload`, `exec` lines, and `App.call` and MCP. A secret reads its own
  `<APP>_<NAME>` first, and its declared names join `secret_env_vars` in the manifest; its
  value stays redacted everywhere, and no plain flag of another command may read a variable
  a secret or token reads. A plain flag reads only its declared names, which its
  manifest description lists; a required one shows `required: false` in the manifest and the
  `--raw-payload` schema, since a variable may supply it. A value read from a variable is
  validated as strictly as a passed one, and its error names the variable in
  `context.source`. `--help`, AGENTS.md, the `env-prefix` audit rule, and
  `DEPRECATED_ENV_VAR` work as for settings (#9). An object flag reads its JSON object from
  the variable, checked as on argv; a list of objects cannot declare names, since a comma
  cannot split JSON, and is a `RegistrationError`
- A command declared `output_file=True` that returns `treaty.Binary` writes the raw bytes
  to `--output`, atomically and whatever the `--format`, instead of their base64 wrapper
  in the `--format` representation; `data` is `{path, bytes, content_type, sha256}`, with
  `sha256` in lowercase hex and `content_type` only when the command declared one. A
  failed run still writes no file, the output cap bounds the envelope and never the file,
  and without `--output` the bytes stay base64 in `data`. A relative `--output` lands in
  the base `output_file=` declares, as for the `--format` representation, and the
  `--output` description names both the raw write and that base. `--output -` on such a
  command exits `2`, as stdout carries only the envelope; other commands still write a
  file named `-`. The new `binary-output-file` audit rule, an advice, suggests
  `output_file=True` for a command returning bytes without it (#10)
- The manifest is ManifestResponse 3.3: the `CommandEntry` of a command declared
  `output_file=`, with any base, carries `output_file`, `"binary"` when it returns
  `treaty.Binary` and its `--output` gets the raw bytes, `"formatted"` when the file gets
  the `--format` representation. A passthrough command, whose `--output` gets the JSON
  envelope whatever the `--format`, and an app's own `output` flag have no key, a
  departure from REQ-O-001 listed in COMPLIANCE.md until cli-agent-spec#27 settles it.
  `schema_version` is `"3.3"`; the spec's 3.2 adds `ConditionalRule` `any_of` and
  `one_of`, which treaty's manifest does not emit, as only `--schema` shows a group rule
  (#10)

### Changed

- `--format plain` without a renderer prints a list of objects whose values are all
  scalars as an aligned table: a header of the field names in the output dataclass's
  field order, one row per object, numbers and `Decimal` fields right-aligned, `null` as an
  empty cell, and `(no rows)` for an empty `list[T]`. A list of models an output adapter
  writes, such as pydantic's, is a table in the order its dump writes the keys, its
  integer and number properties right-aligned. Fifty items read as fifty lines instead of
  fifty `key: value` blocks. When `COLUMNS` is a positive integer, a wider
  table cuts its widest text column with an ellipsis, down to four cells, and never cuts a
  number; `COLUMNS` joins the unprefixed conventions, so AGENTS.md lists it beside
  `NO_COLOR` and `TERM`. `Out(table=False)` leaves a field out of the table without
  touching JSON or the output schema. A list holding a nested value keeps the
  `key: value` blocks; JSON, `jsonl`, `ndjson`, and `tsv` do not change. Upgrading: rerun
  `treaty agents-md` on each app after upgrading, or `treaty check-docs` exits 81 on the
  Environment Variables section of its AGENTS.md, which now lists `COLUMNS` (#8)

## [1.0.0rc10] - 2026-10-01

The tenth 1.0 release candidate: 5 fixes and 11 additions.

### Fixed

- A warning reaches stderr in a text format: under `--format plain`, `tsv`, or a format
  an app registers with `app.format`, the stdout text has no room for the envelope's
  `warnings`, so a `ctx.warn` call, or a framework warning such as `CWD_CHANGED` or
  `CLEANUP_FAILED`, was lost. Each is now one `warning: <CODE>: <message>` line on stderr
  after the result, redacted and cleaned of escapes as a `ctx.log` line is, and kept off
  by `--quiet`; a stream writes each once, after the event it arrived with. A warning
  treaty already writes in text mode, such as a deprecated flag's notice, the
  `--no-injection-protection` notice, or a `--token-limit` cut, is not written twice.
  stdout and the exit code are unchanged (#152)
### Added

- Object arguments: a flag annotated with a frozen dataclass, `X | None`, or
  `tuple[X, ...]` takes a structured record, nesting other objects and lists of them up to
  8 deep. `exec` lines, `--raw-payload`, `app.call(...)`, and MCP carry it as a JSON
  object; on argv each repeat of the flag takes one JSON object, and `from_stdin=True` one
  per line. Each field goes through the same checks as a flag's value (`Decimal`, `Path`,
  enums, `Literal`, `app.scalar` types, and `Flag(pattern=, max_bytes=, multiline=)` on
  the dataclass's fields), an unknown or missing key is refused, and every error is one
  `error.errors` entry at its location, such as `postings[1].number`, exit 2. `--schema`
  and the MCP `inputSchema` carry the object's schema, and the manifest and `--help` show
  its shape. An object holds no secret (REQ-C-016): a nested field under a secret's name
  or declared `secret=True` fails registration naming its path, such as
  `postings[].token`, so it becomes a top-level flag read from `--x-from-env` or
  `--x-from-file`. An object flag cannot be positional, a secret, a setting, or a
  `Subprocess(user_controlled_args=)` field (#6)
### Added

- `app.output_adapter(base, schema=..., dump=...)` lets a handler return a class treaty
  does not know, such as a pydantic `BaseModel`, with no dataclass mirror: one
  registration covers every subclass, returned at the top, in a `list` or `tuple`, as
  `Model | None`, or in a dataclass field. treaty imports no pydantic. The output schema
  comes from `schema(cls)` with its `$defs` inlined and every key required, and `data`
  from `dump(obj)`. The stable-output rules are checked on that schema at registration:
  a null list or dict is refused unless `none_as_empty=True` writes it as `[]` or `{}`,
  and a property declares the `Out` options as `x-sort-key`, `x-ordered`, `x-volatile`,
  `x-high-entropy`, and `x-external` (pydantic's `Field(json_schema_extra=...)`), each
  optional. `treaty audit` advises declaring the order of an adapted array of objects
  and notes an untyped `dict[str, Any]` value. `OutputAdapter` is exported (#2)
- Security: a declared secret a handler passes to `ctx.warn`, in the message or as a
  context value, no longer reaches the response's `warnings` unredacted. Each warning's
  `message` and context strings are redacted of every live run's and handler thread's
  secret values where the envelope is built, as `error.message` is, so JSON and JSONL
  output, stream events, `exec` lines, `App.call` and MCP results, and the envelope
  describing an `--output` write all carry the redacted text. Context keys stay as given
  (#162)
- A dry run treaty switches on is built in phase 1: the args of a `safe_default` command
  run without `--live`, or of a destructive command run without `--confirm-destructive`,
  are rebuilt with the dry-run switch before anything runs, and what `__post_init__`
  raises there is answered as at parse time, where it escaped as a traceback: a
  `ParseError` or `InvalidValue` is exit 2 `ARG_ERROR` with a suggestion naming `--live`
  or `--confirm-destructive`, anything else exit 1 `HANDLER_CRASHED`, the same answer
  as passing `--dry-run` explicitly. That holds on the command line, an exec line,
  `call()`, MCP, and `--validate-only` alike, and for the rebuild that rebases relative
  paths under `--cwd`; the args are rebuilt once rather than again when the handler
  starts (#161)
- Security: what a child process or a C extension writes to file descriptor 1 under
  `App.main()` or `intercept_stdout()` reaches stderr cleaned, as a stray `print()` is
  (#105): OSC (clipboard writes, links, titles), cursor movement and other CSI escapes,
  and C1 controls are gone, colors (SGR) stay only where the run may color, and other
  controls but tab, carriage return, and newline are shown as escapes. Before, the bytes
  were forwarded raw, so a child could write the clipboard or retitle the terminal. A
  character or escape split across two reads is held until it completes, an unfinished
  one for at most 4096 characters, and bytes that are not UTF-8 pass through unchanged.
  The `THIRD_PARTY_STDOUT` warning still counts the bytes as written. `ctx.run`'s
  `stream=True` lines and a failed child's stderr already reached stderr cleaned, now
  covered by tests; `Completed.stdout` and `stderr` stay as the child wrote them (#117)
### Added

- `stdin_input="lines"` reads a command's input one line at a time: `ctx.stdin_lines`
  yields each line as the producer writes it, with no total cap, so an NDJSON pipeline
  streams instead of hitting the 64 KiB `STDIN_TOO_LARGE` cap of `stdin_input=True`,
  which is unchanged. Each line is capped by `App(max_line_bytes=...)`, 1 MiB by default:
  a longer line exits 1 with `LINE_TOO_LARGE`, and one that is not UTF-8 with
  `LINE_NOT_UTF8`, each with the 1-based line number in `error.context.line`. CRLF, a
  final line without a newline, and a leading BOM are read as text lines. With
  `streaming=True` the command is a filter, writing each event as it goes, and every line
  read restarts the idle timeout. `--input-file` reads the lines from a file, a terminal
  on stdin exits 2 with `STDIN_IS_TTY`, and in `exec`, `App.call`, and MCP the request
  gives them as `input_lines`, an array of strings. `--schema` adds `"stdin_mode":
  "lines"` (#33)
- `stdin_records=Sec`, a frozen dataclass, reads another command's output as typed records:
  `ctx.stdin_records` yields one `Sec` per bare JSON line or per item of an envelope's
  `data`, and a stream's terminal envelope ends the input. An upstream `ok: false` exits 1
  with `UPSTREAM_FAILED` and the upstream error, redacted, in `context.upstream`; envelopes
  that stop before their terminal one exit 1 with `UPSTREAM_INCOMPLETE`; a record that
  fails its fields exits 1 with `RECORD_INVALID`, `context.line`, and `context.field`. A
  paged or cut upstream response adds an `UPSTREAM_TRUNCATED` warning. `--schema` gives
  the record type as `stdin_records_schema` (#32)

### Fixed

- A `streaming=True` command with `stdin_input=True` gets its payload: `ctx.stdin_text`
  was always None, as the stream never read stdin (#33)
- Security: a phase 1 error written by app code no longer carries a secret value it
  quotes. A `ParseError` or `InvalidValue` raised from an args `__post_init__` sees every
  secret argument in the clear, and its message, suggestion, `errors[]` items, and context
  values reached the envelope as written. They are now redacted of the run's secret
  values, as a handler's error is, on every path that answers with that envelope: JSON,
  the plain rendering on stderr, `exec` lines, `--raw-payload`, `--validate-only`,
  `App.call`, and MCP tool calls. That holds at parse time, for the rebuild of a forced
  dry run or under `--cwd`, and for an object flag's own `__post_init__`, whose crash
  report is now redacted of the run's secrets too. treaty's own phase 1 errors, which
  never echo a secret, read as before (#165)
- Args models: `app.args_adapter(base, schema=..., validate=...)`, returning a
  `treaty.ArgsAdapter`, lets a handler's arguments be a subclass of `base`, such as a
  pydantic `BaseModel`, instead of a dataclass; the input-side counterpart of
  `app.output_adapter`. The model's JSON Schema becomes the flags (a `treaty` key carries
  `positional`, `short`, and the other `Flag` options), so argv, `--raw-payload`, `exec`,
  `app.call(...)`, MCP, `--help`, completion, `--schema`, and `--validate-only` take it
  as they take an args dataclass. Phase 1 then calls `validate` with the parsed values,
  and each entry of a `ValueError`'s `errors()`, such as a pydantic `ValidationError`'s,
  is one `error.errors` item, exit 2; a password-format field is a treaty secret. treaty
  imports no pydantic (#102)
- `--format ndjson` (`Format.NDJSON`) writes `data` alone for `jq -c`, `duckdb`, `mlr`, or
  the next command in a pipe: one compact JSON line per item of a list result or event of a
  stream, a single result as one line, and nothing for `null` data, with keys sorted and
  values masked as in the envelope, and `--fields` applied. The exit code carries the
  status, and stderr gets one JSON line each for the error (`{"error": {...}}`), every
  warning, a cut page (`{"pagination": {...}}`), and `ctx.log` lines. `--max-output`
  caps it (REQ-F-052): a buffered answer writes whole records up to the cap, and a stream
  leaves out each record over the cap and goes on, as `jsonl` caps each envelope; each
  cut is one `{"truncation": {...}}` line and a `FIELD_TRUNCATED` warning on stderr, the
  exit code unchanged. The flag's description now says so. Every app offers it,
  so the manifest's `--format` enum and `--help` list it; it takes no renderer, and
  `--output ndjson` is refused as a format name. `jsonl` stays the envelope stream (#34)
### Added

- `treaty scaffold-from typer|click|argparse module:obj` writes a treaty module from an
  existing CLI: it imports the target, walks the click or typer command tree (typer's own
  copy of click included) or the argparse subparsers, and writes an args dataclass per
  command, group options on base classes, and a handler that returns its arguments with
  effect `noop`. Every command starts as `danger_level="mutating"` with `exit_codes=()`,
  which `treaty audit` reports until the author declares them; commands and options treaty
  provides are left out and options on reserved names renamed, both listed in `data`. The
  module goes to `--out` (exit 6 `CONFLICT` over an existing file without `--force`) or to
  `data.source`, and `--format plain` prints it alone. It passes ruff and `mypy --strict`,
  and runs once before it is written. The click/typer chapter gains Step 0, separate logic
  from presentation, the agentyper rows that need no open issue, and a before and after
  run of the conformance kit; the argparse chapter points at the scaffold. A default that may be a secret (a hidden option, a
  secret-like name, or the value of an environment variable) is left out of the module (#3)
- `app.resolves(argv)`, for a shim that routes a CLI moving to treaty a command at a time:
  true when argv names a registered command, by the longest path, so a half-migrated group
  splits (`transaction list` on treaty, `transaction add` on the old CLI). Global options
  before the path are skipped; built-ins and redirected paths resolve; groups, root
  `--help`, and `--version` do not. `App(exec_fallback=)` passes an `exec` line whose
  `_cmd` is no registered command to the old CLI's dispatcher, as `(cmd, payload)`, and
  wraps what it returns in a success envelope with `meta.exec_fallback`; a `ParseError` it
  raises is exit 2, a `KeyboardInterrupt` `CANCELLED` as from a handler, any other
  exception exit 1 `FALLBACK_FAILED`. Its lines are redacted,
  masked, capped, and audit-logged like a command's, never deduplicated, and refused under
  `exec --dry-run`. The click/typer and argparse chapters' shims use both (#28)
- `output_file=` takes the directory a relative `--output` lands in: `True` stays the
  working directory, `treaty.OutputBase.PROJECT_ROOT` the command's `project_root=`, and a
  resource class with a `directory` or a function `(ctx: Ctx, *resources) -> Path` a
  directory the app resolves itself, such as a `--project` it reads. An absolute `--output`
  is used as given, a failed run writes no file, and the `--output` description in `--help`
  and the manifest names the base. The `fs-side-effects` audit rule now suggests
  `output_file=True` for a safe command that writes to a path from one of its `Path` flags,
  instead of a cache side effect (#68)
### Added

- Passthrough commands: `@app.command(..., passthrough=True)` wraps another tool's own
  argument parser. The args type is `NoArgs`, `ctx.argv_rest` holds every token after the
  command path verbatim (`--help` and `--` included), and the handler returns the tool's
  exit code or lets its parser's `SystemExit` through; that code is the process exit code,
  with `DELEGATED_EXIT` when it is not 0. The tool owns stdout, descriptor 1 included, so
  the final envelope is the last line on stderr, and goes to `--output PATH` too.
  Treaty's flags go before the path; the timeout, signals, session deduplication by argv,
  and the audit log (with `argv` `[OMITTED]`) apply. `help_command=` is the argv the tool
  gets for a lone `--help`. The manifest stays within the spec's schema: the entry has
  `option_placement: "strict"` and a description ending in a sentence saying the arguments
  go to the delegated tool. Exec lines and `App.call` take `"argv": [...]`, MCP lists no
  passthrough command, and completion offers file paths after the path. COMPLIANCE.md lists
  the spec requirements a passthrough command departs from (#35)

## [1.0.0rc9] - 2026-10-01

The ninth 1.0 release candidate: 2 fixes.

### Fixed

- The output cap measures the envelope it falls back to when no cut of `data` fits: when
  the truncation meta and warning would pass the cap, an exec line or `call()` hint takes
  its short form, and a line that still passes the cap does so by that report alone. A
  response cut again after `--warnings-as-errors` or a failed audit log write grew it
  keeps the first cut's `meta.total_bytes`, the full response's size, and reports each
  field in a single `FIELD_TRUNCATED` warning against its original length (#134)
- Security: a handler abandoned at its timeout or on a signal no longer leaks a secret it
  prints or writes to `sys.stderr` after its run returned, as after a host's `App.call`.
  With no run's stand-in left on `sys.stdout`, the text reached the host's streams
  unredacted. Now, while such a thread lives, `sys.stdout` and `sys.stderr` redact what it
  writes of every live run's and handler thread's secrets, and send its stdout text to
  stderr, so it never lands in the host's own output; other threads' writes, and bytes
  written through `.buffer`, pass through untouched. The streams are restored once the
  thread ends, unless the host replaced one meanwhile (#135)

## [1.0.0rc8] - 2026-10-01

The eighth 1.0 release candidate: 1 change and 2 fixes.

### Changed

- The package metadata names the author, links the documentation and the changelog, and
  adds the `Environment :: Console`, `Operating System :: OS Independent`,
  `Programming Language :: Python :: 3 :: Only`, and `Typing :: Typed` classifiers. The
  README's links are absolute, so they resolve on pypi.org, and a test fails on a relative
  one (#122)

### Fixed

- `--max-output` and `<APP>_MAX_OUTPUT_BYTES` now bound the line as written, its newline
  included. The cut measured the JSON envelope without the newline that ends every line
  on stdout, so a cut that filled the cap exactly, which a string cut always does, wrote
  one byte over it; this held for a command's response, each stream event, and each
  `exec` line. `meta.total_bytes` counts the same bytes, so it is one more than before
  (#129)
- Security: a stream abandoned at its timeout or on a signal no longer leaks a secret its
  generator logs from `finally`. The generator was never closed, so it was finalized at
  garbage collection, after its worker was forgotten and the framework's handler had left
  the root logger, and the record went to `logging.lastResort` unredacted. Now the worker
  closes the generator as soon as its `next()` returns, while its secrets are still
  redacted. A handler on a worker thread also runs only once the worker is registered, so
  one that returns at once can no longer leave the framework's handler on the root logger,
  and `logging.basicConfig()` a no-op, until the next `App.call` (#128)

## [1.0.0rc7] - 2026-09-30

The seventh 1.0 release candidate: 2 fixes.

### Fixed

- The audit counts a distribution's other top-level packages as first-party only when the
  handler was loaded from that distribution: a file its RECORD lists, or a file under the
  project of its editable install. A local package that shadows an unrelated installed
  one of the same name keeps only its own package, so the source rules no longer follow
  helpers into the other distribution's packages. A `direct_url.json` that is not a
  PEP 610 record was taken as a non-editable install. Now the record of the distribution
  that ships the handler's package stops the audit with exit 4 and `PRECONDITION`,
  naming the distribution, and any other distribution's is skipped and named in the
  report's `scope` (#106)
- Security: a handler that outlives its timeout no longer leaks its secret through
  `logging` once no run is attached, as after `App.call` returned. The framework's handler
  left the root logger with the last run, so a WARNING the handler logged then went to
  `logging.lastResort` and `sys.stderr` unredacted. The handler now stays on the root until
  every such thread ends, and with no run attached writes a record at WARNING or above where
  `lastResort` would, the held threads' secrets redacted, and nothing when the host's own
  handler takes the record (#118)

## [1.0.0rc6] - 2026-09-30

The sixth 1.0 release candidate: 7 breaking changes, 12 additions, and 13 fixes. Not
additive over rc5: see Breaking.

### Breaking

- The audit log is off by default, as the spec's REQ-O-030 now says (it retired REQ-F-026,
  which had it on): a run of an app that configured nothing creates no file or directory,
  and `meta.audit_log_path` is absent. `App(audit_log=None)` is the default;
  `App(audit_log=treaty.AuditLog())` turns it on. `<APP>_AUDIT_LOG` wins over the app:
  `1` turns it on, `0` off, an absolute path on at that path. Any other value, an empty one
  included, exits 2 with `INVALID_AUDIT_LOG_SETTING` for every invocation except `--help`
  and `--version`, where `manifest` and `--schema` answered before. That includes `off`,
  which earlier releases documented: write `0` instead. An `AuditLog(path=...)` no longer
  beats the operator's path (#71)
- The default audit log path is `$XDG_STATE_HOME/<app>/audit.jsonl`, else
  `~/.local/state/<app>/audit.jsonl`. A log written by an earlier release under
  `$XDG_DATA_HOME/<app>/` (`~/.local/share/<app>/`) is neither moved nor read; delete it,
  or point `<APP>_AUDIT_LOG` at it (#71)
- `AuditLog` defaults to 10 MiB per file, 5 rotated files, and 30 days, O-030's own bounds:
  at most 60 MiB per tool where it was 600 MiB. This settles #53. The active file also
  rotates once its first entry is older than `max_age_days`, and rotated files that old are
  deleted before every append rather than once per process (#71)
- An audit entry matches the spec's `audit-log-entry.json`: `parameters` is `args`, with
  `validate_only`, `confirm_destructive`, and `no_injection_protection` added when given;
  `operator` is `session_id` (still read from `<APP>_SESSION`), present only when set, as
  `trace_id` is; `timestamp` is when the run started; `error_code`, `data`, and
  `data_bytes` are gone. A line is at most 16 KiB: the largest `args` values become
  `[TRUNCATED]` and `truncated` is `true`, where strings were cut at 1,024 characters (#71)
- Only invocations that resolve to a command are logged: an unknown command, `--version`,
  `version`, `manifest`, `completion`, and `audit-log` itself no longer append an entry
  (#71)
- `audit-log` is on every app, the treaty CLI included, since the operator can turn the log
  on for any of them. While the log is off it exits 4 with `AUDIT_LOG_DISABLED` (was
  `AUDIT_LOG_OFF`). `--since` takes a positive duration or an ISO 8601 time with a UTC
  offset (`0h` and a time without an offset exit 2), and `--command config` also matches
  `config set` and `config get`, but not `configure` (#71)
- While the log is on, the manifest lists its path as a `log` side effect of every command
  it records; the log's files are created `0600` and new directories `0700` whatever the
  umask (#71)

### Added

- `Flag(audit=False)` and `Arg(audit=False)` keep an argument's key in the audit log's
  `args` with the value `[OMITTED]`, for a value that is no secret but should not be
  kept, such as a message body. It still works on argv, in `exec`, `--raw-payload`, MCP, and
  `app.call`; the manifest adds "(omitted from the audit log)" to its description and the
  args schema marks it `"x-audited": false`. With `secret=True` the `[REDACTED]` wins.
  Idempotency records, which hold a hash of the arguments and the `data` a repeat replays,
  are not masked (#54)
- Free-threaded CPython 3.14t is supported and tested: CI runs the suite on it with the
  GIL off, and the package declares `Programming Language :: Python :: Free Threading ::
  2 - Beta`. A new stress test runs `App.run` and `App.call` from several threads at
  once and checks each run gets its own envelope, log lines, and secret redaction (#74)
- `--timeout` on every command that declares `timeout=None` or a timeout longer than the
  app's `default_timeout`, not only on network commands and streams, so a caller can
  bound one run of long work: `checks play --timeout 600` ends in `TIMEOUT` (exit 10)
  after 10 minutes, and `ctx.remaining`, `ctx.run`, and `ctx.lock` count down from it.
  It means what it does on network commands, the deadline of the whole run, in place of
  the declared one; `0` runs unbounded. It is in the manifest, `--help`, completion, the
  MCP `inputSchema`, and `exec`'s `_opts`. `--proxy` and `--no-proxy` stay on
  `has_network_io=True` commands. A command with a field of its own named `timeout`
  keeps it: the framework's flag yields there, as `-v` yields to a `short="v"` (#69)
- The tutorial has a chapter for migrating a pydantic-settings CLI,
  `docs/tutorial/B-migrate/pydantic-settings.md`: `CliSubCommand` to commands and groups,
  `cli_cmd()` to handlers, `Field` to `Flag`, `model_validator` to `__post_init__` and
  `requires=`, `BaseSettings` to `App(settings=)`, and argument models shared by several
  CLIs as `kw_only` base dataclasses (#102)
- The tutorial index says how a library that supports Python before 3.14 ships a treaty
  CLI as a console script: treaty in a `cli` extra with a `python_version >= "3.14"`
  marker and an entry module that exits 4 with a clear message on an older Python or
  without the extra, or a separate `<name>-cli` distribution when several libraries share
  the CLI's argument models (#103)
- `Arg(default=...)` declares an optional positional, as in
  `query: str | None = Arg(default=None, ...)`: `resolve AAPL` and `resolve --figi X` both
  run. The manifest's `PositionalEntry` says `required: false`, `--schema`, `exec`,
  `--raw-payload`, and MCP let the field be left out, and `--help` shows `[query]`. An
  optional positional may follow a required one; a required one after it, a variadic
  `tuple[...]` without a default included, raises `RegistrationError`, as does a default
  that breaks the field's own type or pattern. The migration guides map
  `typer.Argument(None)`, click's `required=False`, and argparse's `nargs="?"` to it (#57)
- `RequiresAny(("isin", "figi", "symbol"))` in `requires=` needs at least one of its
  flags, and `RequiresOne` exactly one: phase 1 exits 2 (`ARG_ERROR`) listing them, on
  argv, `exec`, `--raw-payload`, `App.call`, and MCP alike. A flag counts as given as for
  the other rules. The manifest's `ConditionalRule` has no shape for them, so `--schema`
  shows them as `requires_groups` and as `anyOf`/`oneOf` of the `raw_payload_schema`.
  `--help` now lists every rule of a command under Rules, and an MCP tool's description
  ends with them. Registration refuses a group of fewer than two flags, a repeated or
  unknown flag, and an always-required one (#99)
- `Flag(dry_run=True)` marks a boolean field as the command's dry-run switch, so a
  command that wraps another tool keeps its own `--check` or `--noop` instead of renaming
  it `--dry-run`. `would_*` effects, `meta.dry_run`, the destructive-command rule
  (REQ-C-004), the `--confirm-destructive` preview, `exec --dry-run`, the conformance
  profile, and generated skills all follow the marked field, and the `danger-level`
  audit fix names it. Two marked fields, a marked field that is not a `bool`, or a
  marked field beside one named `dry_run` raise `RegistrationError`; an unmarked
  `dry_run` field works as before (#63)
- A run that reports a `would_*` effect carries `meta.dry_run: true`: a mutating
  command's dry run, a destructive `--dry-run`, and the preview a destructive command
  refuses with `CONFIRMATION_REQUIRED`, as a `safe_default` dry run already did. An agent
  knows nothing changed without reading `data.effect`. Live runs are unchanged (#70)
- "Step 5: Lint with ruff's ALL rules" in the testing chapter: the settings that end
  `RUF009` on `Arg()`, `Flag()`, and `Out()` defaults and keep `Path` a runtime import
  under `TC003`, and `_ctx` for an unused context, which treaty passes by position (#70)
- The manifest marks each command treaty registers with `"builtin": true`, on the
  `manifest` output and on `--schema`, so an agent can tell the app's commands from the
  framework's without a hard-coded name list. An app command omits the key, which reads as
  `false`, including one that replaces a built-in's name. The manifest's `schema_version`
  is now `3.1`, the spec's ManifestResponse contract with the marker (#70)
- `ctx.run(argv, stream=True)` shows a long child's output while it runs: each line it
  writes, on stdout or stderr, goes to the run's stderr as a `ctx.log` INFO line as it
  arrives, redacted (each line of a multi-line secret too, and a secret the 64 KiB split
  of a long line cuts), so a terminal or `--verbose` shows it and stdout keeps only the
  envelope. Off a terminal, in `App.call`, and over MCP the lines are dropped, as
  `ctx.log`'s are. `Completed.stdout` and `stderr` keep the last 4096 characters, as
  `SUBPROCESS_FAILED`'s `context.stderr` does; timeouts, signals, the locale, `input=`,
  and `children.pids` are unchanged (#61)

### Fixed

- The `conditional-rules` audit advice for a `__post_init__` that raises when none of
  several fields is set (`not (self.a or self.b)`, `not any(...)`, or `self.a is None and
  self.b is None`) suggests `RequiresAny` with those fields; it suggested `Excludes` on
  the first two, which would refuse valid calls (#99)

- Parallel runs on Windows no longer fail now and then while one writes a file another
  reads: replacing the config file or the update-check and `ctx.cache` files while
  another process has it open, or reading it while it is being replaced, raised
  `PermissionError` and the run exited 1 or 2. On Windows treaty retries such a sharing
  violation for up to 2 seconds, then raises it, so a real permission problem still
  fails (#55)
- A field, parameter, or return annotation that names a class imported only under
  `TYPE_CHECKING`, or a typo, raises `RegistrationError` at `@app.command` instead of a
  bare `NameError`. Annotations are lazy on 3.14, so the module still imports; the error
  names every undefined name and the field or parameter that uses it, and `treaty audit`
  reports it as `APP_IMPORT_FAILED` (#73)
- An in-process `app.run([...])` under pytest's `capsys` no longer leaves a
  `PytestUnraisableExceptionWarning` (`ValueError: I/O operation on closed file`), so a
  suite run with `-W error` passes. The finalizer of the stand-in for `sys.stdout` that
  catches stray prints flushed stderr after pytest had closed its capture stream; the
  stand-in's flush now skips a stderr that is already closed (#65)
- Security: a secret no longer leaks between concurrent runs through a handler's
  `print()`. While runs overlap on threads, as the MCP server's tool calls do,
  `sys.stdout` belongs to the last run that started, so one run's printed secret reached
  another run's stderr and `THIRD_PARTY_STDOUT` warning unredacted: only the receiving
  run's secrets were replaced. Printed text is now redacted of every attached run's
  secrets as it is written, as library log records already were (#92)
- Text for a person no longer passes terminal escapes through (security). A data value in
  plain output, a TSV or CSV cell, and the data a custom renderer receives lose their
  escapes as in the JSON envelope, so a value can no longer set the window title, forge
  an OSC 8 link, write the clipboard (OSC 52), or move the cursor. Plain keys, stderr
  error lines (the message, context, error items, and hint), tracebacks, and pagination
  cursors show a control as its escape (`\x1b`, `\x07`, `\r`) instead. A custom
  renderer's own text keeps its colors (SGR) only where the run may color, and `ctx.log`
  lines on a color terminal keep colors but lose every other escape. The 8-bit C1 forms
  (`\x9b` CSI, `\x9d` OSC) are stripped from this text too, and a lone C1 control is shown
  as its escape; the JSON envelope, which is ASCII, keeps them as `\u` escapes (#72)
- Text a handler or a library prints no longer passes terminal escapes to stderr
  (security). It reaches stderr, and the `--debug` `stdout write` line, as a `ctx.log`
  line does: colors (SGR) only where the run may color, every other escape, 7-bit or C1,
  gone, and other controls shown as their escapes. Tab, newline, and carriage return stay
  as printed on stderr, so a progress line drawn with `\r` still rewrites itself. An
  escape printed across two writes, such as an OSC 52 whose payload comes in the next
  `print()`, is held until it ends and cleaned whole. A secret that an escape splits
  (`hun\x1b[0mter2`) is redacted once the escape is gone, on stderr and in the
  `THIRD_PARTY_STDOUT` warning, whose `bytes` still counts what was printed (#105)
- An error message keeps a program name and a trailing id verbatim: a first word with a
  `-`, `_`, `.`, `/`, or digit, or naming a program the command declares in `subprocess=`
  or `required_tools=`, keeps its case (`ansible-playbook exited 4`, `git refused the
  push.`), and a last word with a `-`, `_`, `.`, `/`, `:`, or digit takes no period
  (`Component does not exist: crm-backend`). A hyphenated first word such as `db-migrate`
  is no longer capitalized, and a message ending in a number or version (`upgrade to
  1.4.0`) no longer gets a period (#64)
- Output security no longer masks SSH public keys as credentials. A field named for a
  public key (`public_key`, `publicKey`, `ssh-pub-key`) is not a credential name, and a
  value in a public key format is left alone under any credential name, typed or nested
  in a `dict[str, object]`: an OpenSSH public key line, a PEM `PUBLIC KEY` or
  `CERTIFICATE`, an RFC 4716 SSH2 public key, or an age recipient. Each format is matched
  whole and its body decoded, so private keys, `AGE-SECRET-KEY-1...`, and a value that
  only starts like a public key stay masked, as does a `public_key` holding private
  material; `Out(high_entropy=True)` still masks any value. Log redaction is unchanged:
  REQ-F-034 still writes a name containing `key` as `[REDACTED]` (#66)
- Tools that require a UTF-8 locale, like Ansible, start under `ctx.run`: children no
  longer get `LC_ALL=C`, which made them refuse to run. On Linux, macOS, and the BSDs a
  child gets `LANG=C`, `LC_MESSAGES=C`, and `LC_NUMERIC=C`, so messages stay English
  and numbers dot-decimal, and `LC_CTYPE=C.UTF-8` where the C library has that locale,
  else `C`. `LC_ALL` and the user's other `LC_*` variables are removed from the child's
  environment, so the categories treaty does not name (time, collation, money) stay C
  through `LANG=C`. Windows children get `LC_ALL=C` and `LC_NUMERIC=C` as before, and
  `preserve_locale=True` is unchanged (#62)
- Heartbeats no longer come in a burst after a stall: when the waiting thread woke
  several intervals late, a JSON heartbeat line (`--heartbeat-ms`) or a
  `--heartbeat-interval` progress line was written once per missed interval, all with
  the same `elapsed_ms`. The next beat is now due one interval after the last one
  ticked, so missed beats are skipped and two beats are never closer than the interval
  (#56)
- `treaty audit` follows a handler into every top-level package of the distribution
  that ships it, not only the handler's own: a wheel with `apppkg` and `libpkg` fails
  `no-chdir` when an `apppkg` handler calls `libpkg.workdir.enter`, which runs
  `os.chdir`. `importlib.metadata` names the packages, and for an editable install, the
  packages in the directory its `.pth` file adds. Other distributions, the standard
  library, and treaty are still not followed, and with no installed distribution the
  handler's package alone is first-party. The `Scope:` line and the report's `scope`
  name the packages followed (#67)
- Security: a handler that outlives its timeout no longer leaks its secret into a later
  run. It keeps running on its worker thread after its run answered `TIMEOUT` (or was
  cancelled), and what it printed then reached the stderr and `THIRD_PARTY_STDOUT`
  warning of whichever run held `sys.stdout` unredacted, because its run's secrets were
  released when the run returned. They now stay registered until the handler's thread
  ends; a thread that never ends keeps them for the life of the process (#104)

## [1.0.0rc5] - 2026-09-30

The fifth 1.0 release candidate: fixes to rc4's `stable-order` warning and `Decimal`
arguments. The warning now also covers object arrays nested in a list, tuple, or dict,
so `treaty audit --strict` can fail where rc4 passed.

### Fixed

- The audit rule `stable-order` warns about an array of objects nested in a list, tuple, or
  dict, such as `dict[str, list[Line]]` or `list[list[Line]]`: treaty sorts it by each
  item's JSON text, and neither `ordered=True` on the command nor `Out(ordered=True)` on
  the field reaches it, yet `--strict` passed. Hold the array in a dataclass field that
  declares its order
- The `stable-order` fix names a `sort_key` only from the fields that can be one (a str,
  int, Enum, or date); it no longer suggests a `Decimal` or `float` field, which fails
  registration, and suggests `ordered=True` alone when no field can be a key
- A `Decimal` default reaches the handler as an argument would parse it: `Decimal("-0")`
  is `0` and `Decimal("1E+2")` is `100`, where the default went through unchanged and a
  handler could see a signed zero
- A `Decimal` sent as a JSON number, in `exec`, `--raw-payload`, MCP, or `app.call`, counts
  against `Flag(max_bytes=)` as its text, as a JSON string does; a number skipped the limit

## [1.0.0rc4] - 2026-09-30

The fourth 1.0 release candidate: built-in `Decimal` arguments, and a `stable-order` warning
for an output array of objects with no declared order. Not additive over rc3: see Breaking.

### Breaking

- The audit rule `stable-order` is a warning for an output array of objects with no
  declared order, a `list[T]` result or field of dataclasses, so `treaty audit --strict`
  fails on it: treaty sorts such an array by each item's JSON text, which silently puts
  `"10.00"` before `"5.00"`. Declare `sort_key=` or `ordered=True` on the command, or
  `Out(sort_key=...)` or `Out(ordered=True)` on the field. Arrays inside untyped output
  stay advice (#38)

### Added

- `Decimal` is a built-in argument type, as `int` and `float` are: `--amount 12.30` reaches
  the handler as `Decimal("12.30")`, parsed from fixed-point text without a float, and
  `Decimal | None` and `tuple[Decimal, ...]` work too. An exponent, `NaN`, `Infinity`, or
  `1,5` exits 2 naming the flag, and `-0` is zero. `exec` lines and `--raw-payload` take a
  JSON string or a JSON number read from its source text; a float from an MCP client or
  `app.call` is refused. Its argument schema says `format: decimal`; output schemas are
  unchanged. An app's own `app.scalar(Decimal, ...)` replaces the built-in,
  for output schemas too (#39)

## [1.0.0rc3] - 2026-09-30

The third 1.0 release candidate: retry backoff and the timeout audit rules, `-v` and `-vv`,
library logging routed by level, and a round of audit, secret-redaction, and parsing fixes.
Not additive over rc2: see Breaking.

### Breaking

- `ctx.http` retries a POST or PATCH only when it never reached a server (a refused
  connection or an unknown host): a timeout or a 502 to 504 may come after the server
  acted. GET, HEAD, OPTIONS, TRACE, PUT, and DELETE retry as before. A `Retry-After`
  lengthens the delay, and one over 60 s ends the run with `retry_after_ms` instead
- An integer or number on argv is written as JSON writes it: `1_000`, `+3`, `" 7"`, and
  digits of other scripts are argument errors. `pattern=` on a number is checked against
  the parsed number on argv and JSON alike, so `--price 1.50` and `{"price": 1.50}` agree
- A JSON object that repeats a key is refused (`--raw-payload` and `exec` lines) instead
  of taking the last value, as argv already refused a repeated flag
- The forgiving JSON reader offers no `corrected_input` for a near-number (`+1`, `0x1F`,
  `.5`, `NaN`, `Infinity`): quoting it would have sent a string
- Two commands that differ only in `.` and `-`, such as `cache.clear` and `cache-clear`,
  fail registration: they would share a skill file and skill name
- A flag default that breaks its own `pattern`, `pattern_type`, or `max_bytes` fails
  registration
- An empty `--config=`, `--context=`, or `--instance-id=` is an argument error instead of
  falling back to the variable or the default files
- `--schema-version` takes a major or a `MAJOR.MINOR` the command serves: `1.garbage` and
  an unserved minor such as `1.5` are argument errors
- `ctx.prompt(flag=)` must name a flag of the command
- `ctx.edit` reports an editor that exits non-zero, or is missing, as `EDITOR_FAILED`
  (exit 1) instead of a crash
- `treaty audit`, `schema-lock`, `conformance`, and `init` resolve the target module, the
  schema lock, `conformance/`, and the new project against `--cwd` instead of the
  process's directory; `schema-lock` reports the lock's absolute path
- A background pid file entry carries the child's start time; entries written by rc2 are
  still read
- A command whose arguments or output have no schema, such as a `tuple[SomeDataclass,
  ...]` field, raises `RegistrationError` naming the command at registration, instead of a
  bare `SchemaError` (#6). Code that caught `SchemaError` there catches
  `RegistrationError` now, or `TreatyError` for both

### Added

- A settings field declared `Flag(default=..., description=..., secret=True)` is a secret
  setting whatever its name: redacted by `--show-config` and left out of the config hash.
  An enum setting, like an enum flag, is not inferred secret
- The `describe` rule runs each example through `--validate-only`, and one that does not
  parse is an error: agents copy examples verbatim, and a `<name>` placeholder is one too.
  Only the spelling is judged: a secret's `--<name>-from-env` or `-from-file` reads a dummy
  value, `--cwd`, `--input-file`, and `--config` are dropped with their values, leading
  `VAR=` words set the call's environment, `uv run` and `sudo` are skipped, an unquoted `#`
  at a word's start begins a comment, and an example with a pipe, a redirect, `;`, or a
  lone `-` for stdin is not judged. `--validate-only` goes right after the command path, so
  no handler ever runs during the audit. The no-example fix suggests values its checks accept (a
  preset's sample, a secret from a variable, a tuple's item type); a custom `pattern=`
  leaves a `<name>` to replace
- `App.redirected_paths`: the old command paths `redirect` keeps answering
- `external=False` on a command that calls out says it returns only values it computed,
  which clears the `external-data` warning; unset, `external` is `None`, undeclared
- The `subprocess-declared` rule reports `subprocess.run` and its siblings, `os.exec*`,
  `os.spawn*`, and `os.posix_spawn*`, called in a handler however they were imported:
  outside `ctx.run` a child has no time limit or declared argv, and `doctor` cannot check
  the program. Its fix says `ctx.run` checks the exit status unless `check=False`
- `Arg(multiline=True)` lets a positional hold newlines, as `Flag(multiline=True)` does; the
  `multiline-flag` advice now suggests `Arg` for a positional instead of a `Flag` it cannot take
- Did-you-mean on an unknown command: `context.did_you_mean` lists up to three close
  registered commands, best first, and `suggestion` names them, on the command line (as
  invocations, matching a mistyped word, a command's own name without its group, or the
  dot path) and on `UNKNOWN_COMMAND` from `App.call`, `exec`, MCP, and
  `check-permissions --for` (as dot paths). Close is one typo per three characters typed,
  a swap of neighbors counting as one, or a prefix of a one-word name

- `completion` built-in: `<app> completion bash` or `zsh` prints a completion script
  generated from the manifest, covering commands, nested groups, flags, and the values of
  enum and path flags and positionals, `--flag=value` included. The script is static, so a
  tab press runs no Python. It yields to an app command named `completion`, and MCP serves
  no tool for it. Every app lists one more command, so every manifest etag changes once
- `CLEANUP_KEPT` warning: `cleanup` names the paths it left because they are, or hold, a
  declared credential or config path
- `APP_IMPORT_FAILED` (exit 4) from the `treaty` tooling commands when the app's module
  raises `RegistrationError` or `SyntaxError` on import, instead of a treaty crash. Its
  context carries `target`, `exception`, and the registration `message`, with no
  traceback, on `audit`, `schema-lock`, `changelog-add`, `agents-md`, `check-docs`, and
  `conformance` (#4)
- `App(version=)` accepts PEP 440 development and post releases and their combinations
  (`0.3.0.dev0`, `0.3.0.post1`, `1.0.0rc1.post2.dev3`), as `introduced_in=`,
  `Deprecated(...)`, and an update check do (#5). Their semver spelling puts `.devN` in
  the pre-release (`0.3.0-dev.0`) and `.postN` in the build metadata (`0.3.0+post.1`); a
  `.devN` of a post release stays in the build metadata (`1.0.0+post.1.dev.0`). Semver
  orders `dev` between `beta` and `rc`, so the spelling keeps the parts but not PEP 440's
  order. Epochs and local versions are still refused, and the error says why
- `Retry(backoff=, max_delay_ms=, jitter=, retry_if=)` (#11): each wait grows by
  `backoff` from `delay_ms`, which `--retry-delay` still sets, up to `max_delay_ms` (an
  hour when unset), moved by up to `jitter` of itself either way. A `CliExit` raised
  inside `ctx.retry` with `retry_after_ms` lengthens the wait as a `Retry-After` does in
  `ctx.http`, and one over 60 s ends the run with that error instead. `retry_if` retries
  on a returned value it holds for, and ends with the `exhausted` code when it still
  holds. The defaults keep the fixed delay, and the command's timeout still bounds every
  wait
- `treaty audit` reports its `scope` in JSON and on a `Scope:` line in plain output: what
  the source rules read, so a clean audit is not taken for a runtime check
- `-v` is short for `--verbose`, and `-vv` or `-v -v` for `--debug`, before or after the
  command path. A command that declares its own `short="v"` keeps `-v` for its flag on that
  command only; the `--verbose` and `--debug` descriptions in the manifest and help say so (#13)
- Audit rule `timeout-budget` (#12), a warning: a `retry=Retry(...)` whose waits, at
  their cap and full jitter, add up past the command's timeout, with a `timeout=` sized
  to them; and a `heartbeat=True` command that inherits the app's default timeout
- Audit rule `explicit-timeout` (#15), advice: a mutating or destructive command that
  inherits `App(default_timeout=)`. Any `timeout=`, the default's own value or `None`
  included, records the decision and silences it. `treaty init` scaffolds and treaty's
  own mutating commands now declare theirs

### Fixed

- `exit-code-suggestion` advised redeclaring a retryable framework code such as
  `RATE_LIMITED` with `app.exit_code("RATE_LIMITED", 11, ..., suggestion=...)`, which
  registration refuses, so the advice could never clear. Framework codes are skipped: they
  carry a generic suggestion, and a raise gives its own with `suggestion=` (#30)
- Records of Python's `logging` module reached stderr only under `--debug`, all labelled
  `debug`, so a library's INFO progress never showed under `--verbose`. A handler now sits
  on the root logger for the whole run, `App.call` and nested runs included, and routes
  each record by its level: DEBUG under `--debug`, INFO where `ctx.log` shows, WARNING and
  up as `warn` and `error` lines unless `--quiet`, redacted, with `fields.logger` and the
  record's level in the line. The root's level is lowered for the run when it would hide
  a shown level, then restored. Before, at default verbosity a library WARNING had no
  handler and fell through to `logging.lastResort`, which wrote it to `sys.stderr` raw,
  secrets included and even under `--quiet`. A record goes to the innermost run, which
  may be another thread's `App.call`, so the secrets of every attached run are redacted
  from it (#31)
- Audit rules that read handler source followed only helpers of the handler's own module,
  and `no-chdir`, `env-prefix`, and the other behaviour heuristics none at all, so I/O kept
  in a helper module passed clean (#14). Every source rule now follows the handler into its
  first-party code: its own module however deep, as before, and the other modules of its
  top-level package (or, for an app that is a top-level module, files under its directory)
  up to 3 calls deep, through modules and classes (`helpers.enter(...)`, `Store.load(...)`)
  and a module a helper imports itself. treaty, the standard library, and site-packages are
  never followed. A finding in a helper ends with where it is, such as
  `found via helpers.enter (helpers.py:6)`. Attributes are read statically, so a lazy
  module's `__getattr__` never runs during the audit
- A handler returning `list[dict[str, object]]`, such as a list of `model_dump()` dicts,
  had every array inside re-sorted by its JSON text with no audit finding. `typed-output`
  now flags a list or tuple of untyped dicts, and `stable-order` says an untyped output or
  field has its arrays re-sorted and suggests a typed output or `ordered=True`.
  `ordered=True` and `Out(ordered=True)` keep the order of arrays inside a
  `list[dict[str, object]]` too, as they already did inside a `dict[str, object]` (#27)
- A short flag that takes a value keeps a global option's name as that value, as the long
  form does: `-s -h` and `-s -v` pass `-h` and `-v` to `-s` instead of reading them as
  `--help` and `--verbose`
- A settings field can name a class registered with `app.scalar`: `App(settings=)` checks
  the dataclass's shape at once and its field types on first use (a run, `call`,
  `manifest`, or `treaty audit`, which reports a bad one as `APP_IMPORT_FAILED`), and a
  scalar registered later is picked up on the next use. A value from the environment or a
  file goes through the scalar's `pattern` or bounds and its `parse`, and a bad one exits
  `2` with `CONFIG_INVALID` naming the key and its source. A boolean setting declared
  secret now fails on first use instead of at `App(...)` (#29)
- An argument list built from the whole arguments object, as `["git", *flags(args)]`, got
  a worked-out declaration naming no user-controlled field and no audit warning. Passing
  `args` on whole, or reading a method or property of it such as `args.argv()`, now makes
  the declaration underivable, so `subprocess-declared` asks for one by hand, as does a
  class constant read from `args`. A copy, a name every assignment binds to `args`, another
  copy, or `replace`/`copy.replace` of one, counts `clean.ref` as `ref` plus the fields
  replaced into it; a name also bound any other way, such as `load_settings(args)` on one
  branch, is not a copy and is unknown. `self.cmd.append(args.x)`
  carries the field, and `insert`'s position does not
- A subprocess declaration treaty worked out missed a field whose value reached `ctx.run`
  through a local variable, as in `["git", *extra]` after `extra = list(args.extra)`, so
  the manifest named too few user-controlled arguments. Such a local now carries the
  field, whether assigned, filled with `.append`/`.extend`, bound with `:=`, or opened with
  `with ... as`; a local from anything else, such as a path or a constant, is not listed.
  `retry-declared` also finds a backoff whose sleep is gated at the top of a loop of
  attempts, or whose other handler re-raises
- The example check crashed the audit on a `#` inside a quoted `--flag="..."` value and
  misread the `'\''` apostrophe idiom; comments are found by one quote-aware scan. An
  example that names another command after `--` is not run, a retry's sleep must be in
  its `except`, and every `<name>` placeholder in an example is checked
- `treaty audit --strict` listed `next_steps` sorted instead of in severity order: data
  carried by `Exit(data=...)` lost its dataclass's `Out(ordered=True)`
- A relative `Path` setting, or each path of a `tuple[Path, ...]` one, was used against the
  process's directory; it is resolved against the run's, `--cwd` included, as a `Path` flag
  is. A settings field refuses `Flag` options settings do not enforce, such as `pattern=` or
  `max_bytes=`, and `secret=True` on a boolean
- A redirect's message starts "Command", or "Tool" over MCP, so a lowercase command name
  keeps its case
- `retry-declared` fired on a loop that sleeps to throttle between items; it now counts a
  loop that sleeps and is left from inside a `try` once the call succeeds (a `return` or a
  `break` directly in its body, or in a `with` there, or right after a `try` whose
  handlers `continue`), which a throttle, a poller, a consumer, or a search is not. A retry
  that returns only behind an `if` in the `try`, such as on a status code, is not found.
  `asyncio.sleep` and an aliased `time.sleep` count as sleeps
- Following helpers, the audit looped forever on a lazy proxy or mock a handler calls, and
  failed on an unhashable doctor check; it unwraps only what a decorator set, and parses an
  unhashable callable without the cache, which is bounded and cleared per audit. An import
  made inside a helper (`import requests as r`, `from requests import get as g`) is
  resolved to the name it stands for
- `sentence()` left a hyphenated or comma-ended first word lowercase, such as
  `instance-id:`; only a word with a dot, slash, underscore, or digit keeps its case. An MCP
  redirect's message names tools too
- The conformance profile probed a `safe_default` command as destructive with `--live`,
  which alone applies it: the kit applied the change against the sandbox and failed L2
  when nothing refused. Such a command is now probed as the read its default is; since
  `--live` alone confirms it (REQ-O-048), an app whose only destructive commands are
  `safe_default` scores `incomplete` in the kit rather than failing
- An error message that began with a path or file name was capitalized into another
  name, such as `Out.json exists.`; a first word that is not a plain word keeps its case
- A compat shape's schema lacked the `noop` a replayed idempotency key answers, so a
  replay under `schema_version` failed an MCP client's check; an MCP redirect now names
  the tool to call, not the command path; a streaming command with compat shapes lists
  one array schema per shape; a `tuple[A, B]` output no longer crashes registration
- Following helpers, the audit crashed on a lambda helper, and flagged `network-io` for a
  helper whose docstring said "requests" or that called `urllib.parse`; helpers and
  resources now count only calls into a network module, decorated helpers are followed
  through `__wrapped__`, and the parsed sources are cached across rules
- An MCP call to a redirected command's old tool name answered `UNKNOWN_TOOL`; it answers
  `REDIRECTED` with the new path, as the command line and `exec` do
- The `network-io`, `network-timeout`, `http-client`, and `subprocess-declared` rules read
  only the handler's source; they now follow the functions of the handler's module it
  calls, so a `fetch()` helper beside it is checked too. The `network-io` fix says to call
  out through `ctx.http`
- A command whose output had an `Out(external=True)` field tagged `data` with `_source` and
  `_trusted`, but its output schema did not list them, so an MCP client that validates
  structured content refused every result. The schema now lists the tags wherever the
  runtime puts them: on `data` as served, after a batch or job wrapper, on each branch of
  an `Optional` output, beside a `dict` output's keys, and in compat shims' schemas; an MCP
  tool's output schema admits each compat shape `schema_version` can select
- The `network-io` rule scanned only the handler; it now scans every resource the handler
  reaches too, where a migrated CLI's HTTP client usually sits
- An enum or `Literal` field named like a secret, such as a positional `key`, was inferred
  a secret and refused; an enum's values are public, so it no longer is, nor is an array
  of them, and the positional-secret error offers `secret=False`. Such a field that was
  registered before now takes its value on the command line, without `--<name>-from-env`,
  `--<name>-from-file`, or the `<APP>_<NAME>` default
- The `paginated-list` rule warns when a paged list command keeps its own `--page`,
  `--offset`, `--per-page`, or `--page-token`: `meta.pagination` then says `has_more: false`
  while the source has more
- The `config-write-scope` fix suggests `"global"` for a user file, with the `--global` every
  call then passes, beside `"local"`
- `delete-not-found` fired on every destructive command with `NOT_FOUND`, such as a
  restore from a missing snapshot; it now applies to commands whose output admits
  `deleted`, or, without an effect enum, whose name is a delete verb (`delete`, `remove`,
  `purge`, `prune`, `wipe`, `clear`, and the like)
- An app whose type has no JSON Schema, found as `treaty audit` imported it, crashed the
  treaty command; it is `APP_IMPORT_FAILED` like a registration error
- The `delete-not-found` fix said to return `noop` when the resource is gone, which a dry
  run refuses with `INVALID_EFFECT`; it now names `would_delete` with an empty `Affects` for
  the dry run. `already-exists` no longer says a create lacks `CONFLICT` when the manifest
  lists the generic one
- A field named like a framework flag the command gets, such as a migrated `--timeout` on a
  network command, was refused with "rename the fields". For `--timeout`, `--limit`,
  `--cursor`, `--proxy`, and `--no-proxy` the error now says to drop the field and use the
  framework's, since a renamed one would duplicate it
- The `schema-version` rule called any change inside an `anyOf`, `oneOf`, or `allOf` breaking, so
  a field added to the items of a `Batch` result, or to an optional object, demanded a new
  major version. Union branches are now compared one by one; a branch added or removed is
  still breaking
- `@app.command` and `@group.command` typed the function they return as `Callable[..., Any]`,
  so mypy passed a direct call to a handler that no longer matched its signature, such as
  one missing a parameter added later. The decorated function now keeps its own type
- `treaty conformance --run --format plain` printed "Kit not run; add --run to execute it"
  when `--run` was passed but the kit could not run or rejected the profile. The data gains
  `run_requested`, and the hint appears only when `--run` was not passed
- `--idempotency-key`'s help printed `($<APP>_SESSION)`, and a truncation hint on an
  `exec` line or MCP call `<APP>_MAX_OUTPUT_BYTES`, instead of the app's own variable, such
  as `$TODO_SESSION`
- The `exit-codes` audit fix suggested code 79 for every command, and a `description="..."`
  that registration refuses, so applying the fixes as written failed. Each finding now names
  the next free code from 79 up and a placeholder description that registers
- `treaty check-docs` passed an AGENTS.md that a command or variable added since was missing
  from, since every name left in the file still existed. Each generated section between
  the treaty markers is now compared with what `treaty agents-md` writes, and one that
  differs is a `section` mismatch. `init`'s `test_agents_md.py` runs the check, so a new
  project's tests fail as soon as a command is added without regenerating the file
- Plain-mode stderr printed `[REDACTED]` for three context fields treaty fills with names,
  not values: a conformance `CONFLICT`'s `changed_keys`, `TOKEN_REQUIRED`'s
  `token_env_vars`, and `CONFIG_INVALID`'s `key`. They print as they are; any other
  context field named like a credential is still masked
- `cleanup` followed an `out/` directory that was a symlink and deleted what it pointed
  at; it now lists output files only under a temp root and `out/` the user owns
- `cleanup` deleted a declared credential or config path when a cache, temp, or log glob
  also matched it, or a cleaned directory held it
- `ctx.http` sent `Authorization`, cookies, and custom headers to a redirect's other
  origin; a cross-origin hop now keeps only the `Accept` headers and `User-Agent`
- Without a state directory, `ctx.spawn`'s pid files lived in a temp directory shared by
  all users, where another user could plant them; they now live under the user's private
  temp root, and a planted directory is refused with `TEMP_DIR_UNSAFE`
- A signal did not cancel an `async def` handler: its teardown waited for it to finish.
  The handler is cancelled, so its `finally` blocks and async releases run at once
- An async command hung forever when `asyncio` could not load; it now fails with why
- A timed-out `Batch` result was never recorded for its idempotency key, so a retry ran
  the mutation again
- A heartbeat line that could not be written answered `HANDLER_CRASHED` and freed the
  idempotency key while the handler still ran
- A handler without a timeout could change the caller's contextvars
- `--flag -` crashed on stdin that was not UTF-8 under `surrogateescape`, and a signal
  during its read gave a traceback instead of `CANCELLED`
- A deeply nested `--cursor` crashed with a traceback; it is `INVALID_CURSOR`
- Registration and `treaty audit` crashed on a nested handler holding a multi-line string
  at column 0
- Changelog versions compared pre-releases as text, so `rc.10` sorted before `rc.9`, and
  a second `changelog-add` at `rc.10` left an app that could not import. The update check
  now tells an rc user about a later rc
- `reap` could signal a process that reused an expired child's pid, sent only SIGTERM,
  and lost entries when two spawns rewrote the pid file at once; it now checks the start
  time, follows SIGTERM with SIGKILL, and rewrites the file under a lock
- `prune` deleted the temp directory of a run still going after a day; a run now holds a
  lock on it
- `--say -h` ran `--help` instead of passing `-h` to `--say`, and likewise for the names
  of other global options, when the command's flag takes a value
- A renderer returning anything but text crashed the run; its traceback is now redacted
- The conformance kit outlived a timeout, as only `uv` was killed; it now runs in a
  process group of its own, with the run's environment
- `agents-md` appended a new generated block on every run when prose quoted the end marker
  before the begin marker; a begin marker without an end marker is now an error
- `check-docs` did not check command lines behind `$`, `uv run`, `uvx`, or `pipx run`
- `<APP>_CONTEXT` failed every run that read no config file (`--no-config`, or an app
  without settings), and a context in the user file lost to the project file's top level
- The forgiving JSON reader read `{a: 1/* c */}` as a string and an invented key
- A failing async handler's tasks kept running while its resources were released
- `write_atomic` now also syncs the directory, so a rename survives a crash
- Captured third-party stdout could end in a stray `U+FFFD` where the 4 KiB cut split a
  character, and a prompt's answer kept a Windows terminal's `\r`
- Nested `armed()` signal windows were caught by an `assert`, which `python -O` drops
- The `generate-skills` example claimed Claude Code finds the files it writes
- `check-docs` said "1 items in the docs disagree" for a single mismatch; it now says
  "1 item in the docs disagrees"
- A `Path` argument with `%2e%2e` was refused with a suggestion that decoded it to a `..`
  path, which was refused in turn; the suggestion is now the absolute path, and a value
  that decodes to a null byte, a line break, or another encoding gets no path suggested
- The absolute path suggested for a `..` in a `Path` argument was resolved against the
  process's directory, even under `--cwd`, where the argument itself resolves; it is now
  under `--cwd`, and symlinks in it are no longer resolved
- The path in a `Path` argument's suggestion was not quoted, so one with a space or a quote
  broke when pasted into a shell; it is quoted with `shlex.quote`, like treaty's other
  suggested commands
- A `Batch` command's content from outside the tool reached the agent untagged: an
  `Out(external=True)` field of the item type was ignored, and a batch with a failed item
  (exit `3`) was never tagged, even with `external=True` on the command. Each successful
  result is now protected by the item type, so its `external` and `high_entropy`
  declarations apply, and a batch's `data` is tagged whether or not every item succeeded
- `treaty audit`'s next steps were the first findings in rule order, so an error from a late
  rule, such as `additive` under `--baseline`, could sit behind advice and past `--limit`;
  they now come errors first, then warnings, then advice, with `--all` in the same order
- `--debug` traced a `ctx.http` request only once it was answered, so a refused
  connection, an unknown host, or a timeout left no line; each now has its `http request`
  line with the failure's code in `error` instead of a `status`
- The `THIRD_PARTY_STDOUT` warning said the stray text went to stderr, but off a terminal
  and without `--verbose` it is written nowhere else; the message now says the text is
  in the warning instead
- A list cut to a page in a text format (`plain`, `tsv`, a custom renderer) gave no sign that
  more items exist, since text carries no `meta`; stderr now says how many were shown and
  gives the next `--cursor`, as `--format id` already did
- An error message ending in a path, URL, quoted value, flag, or identifier gets no
  closing period (REQ-C-013), which would read as part of it: `/nonexistent` no longer
  becomes `/nonexistent.`, and `Unknown flag '--token'` ends at its quote (#16)
- `examples=` on `app.command` and a group's `command` refuses anything but
  (description, command) pairs of strings with a `RegistrationError` that names the
  command and the shape to write, instead of a raw `ValueError` for a plain string or a
  silent unpacking of a two-character one

## [1.0.0rc2] - 2026-09-28

The second 1.0 release candidate: native `async def` handlers, cooperative deadlines, and
PEP 440 pre-release versions for apps. Additive over rc1; no breaking changes.

### Added

- `async def` handlers, and `async def` resource `acquire` and `release` (REQ-F-049): one
  event loop per run on a thread of its own, cancellation at the timeout, and an
  `UNAWAITED_TASKS` warning for tasks the handler left running. An async resource needs an
  async handler; streaming handlers and other hooks stay plain `def`
- `App(version=)` accepts a PEP 440 release with an `a`, `b`, or `rc` pre-release, such
  as `importlib.metadata.version` returns (`1.0.0rc1`); `--version`, `meta.tool_version`,
  and `app.version` give its semver spelling (`1.0.0-rc.1`). `introduced_in=`,
  `Deprecated(since=, removed_in=)`, and a version string from an update check are read
  the same way. Development and post releases are still refused
- `ctx.remaining`, the seconds left before the command times out (`None` without a
  limit), and `ctx.expired`: a handler passes the deadline to a client's `timeout=`,
  or stops a long loop at the limit instead of running on after `TIMEOUT`

## [1.0.0rc1] - 2026-09-28

The first 1.0 release candidate: every Level 3 requirement that changes the public API,
the additive Level 3 work, and the API review. See `plans/1.0/` and `docs/api.md`. The
package version is PEP 440 (`1.0.0rc1`); `treaty --version` and `meta.tool_version` give its
semver spelling (`1.0.0-rc.1`), which `App(version=)` requires.

### Breaking

- `treaty conformance` no longer overwrites a profile that differs from the generated one:
  it exits `6` (`CONFLICT`) naming the changed keys and probes, and `--force` replaces the
  file; an equal profile is left untouched (`effect: noop`). `treaty init` now scaffolds
  exactly the profile `conformance` generates
- `TREATY_FORMAT`, `TREATY_MAX_OUTPUT_BYTES`, `TREATY_MAX_STDIN_BYTES`, and
  `TREATY_STATE_DIR` are now `<APP>_FORMAT` and friends; every variable treaty reads for
  an app carries its prefix, and `<APP>_STATE_DIR` names the directory itself
- `Envelope` takes a `treaty.Meta` instead of `duration_ms` and `request_id`;
  `Meta.request_id` and `timestamp` are optional
- `App(version=)` must be semver
- `Ctx.config` is private; config writes go through `ctx.write_config` and are locked
- `RATE_LIMITED` without `retry_after_ms` and an invalid `fix_command` end the run as
  `INVALID_EXIT`; a missing login is `UNAUTHENTICATED` and a missing scope at run time
  `PERMISSION_DENIED` (was `AUTH_REQUIRED` and `INSUFFICIENT_SCOPES`); a read-only
  command's `TIMEOUT` is retryable
- `async def` handlers, hooks, and resource acquires fail registration
- Unparseable JSON input is `INVALID_JSON` (was `ARG_ERROR`, or `DISPATCH_PARSE_ERROR`
  for an `exec` line); `validate_only` is a framework key; every manifest entry has
  `option_placement`
- Arrays in `data` are sorted unless the field is `Out(ordered=True)`; `Path` output is
  absolute; `list[T] | None` output fields are refused; every output key is required
- `cleanup=` runs after every handler run, not only on signals
- High-entropy strings and credential-named fields in `data` are masked unless
  `--unmask`; `unmask` and `no-injection-protection` are global names; output fields
  named `_source` or `_trusted` are refused; more names are inferred secret (`cookie`, a
  `pass` segment); truncation warnings name `data.x` instead of `$.x`
- `os.system`, `os.popen`, and `shell=True` in a handler fail registration (the `no-shell`
  audit rule is gone); `gui_operations` requires `headless_behavior=`
- Every app has the `doctor`, `cleanup`, `status`, `generate-skills`, `mcp-validate`, and
  `audit-log` built-ins, each yielding to an app command of the same name, so every etag
  changes once; `cleanup` output has `cleaned` instead of `removed`
- Children of `ctx.run` get `LC_ALL=C` unless `preserve_locale=True`, and `CI=1` off a
  terminal
- Off a terminal or under `CI`, `ctx.log` and stray `print()` text no longer reach stderr
- Every app writes a rotated, redacted audit log under `XDG_DATA_HOME` unless
  `<APP>_AUDIT_LOG=off` or `App(audit_log=None)`
- A field named after a reserved framework flag (`--config`, `--quiet`, `--fields`,
  `--token-limit`, and the rest of the table in `_framework.py`) fails registration
- The manifest's `framework_version` is treaty's version; the app's is `meta.tool_version`
- API review: `treaty.ExecArgs` is no longer exported; `Ctx`'s run plumbing (`log_sink`,
  `warn_sink`, `processes`, `prompter`, `retrier`, `locks`, `teardown`, `steps`,
  `session`) and `App`'s run-path helpers (`renderer`, `moved`, `check_fixes`,
  `fix_problem`, `named_commands`, `effective_timeout`, `silence_notifiers`) are private
- `--version --format plain` prints the bare version

### Added

- Response meta: `command`, `timestamp`, `schema_version`, `tool_version`, `cwd`,
  `trace_id`, `project_root`, and `retries`; `schema_version=` and `compat=` with
  `--schema-version`, `--output-schema`, `--print-schema`; `project_root=`; `treaty.Retry`
  with `ctx.retry`; `treaty schema-lock`
- Settings: `App(settings=)` from `<APP>_<FIELD>`, `--config`, project and user TOML
  files, and defaults; `--context`, `--no-config`, `--show-config`, `--instance-id`;
  `App(init=)` with the `init` built-in and `INIT_REQUIRED`
- Error contract: `retry_strategy`, `treaty.already_exists` with `conflict_id`,
  `fix_commands=` and `App(companions=)`, `treaty.Expired` with `refreshes_auth=`,
  `treaty.NetworkContext`, `ctx.lock` with `LOCK_HELD`, `<APP>_SESSION` idempotency keys,
  and `App.redirect` (exit 13 `REDIRECTED`, manifest `aliases`)
- Argument grammar: `pattern_type=`, `requires=` with `RequiredWhen`, `Excludes`, and
  `DefaultWhenAbsent`, `option_placement="strict"`, `from_stdin=`, `--validate-only`, JSON5
  in `--raw-payload` and `exec` with `corrected_input`, `introduced_in=`,
  `deprecated=treaty.Deprecated(...)`, and `treaty audit --baseline`
- Output data: `treaty.Out` (`sort_key`, `ordered`, `volatile`, `high_entropy`,
  `external`), `treaty.Binary`, `Flag(max_bytes=)` with `FIELD_TOO_LARGE`,
  `ctx.truncated`, `--stable-output`
- Multi-step commands: `steps=`, `ctx.step`, `--resume-from`, `--rollback-on-failure`,
  `treaty.Batch`; resources' `release` on every exit
- Output security: masking, `external=True` with `_source` and `_trusted` tags,
  `--no-injection-protection`, `scrub()` for logs and stderr
- Declarations: `subprocess=treaty.Subprocess(...)`, `platform=`, `required_tools=`,
  `filesystem_side_effects=`, `background=` with `ctx.spawn`,
  `App(dependencies=[treaty.Dependency(...)])`, and the `doctor` and `cleanup` built-ins
- Session hygiene: `app.suppress_update_notifier`, `App(update_check=)` with
  `--no-update-check`, `--cwd` and `CWD_CHANGED`, `ctx.tmp_dir`, `ctx.temp_file()`,
  `ctx.output_file()`, `treaty.intercept_stdout()`, `cache=treaty.CachePolicy(...)` with
  `ctx.cache`, `--no-cache`, `--cache-ttl`
- Network and filesystem: `ctx.http` (proxies, CA bundles, `error.network_context`,
  retries), `--proxy`, `--no-proxy`, `recursive_traversal=True` with `ctx.walk`,
  `--no-follow-symlinks`, `--max-depth`
- Logging: `--quiet`, `--verbose`, `--debug`, `ctx.progress`, `ctx.debug`,
  `ctx.log_error`, `--warnings-as-errors`, `treaty.AuditLog`, the `audit-log` built-in
- Output selection: `--fields`, `--stream`, `--format id` with `id_field=`,
  `--heartbeat-interval`, `--token-limit`, `--token-offset`, `--token-count`,
  `--tokenizer`, `app.tokenizer()`, the `treaty[tiktoken]` extra
- Built-ins: `manifest --etag` with `meta.not_modified`, `App(checks=)` with `treaty.Check`
  and `treaty.endpoint`, `status`, `cleanup --scope` and `--min-age`,
  `App(schema_changelog=)` with `changelog` and `treaty changelog-add`, `generate-skills`,
  `mcp-validate`, `treaty-mcp --list-tools`
- Agent docs: `treaty agents-md`, `treaty check-docs`, AGENTS.md from `treaty init`
- 37 new audit rules (56 in all, listed by `treaty rules`), each with a generated fix
- `docs/api.md` (the frozen surface), `docs/guide.md`, this changelog, a stability policy,
  and per-platform notes in the README

### Fixed

- On Windows: `cleanup` and `status` report matched side-effect paths in native form
  instead of mixing `/` and `\`, and a `THIRD_PARTY_STDOUT` warning's `text` ends lines
  with `\n` (its `bytes` still counts what was written)
- On Windows, the audit log no longer loses an entry or fails `audit-log` when a run
  rotates it while another reads or appends: a file in use defers the rotation to the
  next append, and an open that meets a rename retries for up to a second
- `treaty conformance` refreshes a profile's `command` without a conflict: it is derived
  from the machine (the console script on Windows), not written by hand

## [0.1.0] - 2026-09-27

CLI Agent Spec Level 1 and Level 2.

### Breaking

- `@app.command` requires `danger_level=` and `exit_codes=` (the Level 2 plan's D3; no
  deprecation window)
- Argument errors exit 2 only in the validation phase, before user code runs; a
  handler-raised `ParseError` exits 1 with `VALIDATION_AFTER_START`
- Newlines, carriage returns, and null bytes in `str` arguments are refused unless
  `Flag(multiline=True)`

### Added

- Output hygiene: the stdout guard, `ctx.log`, JSON cleaning, color and pager
  environment, ISO dates, sentence-case errors
- `would_affect` and `safe_default=`; paginated lists with `--limit` and `--cursor`, and
  `--live` for safe-default commands
- `ctx.run` and `ctx.pipeline` with a hardened child environment and headless runs
- Prompts with `--yes` and `--non-interactive`, editors, stray `input()` detection
- Idle stream timeouts, exit 0 on a closed reader, heartbeats, stdin payloads, `jsonl`
  and `tsv`, `--output PATH` for `output_file=True` commands
- Credentials and scopes: the credentials gate, `check-permissions`, login commands
- Async jobs with job descriptors and `job status`; config writes with a scope
- The tutorial in `docs/tutorial/`

### Fixed

- Byte-exact atomic writes and steadier timing on Windows and macOS

## [0.0.6] - 2026-09-27

### Added

- CSV in the hello example; the error for an unoffered `--format` names the offered ones

## [0.0.5] - 2026-09-27

### Added

- Per-format renderers keyed by the `Format` enum

## [0.0.4] - 2026-09-26

### Breaking

- `--format human` is `--format plain`

### Added

- The plain fallback renderer
- Exit codes hoisted in the root `--schema`; piped `--help` points to `--schema`

## [0.0.3] - 2026-09-26

### Fixed

- Conformance runs on Windows through the app's console script

## [0.0.2] - 2026-09-26

### Added

- CI on Linux, macOS, and Windows for pushes and pull requests; publishing reuses it

### Fixed

- `EINVAL` on a closed pipe is a broken pipe on Windows
- The timed-out idempotency test is event-driven

## [0.0.1] - 2026-09-26

First release.

### Added

- Typed commands from dataclasses, the JSON envelope, the manifest, and `--schema`
- `exec` with a 64 KiB stdin cap, `--input-file`, and an `EMPTY_STREAM` envelope
- `effect` on mutating and destructive commands and `--idempotency-key`
- Secret flags from an env var or file only, redacted in errors
- `Path` fields hardened against traversal and encoded bytes
- Output size cap with truncation metadata
- Every validation error in one run
- Custom scalars, typed resources on the handler signature, streaming handlers, and the
  MCP adapter (`treaty[mcp]`)
- `treaty audit`, `treaty init`, and `treaty conformance`
- The benchmark against argparse and click on the spec harness

[Unreleased]: https://github.com/romamo/treaty/compare/v1.0.0rc27...HEAD
[1.0.0rc27]: https://github.com/romamo/treaty/compare/v1.0.0rc26...v1.0.0rc27
[1.0.0rc26]: https://github.com/romamo/treaty/compare/v1.0.0rc25...v1.0.0rc26
[1.0.0rc25]: https://github.com/romamo/treaty/compare/v1.0.0rc24...v1.0.0rc25
[1.0.0rc24]: https://github.com/romamo/treaty/compare/v1.0.0rc23...v1.0.0rc24
[1.0.0rc23]: https://github.com/romamo/treaty/compare/v1.0.0rc22...v1.0.0rc23
[1.0.0rc22]: https://github.com/romamo/treaty/compare/v1.0.0rc21...v1.0.0rc22
[1.0.0rc21]: https://github.com/romamo/treaty/compare/v1.0.0rc20...v1.0.0rc21
[1.0.0rc20]: https://github.com/romamo/treaty/compare/v1.0.0rc19...v1.0.0rc20
[1.0.0rc19]: https://github.com/romamo/treaty/compare/v1.0.0rc18...v1.0.0rc19
[1.0.0rc18]: https://github.com/romamo/treaty/compare/v1.0.0rc17...v1.0.0rc18
[1.0.0rc17]: https://github.com/romamo/treaty/compare/v1.0.0rc16...v1.0.0rc17
[1.0.0rc16]: https://github.com/romamo/treaty/compare/v1.0.0rc15...v1.0.0rc16
[1.0.0rc15]: https://github.com/romamo/treaty/compare/v1.0.0rc14...v1.0.0rc15
[1.0.0rc14]: https://github.com/romamo/treaty/compare/v1.0.0rc13...v1.0.0rc14
[1.0.0rc13]: https://github.com/romamo/treaty/compare/v1.0.0rc12...v1.0.0rc13
[1.0.0rc12]: https://github.com/romamo/treaty/compare/v1.0.0rc11...v1.0.0rc12
[1.0.0rc11]: https://github.com/romamo/treaty/compare/v1.0.0rc10...v1.0.0rc11
[1.0.0rc10]: https://github.com/romamo/treaty/compare/v1.0.0rc9...v1.0.0rc10
[1.0.0rc9]: https://github.com/romamo/treaty/compare/v1.0.0rc8...v1.0.0rc9
[1.0.0rc8]: https://github.com/romamo/treaty/compare/v1.0.0rc7...v1.0.0rc8
[1.0.0rc7]: https://github.com/romamo/treaty/compare/v1.0.0rc6...v1.0.0rc7
[1.0.0rc6]: https://github.com/romamo/treaty/compare/v1.0.0rc5...v1.0.0rc6
[1.0.0rc5]: https://github.com/romamo/treaty/compare/v1.0.0rc4...v1.0.0rc5
[1.0.0rc4]: https://github.com/romamo/treaty/compare/v1.0.0rc3...v1.0.0rc4
[1.0.0rc3]: https://github.com/romamo/treaty/compare/v1.0.0rc2...v1.0.0rc3
[1.0.0rc2]: https://github.com/romamo/treaty/compare/v1.0.0rc1...v1.0.0rc2
[1.0.0rc1]: https://github.com/romamo/treaty/compare/v0.1.0...v1.0.0rc1
[0.1.0]: https://github.com/romamo/treaty/compare/v0.0.6...v0.1.0
[0.0.6]: https://github.com/romamo/treaty/compare/v0.0.5...v0.0.6
[0.0.5]: https://github.com/romamo/treaty/compare/v0.0.4...v0.0.5
[0.0.4]: https://github.com/romamo/treaty/compare/v0.0.3...v0.0.4
[0.0.3]: https://github.com/romamo/treaty/compare/v0.0.2...v0.0.3
[0.0.2]: https://github.com/romamo/treaty/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/romamo/treaty/releases/tag/v0.0.1
