# treaty — Environment Profile

**Generated:** 2026-09-24

## OS
- Platform: darwin
- Version: 25.2.0

## Runtime
- Language: Python
- Version: 3.14.7
- Toolchain: uv 0.12.5

## Binary
- Entry point: `uv run treaty`
- Version: 0.0.1 (via `treaty version` or root-level `treaty --version`)
- Resolved path: /Users/roman/PycharmProjects/CLI-argonomics/treaty/.venv/bin/treaty

## Non-Interactive Flags
- `--confirm-destructive`: framework flag required to run commands with `danger_level="destructive"` (no prompt)
- `--dry-run` (`init`, `exec`): plan without writing
- `--timeout`: framework-level per-command timeout flag
- `--raw-payload` (`conformance`): JSON object of field values instead of individual flags
- No prompting flags (`--yes`, `--non-interactive`) exist; source contains no `input()` calls

## Output Format Flags
- `--format human|json`: global output mode
- `--help` / `-h`: prints the JSON manifest in non-TTY
- `--schema`: global flag, prints schema
- `treaty manifest`: full command manifest for agents

## Config
- `TREATY_FORMAT`: forces output mode (human|json)
- `CI`: any non-empty value forces JSON mode
- `TREATY_SPEC_DIR`: spec checkout for `conformance --run`
- Output mode resolution: `--format` > `TREATY_FORMAT` > (non-TTY stdout or `CI` → json) > human

## Timeout Method
- `perl -e 'alarm(N); exec(...)'` (no GNU `timeout` on macOS) or `subprocess.run(timeout=N)`

## Source
- README.md, pyproject.toml (`[project.scripts] treaty = "treaty._cli:main"`), `treaty --help`, src/treaty/_mode.py, src/treaty/_parse.py
- No AGENTS.md or CODING_AGENTS.md present
