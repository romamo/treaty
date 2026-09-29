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
`todo` runs this chapter's example. The checks give git an identity to commit with, and set
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
the condition is false. `tests/test_tutorial.py` runs the checks the same way.

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

No shell ever sees the list: each item reaches git as one argument, so spaces, `*`, `;`, and
`$(...)` are text, not syntax. treaty enforces it. A string where the list belongs, such as
`ctx.run(f"git add {name}")`, fails registration with `SHELL_STRING_PROHIBITED` when the
source shows it, and a handler that calls `os.system`, `os.popen`, or anything with
`shell=True` fails registration too. `ctx.pipeline([[...], [...]])` joins programs the way
`|` does, still without a shell.

The `--` before the file name ends git's options: whatever the name is, git reads it as a
path. Put `--` before every argument that comes from the caller when the program supports
it.

## Step 2: Know what the child gets

Every program `ctx.run` starts gets an environment that keeps it from waiting for a person:

- **stdin is `/dev/null`**, unless the handler passes `input=`, so a prompt reads end of file
  and fails instead of hanging
- **No pager, no editor**: `PAGER`, `GIT_PAGER`, and `MANPAGER` are `cat`, and off a
  terminal `EDITOR`, `VISUAL`, and `GIT_EDITOR` are `true`, which exits at once
- **One language**: `LC_ALL=C`, so messages are English and numbers use a dot, on every
  machine; `preserve_locale=True` on the command turns it off, and the `preserve-locale`
  audit rule asks you to say why
- **No colour, no update notices**: `NO_COLOR=1`, and off a terminal `CI=1` and the
  notifier switches of npm, Homebrew, pip, and gh
- **The run's time limit**: the child gets what is left of the command's timeout; running
  out stops it and its own children, and the run exits with `TIMEOUT`
- **The run's temp directory** as `TMPDIR`, removed when the run ends

`env=` overrides single variables for one call.

## Step 3: Keep free text out of the arguments

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

Two declarations on the command tell an agent, and `doctor`, what it runs:

<!-- file: examples/tutorial/todo_git.py -->
```python
    required_tools={"git": "2.30.0"},
    subprocess=Subprocess(
        "git", user_controlled_args=("db",), hardcoded_args=("add", "commit", "--file", "-")
    ),
```

- **`required_tools=`** maps each program to its minimum version. `todo doctor` then checks
  git is installed and new enough, and names the fix when it is not. Without it the audit
  reports `(advice) required-tools [save]: runs 'git' (line 10 of the handler), which
  required_tools does not list, so doctor cannot check it is installed`
- **`subprocess=`** names the binary, the fields whose values become its arguments, and the
  arguments it always passes. A declared field is checked before the handler runs: a value
  with a shell metacharacter, a line break, or a leading `-` exits 2 with
  `SHELL_METACHARACTER`. `--db` is declared because the file name git receives comes from
  it; `--message` is not, since it never becomes an argument

treaty reads the argument lists written as list literals in the handler, as `save`'s are,
and derives a declaration from them when there is none. The `subprocess-declared` rule warns
when it cannot: when the list is built at run time, such as `ctx.run(["git", *extra])`, the
manifest cannot say which binary gets which argument unless you declare it.

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
when the item file is not in a repository:

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
between `created` and `noop`.

**Check:** outside a repository, `save` exits 82 with git's own message in `context`; an item
file the repository ignores (a new one; a tracked file is committed whatever `.gitignore`
says) fails `git add`, and the envelope carries git's argv, exit code, and stderr

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

## Next

The audit's next rule is `path-typed`, for arguments that name files:
[Type path arguments as Path](path-typed.md).
