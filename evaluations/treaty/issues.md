# treaty — Issues

### §12/§3 candidate: `treaty exec` with empty stdin emits nothing (fixed 2026-09-24)
`treaty exec </dev/null` exits 0 with zero bytes on stdout and stderr. No envelope, so an agent can't tell "no lines dispatched" apart from "output lost". Fixed: an empty or blank-only stream now exits 2 with one `EMPTY_STREAM` validation envelope.
Discovered during §10 evaluation on 2026-09-24.

### §10 observation: `treaty exec` waits on an interactive TTY stdin (fixed 2026-09-24)
`exec` reads JSONL from stdin by design. If an agent runs it with an inherited TTY stdin and no pipe, it blocks until EOF. Not scored against §10 because stdin is the command's data channel, Fixed: `exec` now exits 2 with a `STDIN_IS_TTY` validation envelope when stdin is a terminal.
Discovered during §10 evaluation on 2026-09-24.

### §onboarding observation: `--version` is not recognised (fixed 2026-09-24)
`treaty --version` exits 2 ARG_ERROR (`unknown command '--version'`); the version is only available as the `treaty version` subcommand. Agents commonly probe `--version` first. Fixed: root-level `--version` now aliases the `version` command; below the root it stays unrecognised so command flags named `--version` are not shadowed.
Discovered during onboarding on 2026-09-24.
