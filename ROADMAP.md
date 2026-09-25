# Roadmap

Ordered by value to an agent using a treaty-built CLI. Requirement IDs refer to the
[CLI Agent Spec](../cli-agent-ergonomics/requirements/index.md).

## Done since the skeleton

- `treaty audit module:app` console script: nine ordered static rules over the registry,
  each with a generated fix, human and JSON output, `--all` and `--limit`; `treaty rules`
  lists the rule order
- `human=` renderer hook on `@app.command` for commands whose human output should not be
  raw JSON
- `treaty init <name>` scaffolds a project that passes the audit and the kit at all three
  levels; `--treaty-source` pins a local checkout until the PyPI release
- `treaty conformance module:app [--run]` derives probes from examples and danger levels,
  writes the profile, and runs the spec kit; fails with `CONFORMANCE_FAILED` on failing checks
- `treaty audit --strict` exits `AUDIT_FAILED` (79) on any warning or error finding, for
  CI; the envelope keeps the full report as `data` and advice never fails the run
- Human mode renders `data` on failed runs too, so `--strict` and a failing
  `conformance --run` print their report instead of raw JSON
- Handler-raised `ParseError` becomes a validation-phase exit 2 envelope
- `effect` on every mutating and destructive response (REQ-C-003, REQ-C-004) and a
  framework `--idempotency-key` with a per-app record store (REQ-C-007)
- `exec` stdin cap (64 KiB, `STDIN_TOO_LARGE`) with an uncapped `--input-file`
  (REQ-F-054, REQ-O-039)
- Secret flags (REQ-F-034, REQ-F-051): `secret=` on `Flag` and `Arg`, inferred from the
  name by default; their values and any unknown `--name=value` are never echoed in errors
- Response size cap (REQ-F-052): 1 MiB default, `--max-output` and
  `TREATY_MAX_OUTPUT_BYTES`, `meta.truncated` with a hint, and a `FIELD_TRUNCATED` warning
  per cut field (REQ-F-064)

## 0.1.0: first release

- Push to `romamo/treaty` (git initialised, first commit made)
- Reserve `treaty` on PyPI with the 0.0.1 wheel
- GitHub Actions: pytest, mypy, ruff, and the conformance kit against a spec checkout
- `meta.schema_version` on every response (REQ-F-022), derived from a per-command
  `schema_version=` declaration
- Warnings API: `ctx.warn(code, message, context)` so `warnings` stops being always empty
- `CHANGELOG.md`
- `docs/guide.md`: the judgement calls the audit cannot make (naming paths, what belongs in
  `error.context`, when a failure deserves its own exit code); short, because every
  mechanical step is now an audit rule

## 0.2.0: level 2 coverage

Every remaining P0 requirement the kit cannot yet check. Each new declaration gets a
matching audit rule so adoption never requires reading the spec.

- `--yes` and `--non-interactive` for commands declaring `interactive=True` (REQ-C-005),
  exit `4` when a prompt would block
- Validate-before-execute ordering with `phase` on every error (REQ-F-015); today only
  parse and preview errors set `phase: validation`
- Pagination metadata on list commands: `--limit`, `--cursor`, `meta.pagination`
  (REQ-F-018)
- `ALREADY_EXISTS` returning the existing resource in `data` (REQ-C-028) and a
  `would_affect` object on dry runs (REQ-C-004)
- Secrets only via env var or file (REQ-C-016, REQ-O-022): `--<name>-from-env` and
  `--<name>-from-file` for secret fields, a registration error for direct secret flags, and
  `secret_env_vars` in the manifest
- Pager suppression and locale-invariant serialization audit (REQ-F-010, REQ-F-005);
  both likely already hold and need tests, not code
- `REDIRECTED` exit `13` with `error.redirect` for renamed commands and `aliases` in the
  manifest

## 0.3.0: richer contracts

- Field validation presets and patterns in the manifest (REQ-C-020), including the
  hallucination-pattern rejection list (REQ-F-045)
- Conditional argument rules (REQ-C-026) and `option_placement: strict` for commands that
  forward trailing arguments (REQ-C-027)
- Multi-step commands with a step manifest and `completed_steps` on timeout and
  cancellation (REQ-C-008)
- Framework-managed locks with `retry_after_ms` (REQ-F-033)
- `--validate-only` (REQ-O-009) and safe-default dry run with `--live` (REQ-O-048)
- JSONL streaming for list commands with `--no-stream` (REQ-O-004)
- Dependency declarations and a `doctor` built-in (REQ-O-031)
- Token budget flags `--max-tokens` and `--fields` (REQ-O-049)

## Later

- **pydantic adapter** (`treaty[pydantic]`): a protocol seam in `_flags.py` and
  `_schema.py` so a `BaseModel` can serve as args or output type. First adapter to build
  when the extras are revisited
- **MCP adapter** (`treaty[mcp]`): one MCP tool per manifest entry over the exec dispatch
  path. A product-scope decision, not a framework gap
- **rich adapter** (`treaty[rich]`): human-mode rendering only. Low value
- Shell completion generated from the manifest
- Windows CI: signals are POSIX-only in the tests; the daemon-thread timeout already works
  there
- Benchmark: `benchmark/README.md` compares argparse, click, and treaty builds of the same
  CLI on the spec harness (done 2026-09-24; group help scoped to its subtree and shared
  exit codes hoisted to a root table the same day; S6 to S8 added for hangs, lost
  responses, and lossy text; `ARG_ERROR.context.available` now lists invocations scoped
  to the group after S8 showed agents typing registry keys literally, which halved the
  S8 token cost)

## Non-goals

- Runtime dependencies in core
- Python below 3.14
- Exposing `argparse` or any parser objects in the public API
- Mounting `App` instances as sub-apps; the registry stays flat
