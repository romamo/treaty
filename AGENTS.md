# AGENTS.md

## Install

treaty is not on PyPI yet; install from a checkout. Both commands are non-interactive,
need no input, and exit 0 when repeated:

```bash
uv tool install --reinstall /path/to/treaty   # the treaty CLI on PATH
uv add --editable /path/to/treaty             # the library, inside a uv project
```

Verify with `treaty --version`: it prints a JSON envelope with `data.version` and exits 0.
Add the `mcp` extra (`/path/to/treaty[mcp]`) to get `treaty-mcp module:app`, which serves
any treaty app's commands as MCP tools over stdio.

## Running the CLI

- Output is JSON automatically when stdout is not a terminal; `--format json` forces it
- Nothing prompts: destructive commands refuse without `--confirm-destructive`, and `exec`
  refuses a terminal on stdin
- `treaty manifest` lists every command, flag, and exit code in one call
- `2` is always an argument error; each command's other exit codes are in the manifest

## Developing

```bash
uv sync
uv run pytest
uv run mypy src
uv run ruff check src tests
```

Schema and conformance-kit tests need the sibling `cli-agent-ergonomics` checkout, or
`TREATY_SPEC_DIR` pointing at one.
