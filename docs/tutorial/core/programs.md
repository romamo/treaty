# Run other programs

**Goal:** a command that runs another program (git, docker, a compiler) passes it arguments
that cannot be misread, gives it an environment where it never waits for a person, declares
it so `doctor` can check it is installed, and turns its failures into exit codes an agent
can act on

**You need:** a treaty app, such as `todo` at the end of [Declare exit codes](exit-codes.md);
this chapter covers the audit rules `subprocess-declared`, `required-tools`, and
`preserve-locale`

**Done when:** the strict audit exits 0 with a command that runs git:

<!-- check -->
```bash
uv run treaty audit examples.tutorial.todo_git:app --strict > /dev/null
```

The chapter gives `todo` a `save` command that commits the item file to the git repository
it is in. It starts from
[`examples/tutorial/todo_exit_codes.py`](../../../examples/tutorial/todo_exit_codes.py) and
ends at [`examples/tutorial/todo_git.py`](../../../examples/tutorial/todo_git.py).

## Running the checks

Run the **Check** commands from the root of a treaty checkout, in order, with git installed.
`todo` runs this chapter's example. The checks give git an identity to commit with, which
your own machine usually has already (`git config user.name`), and set
`GIT_CEILING_DIRECTORIES` so git never looks above the scratch directory and finds the
treaty checkout instead:

<!-- check -->
```bash
todo() { uv run examples/tutorial/todo_git.py "$@"; }
rm -rf tmp/tutorial && mkdir -p tmp/tutorial/repo tmp/tutorial/plain
export GIT_AUTHOR_NAME=todo GIT_AUTHOR_EMAIL=todo@example.com
export GIT_COMMITTER_NAME=todo GIT_COMMITTER_EMAIL=todo@example.com
export GIT_CEILING_DIRECTORIES="$PWD/tmp/tutorial"
git init -q tmp/tutorial/repo
todo add "Buy milk" --db tmp/tutorial/repo/todo.json > /dev/null
```

Each check exits non-zero when it fails: JSON output goes through `jq -e`, which exits 1 when
the condition is false.

## What goes wrong when a CLI runs a program

A command that shells out inherits every way the other program can fail an agent:

- **A shell reads the arguments.** `os.system(f"git commit -m {message}")` runs whatever the
  message contains; a message that starts with `-` is read as an option
- **The program waits for a person.** git opens an editor for the commit message, a pager
  for the log, a prompt for credentials; with no terminal, the call hangs
- **Its output changes with the machine.** A German locale translates git's messages and
  writes `1,5` for one and a half, and the agent's parsing breaks on some machines only
- **Its failure is exit 1.** The program's own exit code and message are lost, and the
  agent sees "something failed"

## Step 1: Run programs from an argument list

A handler runs other programs through `ctx.run`, with the arguments as a list:

<!-- file: examples/tutorial/todo_git.py -->
```python
    ctx.run(["git", "add", "--", store.path.name], cwd=here)
```

`here` is the directory the item file is in, `store.path.parent`, and `cwd=here` runs git
there. No shell ever sees the list: each item reaches git as one argument, so spaces, `*`,
`;`, and `$(...)` are text, not syntax. treaty enforces it. A string where the list belongs,
such as `ctx.run(f"git add {name}")`, fails registration with `SHELL_STRING_PROHIBITED` when
the source shows it, and a handler that calls `os.system`, `os.popen`, or anything with
`shell=True` fails registration too. `ctx.pipeline([[...], [...]])` joins programs the way
`|` does, still without a shell.

A migrated handler that calls `subprocess.run` with a list passes registration, but the
audit's `subprocess-declared` rule warns: outside `ctx.run` the child has no time limit or C
locale, and nothing declares it. Change the call to `ctx.run` with the same list. It returns
`returncode`, `stdout`, and `stderr` as text, but it differs from `subprocess.run` in two
ways: it raises on a non-zero exit unless you pass `check=False`, which code that reads
`returncode` itself needs, and it takes no `capture_output=` or `text=`, since it always
captures text. The same goes for a call imported as `from subprocess import run`; a call in
a helper function of your own code, such as `git_ops.save(...)`, is found too, but one in an
object's method is not, so check those by hand.

