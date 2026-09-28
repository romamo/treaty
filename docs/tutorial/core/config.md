# Read settings and secrets

**Goal:** a command reads its settings through treaty, so an agent can see where every value
came from, and takes its secrets from the environment or a file, never from the command line
or into any output

**You need:** a treaty app, such as `todo` at the end of
[Declare network commands](network-io.md); this chapter clears the audit rules
`settings-declared` and `env-prefix`

**Done when:** the strict audit exits 0 for a `todo` whose `import` reads a default feed from
its settings and sends a token:

<!-- check -->
```bash
uv run treaty audit examples.tutorial.todo_config:app --strict > /dev/null
```

The chapter starts from
[`examples/tutorial/todo_network.py`](../../../examples/tutorial/todo_network.py) and ends at
[`examples/tutorial/todo_config.py`](../../../examples/tutorial/todo_config.py): `import`
gains a default feed URL from the settings, and a token for feeds that require one.

## Running the checks

Run the **Check** commands from the root of a treaty checkout, in order. `todo` runs this
chapter's example. The checks point `XDG_CONFIG_HOME` at a scratch directory and clear the
chapter's variables, so a config file or a variable of your own cannot change what they see:

<!-- check -->
```bash
todo() { uv run examples/tutorial/todo_config.py "$@"; }
rm -rf tmp/tutorial && mkdir -p tmp/tutorial/project
export XDG_CONFIG_HOME="$PWD/tmp/tutorial/xdg"
unset TODO_FEED_URL TODO_TOKEN
```

Each check exits non-zero when it fails: JSON output goes through `jq -e`, which exits 1 when
the condition is false. `tests/test_tutorial.py` runs the checks the same way; the calls
that need a feed run there too, against a local one that requires a token.

## Why treaty reads the settings

A setting that a handler reads for itself, from a file it opens or a variable it looks up,
is invisible. An agent that gets an unexpected result cannot ask where a value came from,
cannot turn the files off to rule them out, and cannot tell a typo in a config file from a
bug. treaty reads settings in one place, for every command, and reports on them:
`meta.config_sources` on every response, `--show-config` for the details, `--no-config` to
rule the files out, and an argument error for a key it does not know.

The audit reports the two ways around it. A handler that parses a TOML file itself:

```bash
$ uv run treaty audit myapp.cli:app --format plain
...
  1. (warning) settings-declared [import]: handler parses a config file with tomllib.loads, so meta.config_sources, --show-config, --no-config, and --config cannot see it (REQ-F-028)
     fix: declare the keys as a frozen dataclass with defaults, pass App(settings=Settings), and take settings: Settings in the handler
```

and a handler that reads a variable without the app's prefix, which an agent may have set
for another tool in the same session:

```bash
  1. (warning) env-prefix [import]: reads the unprefixed variable FEED_TOKEN, which an agent may set for another tool in the same session (REQ-F-073)
     fix: read TODO_FEED_TOKEN instead of FEED_TOKEN
```

Both are warnings, so both fail `--strict`.

## Step 1: Declare the settings

Settings are one frozen dataclass whose fields all have defaults, passed to the app:

<!-- file: examples/tutorial/todo_config.py -->
```python
@dataclass(frozen=True, slots=True)
class Settings:
    feed_url: str = ""
    """The list import reads when --url is not given; "" when there is none"""


# mkdir is the one other program a fix_command may run (REQ-C-030)
app = App(
    "todo",
    version="1.0.0",
    description="Track todo items",
    companions=("mkdir",),
    settings=Settings,
)
```

Every command now answers `--show-config`: the value each setting took, where it came from,
and the order the sources are read in.

**Check:** with nothing set, `feed_url` is its default, and the environment comes first in the
order

<!-- check -->
```bash
todo import --show-config | jq -e '.data.effective_config == {"feed_url": ""}
  and .data.sources == {"feed_url": "default"}
  and .data.precedence_order[0] == "env-vars" and .data.precedence_order[-1] == "defaults"'
```

## Step 2: Know where a value comes from

Each setting takes the first value it finds, in this order:

1. **The environment**: `TODO_FEED_URL`, the app's name in capitals, then the field's
2. **The project file**: `.todo.toml` in the working directory, or under `--cwd`
3. **The user file**: `todo/config.toml` under `XDG_CONFIG_HOME`, else `~/.config`
4. **The default** in the dataclass

`--config PATH` reads that one file instead of the project and user files, and `--no-config`
reads none; the environment applies either way. A file may also hold `[contexts.<name>]`
tables that override its top level, chosen with `--context NAME`, for settings that differ
between, say, a staging and a production feed.

**Check:** a project file sets the feed, the environment overrides it, and `--no-config`
drops the file

<!-- check -->
```bash
printf 'feed_url = "https://example.com/team.json"\n' > tmp/tutorial/project/.todo.toml
todo import --cwd tmp/tutorial/project --show-config \
  | jq -e '.data.effective_config.feed_url == "https://example.com/team.json"
    and (.data.sources.feed_url | startswith("file:"))
    and (.meta.config_sources | length) == 1'
TODO_FEED_URL=https://example.com/mine.json todo import --cwd tmp/tutorial/project --show-config \
  | jq -e '.data.effective_config.feed_url == "https://example.com/mine.json"
    and .data.sources.feed_url == "env:TODO_FEED_URL"'
todo import --cwd tmp/tutorial/project --no-config --show-config \
  | jq -e '.data.sources.feed_url == "default"'
```

