# Type path arguments as Path

**Goal:** every argument that names a file or directory is a `pathlib.Path`, so the
framework refuses the paths agents get wrong, resolves relative ones where the caller
asked, and writes every path it returns in full

**You need:** a treaty app, such as `todo` at the end of [Declare exit codes](exit-codes.md),
whose example this chapter uses; this chapter clears the audit rule `path-typed`

**Done when:** the audit has no `path-typed` finding:

<!-- check -->
```bash
uv run treaty audit examples.tutorial.todo_exit_codes:app \
  | jq -e '[.data.rules[].findings[] | select(.rule == "path-typed")] == []'
```

The chapter uses `todo` as [Declare exit codes](exit-codes.md) left it,
[`examples/tutorial/todo_exit_codes.py`](../../../examples/tutorial/todo_exit_codes.py),
whose `--db` flag names the item file.

## Running the checks

Run the **Check** commands from the root of a treaty checkout, in order. `todo` runs the
example, and the item file goes to a scratch directory:

<!-- check -->
```bash
todo() { uv run examples/tutorial/todo_exit_codes.py "$@"; }
rm -rf tmp/tutorial && mkdir -p tmp/tutorial
```

Each check exits non-zero when it fails: JSON output goes through `jq -e`, which exits 1 when
the condition is false.

## Why a path is not a string

An agent builds paths by guessing. It climbs out of a directory with `../`, copies a URL's
`%2F` into a file name, or calls from a directory other than the one it thinks it is in. A
`str` field takes all of that as given, so the handler opens whatever the string happens to
name. A `Path` field is checked before the handler runs, and resolved against the directory
the caller named.

## Step 1: Find the path arguments

The `path-typed` rule reports `str` fields whose names say they are paths:

```bash
$ uv run treaty audit myapp.cli:app --format plain
...
  1. (warning) path-typed [add]: db_file looks like a path but is a str; traversal and encoded bytes are not rejected (heuristic)
     fix: db_file: Path = ... so the framework rejects '..', %XX and null bytes
```

It matches on the name: a word such as `path`, `dir`, `directory`, `file`, `folder`, or
`filename` in it, or a name such as `outfile`, `srcdir`, or `configpath`. It cannot see
`todo`'s own flag: a `db: str` passes the rule, while the same field named `db_file` is
reported. Go through your fields for the names the rule does not know: `db`, `output`,
`dest`, `target`, `source`, `root`, `cache`, `store`, `location`, `home`.

The rule also checks results: a `str` output field with a path-like name is reported, since
a relative path in `data` means nothing to a caller in another directory.

**Check:** `--db` is a path on every `todo` command; the manifest marks it `filepath`

<!-- check -->
```bash
todo manifest | jq -e '[.data.commands | .add, .list, .done, .purge | .flags.db.pattern_type]
  | all(. == "filepath")'
```

## Step 2: Type them `Path`

Annotate the field `Path`, `Path | None`, or `tuple[Path, ...]` for a list:

<!-- file: examples/tutorial/todo_exit_codes.py -->
```python
@dataclass(frozen=True, slots=True, kw_only=True)
class Common:
    db: Path | None = Flag(default=None, description="Item file; default ~/.todo.json")
```

Before any handler runs, on the command line, in `exec` lines, in `--raw-payload`, and over
MCP alike, the framework refuses three patterns with exit 2:

| Pattern | `rejected_pattern` | The suggestion |
| --- | --- | --- |
| a `..` segment anywhere, even in an absolute path | `path_traversal` | the absolute path it would have reached |
| a percent-encoded byte such as `%2F` or `%2e` | `percent_encoded` | the decoded path |
| a null byte | `null_byte` | |

The error names the flag and the value in `context`, so an agent sees which argument to fix,
and the suggestion shows the path it most likely meant. `pattern=` is not allowed on a
`Path` field; the checks above are the pattern.

**Check:** a climb out of the directory and an encoded slash are both refused before
anything runs, each with a suggestion

<!-- check -->
```bash
todo list --db ../todo.json | jq -e '.meta.exit_code == 2
  and .error.context.rejected_pattern == "path_traversal"
  and (.error.suggestion | startswith("pass the absolute path if intended: --db /"))'
todo list --db tmp/tutorial/a%2Fb.json | jq -e '.meta.exit_code == 2
  and .error.context.rejected_pattern == "percent_encoded"
  and .error.suggestion == "pass the decoded path: --db tmp/tutorial/a/b.json"'
```

## Step 3: Let `--cwd` place relative paths

Every command takes `--cwd DIR`, which says which directory the caller means: relative
`Path` arguments are resolved under it, and `meta.cwd` reports it. The process itself never
changes directory. An agent that works on several projects from one place passes
`--cwd` instead of building absolute paths for every argument.

Only `Path` fields are resolved. Type `--db` as `str` and run
`todo add x --cwd tmp/tutorial --db todo.json`: `meta.cwd` still reports `tmp/tutorial`, but
the item file is written to the directory the process started in, because the handler
opened the string as given. The response says one place and the file is in another.

**Check:** with `--cwd`, a relative `--db` lands under that directory; a directory that does
not exist is an argument error

<!-- check -->
```bash
todo add "Buy milk" --cwd tmp/tutorial --db todo.json \
  | jq -e '.meta.exit_code == 0 and (.meta.cwd | endswith("/tmp/tutorial"))'
test -f tmp/tutorial/todo.json
todo add "Buy milk" --cwd tmp/tutorial/missing --db todo.json \
  | jq -e '.meta.exit_code == 2 and .error.code == "ARG_ERROR"'
```

## Step 4: Return paths as `Path` too

A `Path` in a result is written absolute: a relative one is joined to `meta.cwd`, without
resolving symlinks, so `Path("reports/today.json")` reaches the caller as
`/project/reports/today.json`. A `str` field would hand back `reports/today.json`, which
the caller can only use from the same directory, and the `path-typed` rule reports it. See
[Type every command's output](typed-output.md) for the rest of the output types.

## What `Path` does not check

The checks catch the paths agents get wrong by accident. They do not decide which files a
command may touch:

- **An absolute path passes.** `--db /etc/hosts` is a valid path; `todo` then reads it and
  exits 79 with `STORE_CORRUPT`. If a command must stay inside a directory, check in the
  handler, where a relative path is already resolved against `--cwd`, and raise a declared
  exit code when it falls outside, before touching the file. `__post_init__` also sees the
  path as the caller typed it, relative to the process's directory, so a check there is
  wrong under `--cwd`
- **Nothing checks that the path exists**, or that it is a file rather than a directory.
  Check what the command needs, and raise an exit code the caller can act on, as `todo`'s
  store does with `STORE_UNWRITABLE`
- **Symlinks are followed** when the handler opens the path, as Python always does

## Next

The audit's next rule is `raw-payload`, for mutating commands with many fields:
[Accept a raw JSON payload](raw-payload.md).
