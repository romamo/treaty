## What and why

<!-- What changes, and the problem it solves. Link the issue: Fixes #123 -->

## Checklist

- [ ] A test that fails without this change
- [ ] An entry under `## [Unreleased]` in `CHANGELOG.md`, in the matching section (`Breaking` for anything that breaks an app)
- [ ] `AGENTS.md`, `docs/reference.md`, and `docs/api.md` updated where the change touches them
- [ ] `ruff`, `mypy`, `pytest`, `treaty audit --strict`, and `treaty check-docs` pass locally
