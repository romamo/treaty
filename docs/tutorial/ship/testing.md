# Test the contract and gate CI

**Goal:** one test suite and one CI job hold a CLI to everything the tutorial built, so a
change that would break an agent fails a pull request instead

**You need:** a treaty app in a project with pytest, such as the one from [Start a new
CLI](../A-new/start.md) or a migrated CLI; Step 2 adds the one test a migrated project lacks

**Done when:** the project's tests pass with the contract tests in them, and fail when an
example goes stale:

```bash
uv run pytest -q
```

The chapter gathers what the other chapters test one at a time. It adds two files to a
`todo` project:
[`examples/tutorial/new_cli/test_contract.py`](../../../examples/tutorial/new_cli/test_contract.py),
tests that hold for any treaty app, and
[`examples/tutorial/new_cli/agent-contract.yml`](../../../examples/tutorial/new_cli/agent-contract.yml),
a CI job that runs every gate.

## Running the checks

Run the **Check** commands from the root of a treaty checkout, in order. The first one makes
a `todo` project as [Start a new CLI](../A-new/start.md) does, with `todo`'s commands from
[`todo_exit_codes.py`](../../../examples/tutorial/todo_exit_codes.py), and changes into it:

<!-- check -->
```bash
examples="$PWD/examples/tutorial"
rm -rf tmp/tutorial && mkdir -p tmp/tutorial
uv run treaty init todo --directory tmp/tutorial/todo --treaty-source "$PWD" > /dev/null
cp "$examples/todo_exit_codes.py" tmp/tutorial/todo/src/todo/cli.py
cp "$examples/new_cli/test_cli.py" "$examples/new_cli/test_contract.py" tmp/tutorial/todo/tests/
cd tmp/tutorial/todo
uv sync -q
uv run treaty agents-md todo.cli:app > /dev/null
```

Each check exits non-zero when it fails. `--treaty-source "$PWD"` makes the project depend
on this checkout, so the checks test the treaty they run in; it writes an absolute path into
`pyproject.toml` that only works on this machine. Your own project, made with
`uvx treaty init todo`, depends on the released treaty and has no such path.

## What to test

An agent relies on the contract, not just on the commands working, so the suite tests both.
Each layer catches something the others cannot:

