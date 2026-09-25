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

### §34: path traversal accepted in `--out` and `--directory` (fixed in treaty's CLI 2026-09-24)
`treaty conformance treaty._cli:cli --out ../../etc/test.json` wrote a file two levels above the repo with exit 0; `treaty init demo --directory ../../etc/test --dry-run` planned the same. The framework has no path-argument hardening (`../`, `%XX`, null bytes), so every treaty-built CLI inherits this.
Fixed for treaty's own CLI: `--out` and `--directory` reject `..` segments, percent-encodings and null bytes with a validation-phase ARG_ERROR whose `suggestion` gives the absolute or decoded form. Framework-level path hardening for other CLIs is still open.
Discovered during §34 evaluation on 2026-09-24.

### §42/§24: framework echoes raw flag values in errors; no sensitive-flag declaration
`treaty audit treaty._cli:cli --limit s3cr3t-value` returns `error.context.value: "s3cr3t-value"`. There is no `Flag(sensitive=True)` or env/file secret source, so a treaty-built CLI with a `--token` flag would echo the token on any parse error.
Discovered during §42 evaluation on 2026-09-24.

### §61: `exec` deadlocks callers that write the whole plan before reading (fixed 2026-09-24)
`exec` writes each envelope as it reads each line. With a 228KB plan, a caller that writes everything before reading blocks once the 64KB stdout pipe fills. There is no stdin size limit, STDIN_TOO_LARGE error or `--input-file` alternative.
Fixed: `exec` now reads stdin to EOF before dispatching, so a write-everything-then-read caller always finishes writing; results still stream per line. Still no stdin size limit or `--input-file`.
Discovered during §61 evaluation on 2026-09-24.

### §71: no documented install; stale wheel in `dist/`
README and HANDOFF.md have no install command. `dist/treaty-0.0.1-py3-none-any.whl` (built 2026-09-23) predates `_cli.py` and has no console-script entry point, so installing it gives no `treaty` binary. Installing from source works.
Discovered during §71 evaluation on 2026-09-24.

### §1/§14 candidate: explicit `--spec-dir` silently ignored when invalid (fixed 2026-09-24)
`treaty conformance treaty._cli:cli --run --spec-dir /nonexistent` exits 0 and runs the kit from the `../cli-agent-ergonomics` fallback. `find_spec_dir` treats the explicit flag as one candidate among several, so a typo in an explicit path is never reported.
Fixed: a `--spec-dir` or `TREATY_SPEC_DIR` without `conformance/run.py` now exits 4 PRECONDITION naming the source, checked before the profile is written; only unnamed discovery falls back to the sibling checkout.
Discovered during §1 evaluation on 2026-09-24.

### §43: no output size cap (fixed 2026-09-24)
`treaty manifest` returned 7.8KB and `exec` 2.4MB with no `meta.truncated`, `meta.total_bytes` or `--max-output`.
Fixed: JSON envelopes are capped at 1 MiB (`--max-output`, `TREATY_MAX_OUTPUT_BYTES`, `App(max_output_bytes=)`); the dominant list, object, or string is cut to the longest fitting prefix with `meta.truncated`, `total_bytes`, `truncation_hint`, and a `FIELD_TRUNCATED` warning per cut. Still no per-command `max_output_bytes` in the schema.
Discovered during §43 evaluation on 2026-09-24.
