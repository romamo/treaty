# 14: Agent docs and integration artifacts

Size: M

Makes AGENTS.md a generated, checked artifact instead of hand-kept prose: the required
sections come from the registry, every static artifact names the version it describes, and
CI fails when the docs drift from the binary. Phase B and additive: two `treaty` dev
commands and no new exports. Builds on 02's env var registry (`_env.KNOWN`) and on 13's
`generate-skills` and `treaty-mcp --list-tools`.

| Requirement | Priority | Now | Gap | API change |
|-------------|----------|-----|-----|------------|
| O-043 AGENTS.md required content | P1 | Not started | Treaty's AGENTS.md lacks the four sections and the version comment; `treaty init` writes none | Additive: `treaty agents-md` |
| O-044 Non-interactive install docs | P1 | Partial | Heading is `## Install`, not `## Installation`; scaffold documents none; idempotency is untested | No |
| O-045 Artifact version declaration | P1 | Partial | MCP serves `app.version` in-process; static artifacts carry no version | No: fields inside generated files only |
| O-046 AGENTS.md CI validation | P2 | Not started | No check in treaty's CI or the scaffold's tests | Yes: `treaty check-docs`, exit `DOCS_OUT_OF_DATE` (81) |

## API impact

- No new exports or keywords: variables the app's code reads are declared through 02's
  `settings=` fields (`<APP>_<FIELD>`), which the inventory already covers
- `treaty agents-md` and `treaty check-docs` are commands of the treaty CLI, not
  built-ins of every app: they read source files and belong to the author's workflow
- Decision 14-D1 may change the plain rendering of `version`; JSON is unchanged
- New audit rule `agents-md`; a second finding kind on 02's `env-prefix` rule

## Design

### Environment variable inventory (O-043)

A new `_envvars.py` with `env_vars(app) -> tuple[EnvVarDoc, ...]` (private: `name`,
`type`, `required`, `default`, `description`), sorted by name. It reads 02's `_env.KNOWN`
(`<APP>_FORMAT`, `<APP>_MAX_OUTPUT_BYTES`, `<APP>_STATE_DIR`, `<APP>_CONFIG`, and the
rest after the F-073 rename), each `settings=` field as `<APP>_<FIELD>` with its type,
each command's `secret_env_vars` and `token_env_vars` (`_command.py`), and the unprefixed
variables treaty honors (`NO_COLOR`, `CI`, `XDG_STATE_HOME`, `XDG_CONFIG_HOME`, proxies).
Two sources giving one name different types is a `RegistrationError`. 02 already lists
these in root `--help` (02-D1); this is the same list, so AGENTS.md and `--help` cannot
disagree.

02's `env-prefix` rule AST-scans handlers and `acquire` for literal names in `ctx.env`,
`os.environ`, and `os.getenv`. It gains a second finding: a prefixed name that is not in
the inventory, so AGENTS.md could not list it; the fix is the `settings=` field to add.

### `treaty agents-md` (O-043, O-044, O-045)

`treaty agents-md module:app --path AGENTS.md [--invocation TEXT] [--install TEXT]`
(mutating; `effect` is `created`, `updated`, or `noop`) renders from the registry, in a new
`_agents_md.py`:

- Line 1 `<!-- cli-version: {app.version} -->`, then `# AGENTS.md: {app.name}`
- `## Canonical Invocation`: `--invocation`, default `app.name`
- `## Installation`: `--install`, default `uv tool install {app.name}`, followed directly by
  `{app.name} --version` as the verification command (O-044)
- `## Non-Interactive Flags`: the framework flags that answer or suppress a prompt, taken
  from `_framework.FLAGS` (`--yes`, `--non-interactive`, `--confirm-destructive`, `--live`,
  `--headless`), each with the commands it applies to, plus `--format json`
- `## Environment Variables`: `env_vars(app)` as `` `NAME` (type, required|optional): description ``
- `## Input Conventions`: flags and positionals, `--input-file` and stdin for
  `stdin_input=True` commands, `--raw-payload`, and JSONL through `exec`
- `## CI Validation`: the `treaty check-docs` command (O-046)

