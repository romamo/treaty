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

### §34: path traversal accepted in `--out` and `--directory` (fixed 2026-09-24, framework 2026-09-25)
`treaty conformance treaty._cli:cli --out ../../etc/test.json` wrote a file two levels above the repo with exit 0; `treaty init demo --directory ../../etc/test --dry-run` planned the same. The framework has no path-argument hardening (`../`, `%XX`, null bytes), so every treaty-built CLI inherits this.
Fixed for treaty's own CLI first: `--out` and `--directory` reject `..` segments, percent-encodings and null bytes with a validation-phase ARG_ERROR whose `suggestion` gives the absolute or decoded form. Framework fix 2026-09-25: any field annotated `pathlib.Path` gets the same checks on argv, `exec` and `--raw-payload`, reports `rejected_pattern` (`path_traversal`, `percent_encoded`, `null_byte`) in `error.context`, and is listed with `pattern_type: "filepath"` in the manifest; treaty's `--out`, `--directory`, `--spec-dir` and `exec --input-file` are `Path` fields now, and the `path-typed` audit rule warns about `str` fields whose names look like paths.
Discovered during §34 evaluation on 2026-09-24.

### §42/§24: framework echoes raw flag values in errors; no sensitive-flag declaration (fixed 2026-09-25)
`treaty audit treaty._cli:cli --limit s3cr3t-value` returns `error.context.value: "s3cr3t-value"`. There is no `Flag(sensitive=True)` or env/file secret source, so a treaty-built CLI with a `--token` flag would echo the token on any parse error.
Fixed: `Flag(secret=True)`/`Arg(secret=True)`, inferred from names containing token/secret/password/key/credential/auth, redacts the value as `[REDACTED]` in every validation error (argv, `--raw-payload`, `exec`); unknown `--name=value` tokens are reported without the value. §24 closed the same day: a secret field has no direct flag at all. The parser exposes `--x-from-env VAR` and `--x-from-file PATH` (the file path hardened like any `Path`), reads `<APP>_<X>` when neither is given, and rejects `--x VALUE` with exit 2 naming the two sources without echoing the value. Missing variable, unreadable or empty file exit 2 in the validation phase. The manifest lists the two source flags and `secret_env_vars`; positional, boolean, array, and short-flag secrets are registration errors.
Discovered during §42 evaluation on 2026-09-24.

### §61: `exec` deadlocks callers that write the whole plan before reading (fixed 2026-09-24)
`exec` writes each envelope as it reads each line. With a 228KB plan, a caller that writes everything before reading blocks once the 64KB stdout pipe fills. There is no stdin size limit, STDIN_TOO_LARGE error or `--input-file` alternative.
Fixed: `exec` now reads stdin to EOF before dispatching, so a write-everything-then-read caller always finishes writing; results still stream per line. Stdin cap and `--input-file` added 2026-09-25: a piped plan over 64 KiB (`TREATY_MAX_STDIN_BYTES`) exits 2 `STDIN_TOO_LARGE` before dispatch, and `--input-file PATH` reads any size from a file.
Discovered during §61 evaluation on 2026-09-24.

### §71: no documented install; stale wheel in `dist/` (fixed 2026-09-25)
README and HANDOFF.md have no install command. `dist/treaty-0.0.1-py3-none-any.whl` (built 2026-09-23) predates `_cli.py` and has no console-script entry point, so installing it gives no `treaty` binary. Installing from source works.
Fixed: README and a new AGENTS.md document `uv tool install --reinstall <checkout>` (CLI) and `uv add --editable <checkout>` (library), both verified non-interactive and exit 0 on a second run, with `treaty --version` as the verify command. `dist/` rebuilt with `uv build`; the new wheel has the `treaty` console script.
Discovered during §71 evaluation on 2026-09-24.

### §1/§14 candidate: explicit `--spec-dir` silently ignored when invalid (fixed 2026-09-24)
`treaty conformance treaty._cli:cli --run --spec-dir /nonexistent` exits 0 and runs the kit from the `../cli-agent-ergonomics` fallback. `find_spec_dir` treats the explicit flag as one candidate among several, so a typo in an explicit path is never reported.
Fixed: a `--spec-dir` or `TREATY_SPEC_DIR` without `conformance/run.py` now exits 4 PRECONDITION naming the source, checked before the profile is written; only unnamed discovery falls back to the sibling checkout.
Discovered during §1 evaluation on 2026-09-24.

### §43: no output size cap (fixed 2026-09-24)
`treaty manifest` returned 7.8KB and `exec` 2.4MB with no `meta.truncated`, `meta.total_bytes` or `--max-output`.
Fixed: JSON envelopes are capped at 1 MiB (`--max-output`, `TREATY_MAX_OUTPUT_BYTES`, `App(max_output_bytes=)`); the dominant list, object, or string is cut to the longest fitting prefix with `meta.truncated`, `total_bytes`, `truncation_hint`, and a `FIELD_TRUNCATED` warning per cut. Still no per-command `max_output_bytes` in the schema.
Discovered during §43 evaluation on 2026-09-24.

### §12: no idempotency key or effect field (fixed 2026-09-25)
`treaty init --idempotency-key k1` was rejected as an unknown flag and no response carried `effect`.
Fixed: mutating and destructive commands must return `effect` (`created`/`updated`/`deleted`/`noop`, or `would_*` on dry runs), checked at registration and on every run, and get a framework `--idempotency-key`. A repeat with the same key replays the stored result as `effect: "noop"` with `meta.idempotency_hit`; a different call on the same key exits 6 `IDEMPOTENCY_KEY_REUSED`. Records are per-key locked files in the app's state directory, expiring after 24 hours.
Discovered during §12 evaluation on 2026-09-24.