## Step 3: Read settings in the handler

A handler asks for the settings by annotating a parameter with the class, as it does for a
resource:

<!-- file: examples/tutorial/todo_config.py -->
```python
def import_items(args: Import, ctx: Ctx, store: Store, settings: Settings) -> Imported:
    url = args.url or settings.feed_url
    if not url:
        raise Exit.PRECONDITION(
            "there is no feed to import from",
            fix_required="pass --url, or set feed_url in .todo.toml or TODO_FEED_URL",
        )
```

A flag the caller passed wins over a setting, so `--url` still picks another feed for one
call. When neither gives a value, the run exits 4 with `PRECONDITION`, and `fix_required`
names all three ways to supply one.

A setting the command cannot work without belongs in the settings only when it has a
sensible default or a clear message when missing, as here. A value every call needs is a
required flag.

**Check:** with no flag, no file, and no variable, `import` exits 4 and says how to supply
the feed

<!-- check -->
```bash
todo import --cwd tmp/tutorial --db todo.json | jq -e '.meta.exit_code == 4
  and .error.fix_required == "pass --url, or set feed_url in .todo.toml or TODO_FEED_URL"'
```

## Step 4: Fail fast on a bad file

A file with a key the settings do not have, a value of the wrong type, or broken TOML stops
every run with exit 2 and `CONFIG_INVALID`, naming the file and the key, and listing the keys
that exist. A typo in a config file is then a clear error on the next call, not a setting
that silently keeps its default.

**Check:** a misspelled key is refused, with the known keys listed

<!-- check -->
```bash
printf 'feed_urll = "https://example.com/team.json"\n' > tmp/tutorial/bad.toml
todo list --config tmp/tutorial/bad.toml | jq -e '.meta.exit_code == 2
  and .error.code == "CONFIG_INVALID" and .error.context.key == "feed_urll"
  and .error.context.known == ["feed_url"]'
```

## Step 5: Take secrets from the environment or a file

`import` now takes a token for feeds that require one:

<!-- file: examples/tutorial/todo_config.py -->
```python
    token: str | None = Flag(default=None, description="Bearer token the feed requires")
```

A field named with `token`, `secret`, `password`, `key`, `credential`, or `auth` in it is a
secret, and `secret=True` makes any other field one. A secret is never taken as a value on
the command line, where it would land in shell history, process listings, and the audit
log. The caller passes it one of three ways instead:

| The caller passes | treaty reads |
| --- | --- |
| nothing | `TODO_TOKEN`, the app's name and the field's |
| `--token-from-env FEED_TOKEN` | the variable the caller names |
| `--token-from-file /run/secrets/feed` | the file, without its trailing newline |

The value reaches the handler as the field, like any other. It never comes back out: an
error about a secret shows it as `[REDACTED]`, `--debug` redacts request headers, and
`--show-config` redacts a setting whose name marks it as secret. The manifest lists the
default variable in `secret_env_vars`, so an agent knows which variable to set.

**Check:** a token on the command line is refused with the two alternatives; a variable that
is not set is an argument error; a token from a file validates; the schema names
`TODO_TOKEN`

<!-- check -->
```bash
todo import --url https://example.com/todo.json --token s3cret \
  | jq -e '.meta.exit_code == 2 and .error.context.accepted == ["token-from-env", "token-from-file"]'
todo import --url https://example.com/todo.json --token-from-env FEED_TOKEN_UNSET \
  | jq -e '.meta.exit_code == 2 and .error.message == "Environment variable FEED_TOKEN_UNSET is not set."'
printf 's3cret\n' > tmp/tutorial/token
todo import --url https://example.com/todo.json --token-from-file tmp/tutorial/token --validate-only \
  | jq -e '.meta.exit_code == 0'
todo import --schema | jq -e '.data.secret_env_vars == ["TODO_TOKEN"]'
```

## Step 6: Test it against a feed that needs the token

`tests/test_tutorial.py` serves a feed that answers only a request carrying the right bearer
token, and checks the setting and the secret end to end: with `TODO_FEED_URL` and
`TODO_TOKEN` set, `import` adds the list, and the token appears nowhere in the envelope; with
the wrong token, the run exits 8 with `UNAUTHENTICATED`, because `import` declares
`AUTH_REQUIRED` and `ctx.http` maps a 401 to it; with no feed at all, it exits 4.

**Check:** the three tests pass

<!-- check -->
```bash
uv run pytest -q tests/test_tutorial.py -k "feed_setting or refused_token or without_a_feed"
```

Tokens that expire, logins, and scopes are the next step up from a static token:
`App(credentials=...)` gives commands a credential store, `check-permissions`, and exit codes
for expired and missing credentials. See [Credentials](../../../README.md#credentials) in the
README.

## Next

The audit's last rule, `profile`, asks for a conformance profile, which
[Run the conformance kit](../ship/conformance.md) writes and runs.