The `--` before the file name ends git's options: whatever the name is, git reads it as a
path. Put `--` before every argument that comes from the caller when the program supports
it.

## Step 2: Know what the child gets

Every program `ctx.run` starts gets an environment that keeps it from waiting for a person:

- **stdin is `/dev/null`**, unless the handler passes `input=`, so a prompt reads end of file
  and fails instead of hanging
- **No pager, no editor**: `PAGER`, `GIT_PAGER`, and `MANPAGER` are `cat`, and off a
  terminal `EDITOR`, `VISUAL`, and `GIT_EDITOR` are `true`, which exits at once
- **One language**: `LANG=C`, `LC_MESSAGES=C`, and `LC_NUMERIC=C`, so messages are English
  and numbers use a dot, on every machine, while `LC_CTYPE=C.UTF-8` (`C` where the system
  lacks it) keeps tools that need UTF-8 working; `LC_ALL` and your other `LC_*` variables
  are removed so they cannot override it. `preserve_locale=True` on the command turns
  it off, and the `preserve-locale` audit rule asks you to say why
- **No colour, no update notices**: `NO_COLOR=1`, and off a terminal `CI=1` and the
  notifier switches of npm, Homebrew, pip, and gh
- **The run's time limit**: the child gets what is left of the command's timeout; running
  out stops it and its own children, and the run exits with `TIMEOUT`
- **The run's temp directory** as `TMPDIR`, removed when the run ends

`env=` overrides single variables for one call.

## Step 3: Keep free text out of the arguments

`save` takes one flag of its own, the commit message, and returns the commit it made:

<!-- file: examples/tutorial/todo_git.py -->
```python
@dataclass(frozen=True, slots=True)
class Save(Common):
    message: str = Flag(default="Update todo items", description="Commit message", multiline=True)
```

<!-- file: examples/tutorial/todo_git.py -->
```python
@dataclass(frozen=True, slots=True)
class Saved:
    effect: str
    commit: str | None = Out(external=True)
    """The new commit's hash, as git printed it; null when the item file had no changes"""
```

`multiline=True` lets a message span lines; without it treaty refuses a line break in a
text flag, and the audit's `multiline-flag` rule suggests it for a field like this one.
`Out(external=True)` is explained in Step 6.

The commit message is free text: the caller may write anything, including `;`, `(`, or a
leading `-`. `save` never puts it in the argument list. git reads the message from stdin
with `--file -`, and `ctx.run` sends it there with `input=`:

<!-- file: examples/tutorial/todo_git.py -->
```python
    # The message goes in on stdin, never as an argument: it is free text
    ctx.run(["git", "commit", "--file", "-"], cwd=here, input=args.message)
```

Most programs have such a way in for text: a `--file -`, a `--stdin`, a config file. Use it
for anything a person would type, and keep the argument list for names and switches.

**Check:** a message with parentheses, a semicolon, and a leading dash is committed exactly
as written

<!-- check -->
```bash
todo save --message "-rf (urgent); plan the week" --db tmp/tutorial/repo/todo.json \
  | jq -e '.data.effect == "created" and (.data.commit | length) == 40'
test "$(git -C tmp/tutorial/repo log -1 --format=%s)" = "-rf (urgent); plan the week"
todo save --db tmp/tutorial/repo/todo.json | jq -e '.data.effect == "noop" and .data.commit == null'
```

The second `save` has nothing to commit and says so with `noop`, as a mutating command
should.

## Step 4: Declare the program