| Layer | Catches | How | Chapter |
| --- | --- | --- | --- |
| Behaviour | a command that does the wrong thing | `app.call()` per command, reading the envelope | [Start a new CLI](../A-new/start.md#step-7-replace-the-tests) |
| Declarations | a missing danger level, exit code, type, or declaration | `treaty audit --strict` | [the index](../index.md#after-the-first-chapter-follow-the-audit) |
| Examples | an example an agent would copy and fail with | every example through `--validate-only` | [Describe every command](../core/describe.md#step-4-test-every-example) |
| Output shapes | a handler that returns what its schema does not say | each result against `output_schema` | [Type every command's output](../core/typed-output.md#step-3-test-that-handlers-keep-the-promise) |
| Agent docs | AGENTS.md, skills, or the MCP tool list out of step | `check-docs`, and regenerate plus `git diff` | [Ship the agent docs](agent-docs.md) |
| Releases | a command, flag, or code gone without notice | `treaty audit --baseline` | [Change the contract safely](stability.md) |
| Runtime | stdout, prompts, colour, refusals as the binary really behaves | the conformance kit | [Run the conformance kit](conformance.md) |
| Edges | network failures, other programs, signals | a local server, a real repository, `kill -TERM` | [network](../core/network-io.md), [programs](../core/programs.md), [cleanup](../core/cleanup.md) |

## Step 1: Test behaviour through the envelope

A behaviour test calls a command in-process with `app.call()`, the path `exec` and MCP take,
and reads the envelope: `exit_code`, `error.code`, `data`. No subprocess and no parsing of
printed text, so a test runs in milliseconds and fails with the field that is wrong.
`app.run(argv, stdout=..., stderr=...)` covers what `app.call` does not: the command line
itself, renderers, and what reaches stderr. The calls pass `env=QUIET`, which turns the
audit log off, so a test run never lands in your real one; a test that passes an idempotency
key also sets a scratch `TODO_STATE_DIR` in it. `todo`'s behaviour tests are the four in
[`new_cli/test_cli.py`](../../../examples/tutorial/new_cli/test_cli.py).

## Step 2: Test the contract in every project

Some tests hold for any treaty app, whatever its commands do.
[`test_contract.py`](../../../examples/tutorial/new_cli/test_contract.py) holds them; copy
it into `tests/`, and change `APP`, your app's import path, and the `from todo.cli import app`
line to your app. It runs the strict audit through `treaty(...)`, a helper at the top of the
file that runs the `treaty` command of the test's own environment:

<!-- file: examples/tutorial/new_cli/test_contract.py -->
```python
def test_the_strict_audit_passes() -> None:
    done = treaty("audit", APP, "--strict", "--format", "plain")
    assert done.returncode == 0, done.stdout + done.stderr
```

passes every example of the app's own commands through `--validate-only`, as
[Describe every command](../core/describe.md#step-4-test-every-example) explains, and
checks that every command declares its output and that the manifest is JSON. With the
`test_agents_md.py` that `treaty init` writes, which fails when AGENTS.md no longer matches
the app, `uv run pytest` covers every layer that needs no network.

A migrated project has no `test_agents_md.py`. Add the same check to `test_contract.py`,
with the `treaty(...)` helper already at its top:

```python
def test_agents_md_matches_the_cli() -> None:
    done = treaty("check-docs", APP, "AGENTS.md", "--format", "plain")
    assert done.returncode == 0, done.stdout + done.stderr
```

Behaviour that depends on your commands stays in your own tests: validating each result
against its `output_schema` needs a call per command with real arguments, as in
[Type every command's output](../core/typed-output.md#step-3-test-that-handlers-keep-the-promise).

**Check:** the project's tests pass: behaviour, the contract, and AGENTS.md

<!-- check -->
```bash
uv run pytest -q > ../pytest.out
grep -q ' passed' ../pytest.out
```

## Step 3: See a stale example fail

The contract tests are there for the change nobody thinks to check. Rename a flag in an
example, as a refactoring does when it renames the flag and misses the example, and the
suite fails before any agent copies it.

**Check:** with `--priority` misspelled in `add`'s example, the example test fails; with the
file restored, it passes again

<!-- check -->
```bash
sed -i.bak 's/--priority high/--urgency high/' src/todo/cli.py
uv run pytest -q -k test_an_example_parses > ../stale.out || true
grep -q '1 failed' ../stale.out
mv src/todo/cli.py.bak src/todo/cli.py
uv run pytest -q -k test_an_example_parses > ../fixed.out
```

In a project where another example also uses `--priority high`, such as one with `edit`,
the sed renames it there too, and two tests fail.

## Step 4: Gate every pull request

The CI job runs the suite and the gates that need more than a test run:

<!-- file: examples/tutorial/new_cli/agent-contract.yml -->
```yaml
      # Behaviour, the contract tests, and AGENTS.md against the binary
      - run: uv run pytest -q

      # Nothing the last release promised is gone. Runs once there is a release to compare
      # with: save its manifest as todo-1.0.0.json, and move to the next at each release
      - if: hashFiles('todo-1.0.0.json') != ''
        run: uv run treaty audit todo.cli:app --baseline todo-1.0.0.json --strict

      # The agent docs and the MCP tool list are regenerated and committed; treaty-mcp
      # needs the mcp extra in the project: uv add "treaty[mcp]"
      - name: Agent docs are up to date
        run: |
          uv run treaty agents-md todo.cli:app
          rm -rf skills && uv run todo generate-skills --output-dir skills
          uv run treaty-mcp todo.cli:app --list-tools > mcp-tools.json
          git add --intent-to-add AGENTS.md skills mcp-tools.json
          git diff --exit-code AGENTS.md skills mcp-tools.json
          uv run treaty check-docs todo.cli:app AGENTS.md skills mcp-tools.json

      # The runtime checks the audit cannot make; ../cli-agent-ergonomics is where treaty
      # looks for the kit when TREATY_SPEC_DIR is not set. A profile that no longer matches
      # the commands fails with CONFLICT: run with --force and commit the new one
      - name: Conformance kit
        run: |
          git clone --depth 1 https://github.com/cli-agent-spec/cli-agent-spec ../cli-agent-ergonomics
          uv run treaty conformance todo.cli:app --run
```

These are the job's steps; the [whole file](../../../examples/tutorial/new_cli/agent-contract.yml)
adds the checkout and setup around them. Save it as `.github/workflows/agent-contract.yml`.

- **`git diff --exit-code`** fails the job when a regenerated agent doc differs from the
  committed one, or, after `git add --intent-to-add`, when one was never committed
- **The conformance step** clones the spec repository, so the job needs network access to
  GitHub, and fails with `CONFLICT` when the conformance profile no longer matches the
  commands
- **The baseline step** is skipped until `todo-1.0.0.json` exists. At each release, save the
  new manifest as the next baseline, as
  [Change the contract safely](stability.md#step-1-keep-the-last-releases-manifest) describes
- **`treaty agents-md`** is a treaty command that reads the app, while **`generate-skills`**
  is a built-in of every treaty app, so it runs as `todo generate-skills`; the job removes
  `skills` first, for the reason [Ship the agent docs](agent-docs.md#step-6-gate-ci-on-all-three)
  gives

With these gates, the pull request that changed a command also shows what changed for agents.

Give the conformance kit's launcher a sandbox before the job runs it on anything that holds
real data, as [Run the conformance kit](conformance.md#step-2-keep-the-probes-away-from-real-data)
shows.

## Step 5: Lint with ruff's ALL rules

A project that lints with ruff and `select = ["ALL"]` gets three findings on every treaty
command. None of them is a bug, and each has a setting or a spelling that ends it:

- **`RUF009`** on an `Arg()`, `Flag()`, or `Out()` default: ruff takes the call for a
  mutable default, but each returns a fresh field description. Declare the three immutable
- **`ARG001`** on a handler's unused `ctx`, and **`ARG003`** on a resource's `acquire`:
  name it `_ctx`. treaty passes `ctx` by position, so the name is yours
- **`TC003`** wants `from pathlib import Path` under `if TYPE_CHECKING:`. That breaks the
  app: treaty reads the annotations of the arguments and output dataclasses when a command
  is registered, and a name imported only for type checkers raises `RegistrationError`.
  `runtime-evaluated-decorators` tells ruff that a `@dataclass` needs its annotations at
  runtime, which keeps those imports where they are

```toml
[tool.ruff.lint]
select = ["ALL"]

[tool.ruff.lint.flake8-bugbear]
extend-immutable-calls = ["treaty.Arg", "treaty.Flag", "treaty.Out"]

[tool.ruff.lint.flake8-type-checking]
runtime-evaluated-decorators = ["dataclasses.dataclass"]
```

A handler's own parameters are read too, so a module that imports `Ctx` or a resource class
only for a handler's annotation gets `TC001` or `TC002` for it. Keep those imports with
`per-file-ignores`, such as `"src/todo/commands/*.py" = ["TC001", "TC002", "TC003"]`. An
import used only by a helper, such as the `Sequence` in a function that renders a table,
can move under `TYPE_CHECKING`: treaty never reads those annotations.

**Check:** with the settings and `_ctx`, neither `RUF009` nor `ARG` fires on `todo` and
its tests still pass; `todo` uses `Path` at runtime, so a small arguments class shows that
`TC003` fires without the setting and not with it. `todo` has no ruff of its own, so the
check runs the treaty checkout's, a dev dependency there; in your project, `uv add --dev ruff`
and run `uv run ruff check`

<!-- check -->
```bash
cat >> pyproject.toml <<'EOF'

[tool.ruff.lint]
select = ["ALL"]

[tool.ruff.lint.flake8-bugbear]
extend-immutable-calls = ["treaty.Arg", "treaty.Flag", "treaty.Out"]

[tool.ruff.lint.flake8-type-checking]
runtime-evaluated-decorators = ["dataclasses.dataclass"]
EOF
sed -i.bak 's/ ctx: Ctx/ _ctx: Ctx/' src/todo/cli.py && rm src/todo/cli.py.bak
ruff() { uv run --project "$examples/../.." ruff "$@"; }
ruff check --select RUF009,ARG001,ARG003 src/todo/cli.py > /dev/null
uv run pytest -q > ../lint.out
grep -q ' passed' ../lint.out
cat > ../show_args.py <<'EOF'
from dataclasses import dataclass
from pathlib import Path

from treaty import Flag


@dataclass(frozen=True, slots=True)
class ShowArgs:
    path: Path = Flag(description="File to show")
EOF
ruff check --isolated --target-version py314 --select TC003 ../show_args.py > /dev/null \
  || flagged=yes
test "$flagged" = yes
ruff check --config pyproject.toml --select TC003 ../show_args.py > /dev/null
```

## Next

That is the end of the tutorial: `todo` is tested as an agent uses it, and every change to
its contract shows up in review. From here, keep the audit in the loop, and
[the index](../index.md#after-the-first-chapter-follow-the-audit) maps each rule to its
chapter.
