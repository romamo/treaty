# Contributing to treaty

Thanks for helping. treaty is a framework other people's agents depend on, so every change
keeps the contract: the manifest, the envelope, and the exit codes mean the same thing
after it as before, unless the change says otherwise in the changelog.

## Before you start

- **A bug:** open an issue with the [bug form](https://github.com/romamo/treaty/issues/new?template=bug_report.yml),
  including the envelope the command printed
- **A feature or a change to the public API:** open an issue first, so the design is agreed
  before the code. The frozen surface is in [`docs/api.md`](docs/api.md)
- **A question:** ask in [Discussions](https://github.com/romamo/treaty/discussions)
- **A vulnerability:** follow [SECURITY.md](SECURITY.md), never a public issue

## Development setup

You need Python 3.14 and [uv](https://docs.astral.sh/uv/). Clone treaty beside the
[spec](https://github.com/cli-agent-spec/cli-agent-spec): the manifest and conformance
tests validate against its schemas in the sibling `cli-agent-ergonomics` checkout and skip
when it is absent (set `TREATY_SPEC_DIR` to point elsewhere).

```bash
git clone https://github.com/cli-agent-spec/cli-agent-spec cli-agent-ergonomics
git clone https://github.com/romamo/treaty
cd treaty
uv sync
```

## Checks

CI runs these; run them before you push:

```bash
uv run ruff check src tests examples
uv run ruff format --check src tests examples
uv run mypy src examples
uv run pytest -n auto
uv run treaty audit treaty._cli:cli --strict
uv run treaty check-docs treaty._cli:cli AGENTS.md
```

A pull request runs CI on Ubuntu; the `full-ci` label runs every OS.

## Pull requests

- One change per pull request, on a branch, with a test that fails without it
- Add an entry under `## [Unreleased]` in [`CHANGELOG.md`](CHANGELOG.md), in the section
  that matches the change: `Breaking`, `Added`, `Changed`, `Deprecated`, `Removed`,
  `Fixed`, or `Security`. The release bot picks the next version from these headings,
  so a breaking change without a `Breaking` entry ships as a minor release
- A change to a built-in command, flag, or environment variable updates `AGENTS.md`:
  `uv run treaty agents-md treaty._cli:cli` regenerates its generated sections
- A change to the documented API updates [`docs/reference.md`](docs/reference.md), and
  [`docs/api.md`](docs/api.md) when it touches the frozen surface
- Reference the issue it resolves (`Fixes #123`)

## Releases

Releases are cut by [shipmill](https://github.com/shipmill/shipmill) from the
hand-written changelog: a release candidate after each batch of merges to `main`, and a
stable release once its milestone closes and the candidate has soaked. Contributors never
bump the version or tag by hand.

## Code of Conduct

Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).
