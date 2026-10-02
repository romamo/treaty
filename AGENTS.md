<!-- cli-version: 1.0.0-rc.18 -->
# AGENTS.md

## Installation

Both install commands are non-interactive, read no stdin, open no browser, and exit 0
when repeated:

```bash
uv tool install treaty   # the treaty CLI on PATH
treaty --version         # verify: exits 0; the version is data.version
uv add treaty            # the library, inside a uv project
```

For unreleased changes, install from a checkout instead: `uv tool install --reinstall
/path/to/treaty` and `uv add --editable /path/to/treaty`.

`treaty --version` prints a JSON envelope off a terminal, and the bare version with
`--format plain`. Add the `mcp` extra (`treaty[mcp]`) to get `treaty-mcp module:app`,
which serves any treaty app's commands as MCP tools over stdio.

## Running the CLI

- Output is JSON automatically when stdout is not a terminal; `--format json` forces it
- Nothing prompts: destructive commands refuse without `--confirm-destructive`, and `exec`
  refuses a terminal on stdin
- `treaty manifest` lists every command, flag, and exit code in one call
- `2` is always an argument error; each command's other exit codes are in the manifest

<!-- treaty:begin -->
## Canonical Invocation

`treaty <command> [arguments] [flags]`, where the command is one of these;
`treaty manifest` returns every command, flag, and exit code as JSON:

- `treaty agents-md`: Write AGENTS.md from the registry: the cli-version comment and the generated sections between the treaty markers; text outside the markers is kept
- `treaty audit`: Check an App's registrations against the spec and suggest the next step
- `treaty audit-log`: Query the opt-in audit log: one entry per invocation, oldest first, with its arguments (secrets redacted), exit code, duration, warning codes, and request, trace, and session ids; exit 4 with AUDIT_LOG_DISABLED while the log is off
- `treaty changelog-add`: Record the manifest changes since the last snapshot in the app's schema changelog, then update the snapshot
- `treaty check-docs`: Check agent docs against the app: the declared version, AGENTS.md's sections, and every command, flag, and variable they name against --help
- `treaty cleanup`: Remove the temp, cache, and log paths the tool's commands declare in filesystem_side_effects, its caches, and the output files commands handed out
- `treaty completion`: Print a shell completion script generated from the manifest: commands, flags, and the values of enum and path arguments; --format plain prints the script alone
- `treaty conformance`: Write a conformance profile from the registry and optionally run the spec kit
- `treaty doctor`: Check the tool's dependencies, the programs its commands run, its state and config directories, and the app's own checks; exit 4 with DOCTOR_CHECKS_FAILED lists a fix for each failure
- `treaty exec`: Dispatch JSONL DispatchRequest lines from stdin in-process
- `treaty generate-skills`: Write agent skill files from the command schemas: CONTEXT.md and one SKILL-<command>.md per command, each with YAML frontmatter
- `treaty init`: Scaffold a new CLI project that passes treaty audit
- `treaty manifest`: Print the command manifest for agents
- `treaty mcp-validate`: Compare a saved MCP tool list with the current command schemas; drift exits 1 with SCHEMA_DRIFT_DETECTED and the diff in data
- `treaty rules`: List the audit rules in the order they are checked
- `treaty scaffold-from`: Write a treaty module from a typer, click, or argparse CLI: an args dataclass and a handler stub per command, with danger_level and exit_codes left for the author
- `treaty schema-lock`: Record each command's schema version and output schema for the audit to diff
- `treaty status`: Show the tool's local state: side-effect paths with sizes, state files, and whether a credential is active; --show-config shows the settings
- `treaty version`: Print the tool name and version

## Non-Interactive Flags

- `--format json`: the JSON envelope on stdout, the default off a terminal
- `--confirm-destructive` (`treaty cleanup`): Required to apply; without it the command previews and exits 2
- Off a terminal nothing prompts: an answer a command needs fails with an exit code

## Environment Variables

- `TREATY_AUDIT_LOG` (string, optional): Audit log, off by default: 1 on, 0 off, or an absolute path to log there
- `TREATY_CONFIG` (string, optional): Config file to read instead of the project and user files
- `TREATY_CONTEXT` (string, optional): Named context of the config files to apply
- `TREATY_FORMAT` (string, optional): Default --format when the flag is not passed
- `TREATY_INSTANCE_ID` (string, optional): Instance namespace for the user config file and state
- `TREATY_MAX_OUTPUT_BYTES` (integer, optional): Default --max-output, in bytes
- `TREATY_MAX_STDIN_BYTES` (integer, optional): Most bytes read from a piped stdin
- `TREATY_NO_UPDATE` (string, optional): Any value turns the update check off, as --no-update-check does
- `TREATY_SESSION` (string, optional): Agent session id: repeats of a mutating call in it are deduplicated
- `TREATY_STATE_DIR` (string, optional): Directory for idempotency records

Shared conventions, read without the prefix:

- `CI`, `GITHUB_ACTIONS`, `JENKINS_URL` (string, optional): CI detection: JSON output, no prompts, and no update check
- `NO_COLOR`, `TERM`, `COLUMNS` (string, optional): Color, terminal capabilities, and table width of plain output
- `TOOL_TRACE_ID` (string, optional): Trace id of the run, inherited by child processes
- `HOME`, `USER`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `XDG_STATE_HOME` (string, optional): Where the user config file, state, and caches live
- `PATH`, `SHELL`, `PWD` (string, optional): Passed to child processes; PATH finds required tools
- `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`, `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE` (string, optional): Proxy and CA bundle for network calls, lowercase proxy names too

## Input Conventions

- Arguments are positionals and `--flag value` pairs; `treaty <command> --schema` prints a command's input and output schema, and `--validate-only` checks the arguments without running anything
- `--raw-payload` takes every field as one JSON object on `treaty conformance`, `treaty scaffold-from`
- `treaty exec` reads JSONL DispatchRequest lines from stdin, one command each, and answers one envelope line per request

## CI Validation

`uv run treaty check-docs treaty._cli:cli AGENTS.md` compares this file with the binary:
the `cli-version` comment, these sections, and every command, flag, and variable named
here. Drift exits 81 with one line per mismatch. `uv run treaty agents-md treaty._cli:cli`
rewrites the text between the treaty markers and keeps the rest.
<!-- treaty:end -->

## Developing

```bash
uv sync
uv run pytest
uv run mypy src
uv run ruff check src tests
```

Schema and conformance-kit tests need the sibling `cli-agent-ergonomics` checkout, or
`TREATY_SPEC_DIR` pointing at one.