Two declarations on the command, `required_tools=` and `subprocess=`, tell an agent, and
`doctor`, what it runs; `Subprocess` comes from `treaty`, beside `App` and the rest. The
whole declaration of `save`:

<!-- file: examples/tutorial/todo_git.py -->
```python
@app.command(
    "save",
    description="Commit the item file to the git repository it is in",
    danger_level="mutating",
    exit_codes=["NOT_A_REPOSITORY"],
    required_tools={"git": "2.30.0"},
    subprocess=Subprocess(
        "git",
        user_controlled_args=("db",),
        hardcoded_args=("rev-parse", "add", "diff", "--cached", "commit", "--file", "-"),
    ),
    examples=[("Commit the items with a message", 'todo save --message "Plan the week"')],
)
def save(args: Save, ctx: Ctx, store: Store) -> Saved:
```

- **`required_tools=`** maps each program to its minimum version. `todo doctor` then checks
  git is installed and new enough, and names the fix when it is not. Without it the audit
  reports `(advice) required-tools [save]: runs 'git' (line N of the handler), which
  required_tools does not list, so doctor cannot check it is installed`
- **`subprocess=`** names the binary, the fields whose values become its arguments, and the
  fixed arguments its calls pass, here the git subcommands and the main switches `save`
  uses. The manifest publishes that list for a reader; treaty checks the fields' values,
  not the list. A declared field is checked before the handler runs: a value
  with a shell metacharacter, a line break, or a leading `-` exits 2 with
  `SHELL_METACHARACTER`. `--db` is declared because the file name git receives comes from
  it; `--message` is not, since it never becomes an argument

If `save` had no `subprocess=`, treaty would work one out: that needs every `ctx.run` in the
handler to take a list written out in place, starting with the same program as a string
literal. A field counts as user-controlled when an argument reads it, directly or through a
local such as `extra = list(args.extra)`. A worked-out declaration only describes the call in
the manifest; only a declaration by hand makes treaty check the values, as `save`'s does.

Where treaty cannot work one out, `subprocess-declared` warns, and you declare it by hand:
when the list is a variable (`ctx.run(cmd)`), the program is a constant (`[GIT, ...]`), an
argument comes from the whole arguments object (`*flags(args)`), or the command uses
`ctx.pipeline`. A command that runs two programs can declare only one, so move the second
into its own command. A `ctx.run` inside a helper function is not read at all: declare it on
the command, with `required_tools=`, so `doctor` checks the program.

**Check:** the schema names git and its version; `doctor` finds it; a `;` in `--db` is
refused before git runs

<!-- check -->
```bash
todo save --schema | jq -e '.data.subprocess.binary == "git" and .data.required_tools == {"git": "2.30.0"}'
todo doctor | jq -e '[.data.checks[] | select(.name == "git") | .ok] == [true]'
todo save --db "tmp/tutorial/repo/a;b.json" | jq -e '.meta.exit_code == 2
  and .error.code == "SHELL_METACHARACTER"'
```

## Step 5: Turn the program's failures into exit codes

`ctx.run` raises `SUBPROCESS_FAILED` (exit 1) when the program exits non-zero, with its
`argv`, `returncode`, and the last 4 KiB of its stderr in `error.context`, secrets redacted.
That is the right answer for a failure nobody can act on. For one the caller can act on,
run with `check=False`, read the return code, and raise a named exit code, as `save` does
when the item file is not in a repository. The code is registered like the ones in
[Declare exit codes](exit-codes.md), and `save` declares it with `exit_codes=["NOT_A_REPOSITORY"]`:

<!-- file: examples/tutorial/todo_git.py -->
```python
app.exit_code(
    "NOT_A_REPOSITORY",
    82,
    description="The item file is not in a git repository; nothing was committed",
    retryable=False,
    side_effects="none",
)
```

