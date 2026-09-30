# Changelog

All notable changes to treaty. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and treaty follows
[Semantic Versioning](https://semver.org/) from 1.0 (see "Stability" in the README).
Before 1.0 any release could break an app; the "Breaking" sections say where.

Apps built on treaty keep their own, structured schema changelog with
`App(schema_changelog=)` and `treaty changelog-add`; this file is treaty's.

## [Unreleased]

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

[Unreleased]: https://github.com/romamo/treaty/compare/v1.0.0rc5...HEAD
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