Generated sections sit between `<!-- treaty:begin -->` and `<!-- treaty:end -->`; text
outside the markers (such as treaty's `## Developing`) is kept on regeneration, so the
file stays hand-editable. A missing file is created whole.

### `treaty check-docs` (O-043, O-045, O-046)

`treaty check-docs module:app PATH...` (safe) accepts AGENTS.md, a `generate-skills`
directory, and a `treaty-mcp --list-tools` JSON file (workstream 13). Per file:

- Version: the `cli-version` comment (Markdown), frontmatter `version:` (skill files), or
  `"cli_version"` (JSON) must equal `app.version`
- Sections (AGENTS.md only): the six headings above, each non-empty
- Names: commands (backticked `{app.name} <words>` spans) must resolve through
  `App._command_named`; flags are checked only inside those spans and in the
  Non-Interactive Flags section, so `uv tool install --reinstall` is not a false positive;
  env vars are checked in the Environment Variables section and anywhere as a backticked
  `<APP>_*` name
- The source of truth is the registry, which is what `--help` renders; a test asserts that
  every registered flag appears in `_help.render_command`, so "in the registry" implies
  "in `--help`"

Drift raises `DOCS_OUT_OF_DATE` with `data.mismatches` (`file`, `line`, `kind`, `name`,
`problem`); the plain renderer prints one diff-style line per item, such as
`- AGENTS.md:14 flag --replicas: not a flag of deployctl`.

### Where the check runs (O-044, O-046)

- Treaty: regenerate `AGENTS.md` with `uv run treaty agents-md treaty._cli:cli --path
  AGENTS.md`, keeping `## Developing` outside the markers, and add a `docs` step to the
  `lint` job in `.github/workflows/ci.yml` (`uv run treaty check-docs treaty._cli:cli
  AGENTS.md`). CI runs on every push and pull request, a superset of the commits that
  touch AGENTS.md or CLI source
- Install idempotency: the `scaffold` job runs the generated project's Installation block
  twice with `< /dev/null` on all three OSes; `publish.yml` runs treaty's own block twice
  after upload. Neither opens a browser or wizard (`uv` never does)
- Scaffold (`_scaffold.py`): writes `AGENTS.md` and `tests/test_agents_md.py`, which runs
  the `treaty check-docs` console script from the same environment, so a new project's
  first `uv run pytest` enforces O-046 without a CI file
- Audit rule `agents-md` (advice, like `_profile`): `./AGENTS.md` missing or failing the
  check; fix `uv run treaty agents-md module:app --path AGENTS.md`

### Integration artifacts (O-045)

Every artifact treaty emits carries `app.version`: AGENTS.md and `CONTEXT.md` the comment,
`SKILL-*.md` the frontmatter, `--list-tools` JSON `cli_version`, the live MCP server its
`serverInfo.version` (already). All are produced by the binary they describe, which meets
packaging option (a). OpenAPI, LangChain, and companion packages: not applicable, treaty
emits none.

## Decisions

| ID | Question | Recommendation |
|----|----------|----------------|
| 14-D1 | O-043 wants `cli-version` to equal `<binary> --version` output, but it prints an envelope | Compare with `data.version`; make the plain renderer of `version` print the bare version so `--version --format plain` matches the comment literally |
| 14-D2 | Should `generate-skills` (13) also write AGENTS.md, as O-034's related row suggests? | No: AGENTS.md lives in the repo and is regenerated by the author; skill files are runtime output. Both share `_envvars.py` and the section renderers |

## Tasks

- [ ] `_envvars.py` over 02's `_env.KNOWN`, settings, secrets, and tokens; the new `env-prefix` finding
- [ ] `_agents_md.py` and `treaty agents-md` with marker-preserving rewrite
- [ ] `treaty check-docs` for AGENTS.md, skill directories, and `--list-tools` JSON; `DOCS_OUT_OF_DATE`
- [ ] Plain `version` rendering (14-D1); test that every registered flag appears in `--help`
- [ ] Regenerate treaty's `AGENTS.md`; `docs` step in `ci.yml`; install idempotency in `scaffold` job and `publish.yml`
- [ ] Scaffold `AGENTS.md` and `tests/test_agents_md.py`; `agents-md` audit rule
- [ ] Update COMPLIANCE.md rows (O-043 to O-046), README, HANDOFF, ROADMAP