<!-- file: examples/tutorial/todo_git.py -->
```python
    inside = ctx.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=here, check=False)
    if inside.returncode != 0:
        raise Exit.NOT_A_REPOSITORY(
            f"{here} is not in a git repository",
            context={"directory": str(here), "git": inside.stderr.strip()},
            fix_required="--db must name a file inside a git working tree",
        )
```

`check=False` is also how a program that answers with its exit code is read:
`git diff --cached --quiet` exits 1 when something is staged, which `save` uses to decide
between `created` and `noop`. After the commit, `git rev-parse HEAD` prints the new commit's
hash, which `save` returns:

<!-- file: examples/tutorial/todo_git.py -->
```python
    staged = ctx.run(["git", "diff", "--cached", "--quiet"], cwd=here, check=False)
    if staged.returncode == 0:
        return Saved(effect="noop", commit=None)
```

<!-- file: examples/tutorial/todo_git.py -->
```python
    head = ctx.run(["git", "rev-parse", "HEAD"], cwd=here)
    return Saved(effect="created", commit=head.stdout.strip())
```

In your own project, try the missing-repository case with an item file outside every git
repository, such as one under a new directory in `/tmp`: inside your project's own
repository, git finds that repository instead. The checks set `GIT_CEILING_DIRECTORIES`
for the same reason: it stops git from looking above a directory.

To test `save` in your project, copy the `repository` fixture, the `_git_env` helper, and
the two tests under "Run other programs" in
[`tests/test_tutorial.py`](../../../tests/test_tutorial.py), with `os`, `shutil`,
`subprocess`, `Iterator`, `Path`, and `pytest` imported, calling your `app`. `_git_env`
gives git an author and sets `GIT_CEILING_DIRECTORIES`, so the tests never touch your own
repository.

A failure `save` does not name ends as `SUBPROCESS_FAILED`. The check below makes one with a
new item file the repository's `.gitignore` excludes: git refuses to add it. A file git
already tracks would be committed whatever `.gitignore` says, which is why the check uses a
new one.

**Check:** outside a repository, `save` exits 82 with git's own message in `context`; an
ignored item file fails `git add`, and the envelope carries git's argv, exit code, and
stderr

<!-- check -->
```bash
todo add "Walk dog" --db tmp/tutorial/plain/todo.json > /dev/null
todo save --db tmp/tutorial/plain/todo.json | jq -e '.meta.exit_code == 82
  and .error.code == "NOT_A_REPOSITORY" and (.error.context.git | startswith("fatal: not a git repository"))'
printf 'private.json\n' > tmp/tutorial/repo/.gitignore
todo add "Call mom" --db tmp/tutorial/repo/private.json > /dev/null
todo save --db tmp/tutorial/repo/private.json | jq -e '.meta.exit_code == 1 and .error.code == "SUBPROCESS_FAILED"
  and .error.context.argv == ["git", "add", "--", "private.json"]
  and (.error.context.stderr | contains("ignored"))'
```

## Step 6: Mark what the program printed

`save` returns the commit hash git printed. The audit's `external-data` rule treats anything
a command passes on from another program's output as content from outside, since it cannot
tell a value the handler checked from one it passed through. `save` marks the field:

<!-- file: examples/tutorial/todo_git.py -->
```python
    commit: str | None = Out(external=True)
```

For a hash the tag costs nothing; for a command that returns a child's text output, such as
a log or a file's contents, it is what keeps that text from being read as instructions. See
[Declare network commands](network-io.md#step-5-mark-what-came-from-outside).

This chapter added a command. If your project has AGENTS.md, from `treaty init` or [Ship the
agent docs](../ship/agent-docs.md), run `uv run treaty agents-md todo.cli:app`, or the
AGENTS.md test fails; if you generated skills and an MCP tool list there, regenerate them
too, as its [Step 6](../ship/agent-docs.md#step-6-gate-ci-on-all-three) does.

## Next

The audit's next rule is `path-typed`, for arguments that name files:
[Type path arguments as Path](path-typed.md).
