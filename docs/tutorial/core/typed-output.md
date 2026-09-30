# Type every command's output

**Goal:** every command returns a typed value, so its `output_schema` tells an agent the
shape of `data` before the first call, and a test holds each handler to that shape

**You need:** a treaty app, such as `todo` at the end of [Declare exit codes](exit-codes.md),
whose example this chapter uses; this chapter clears the audit rule `typed-output`

**Done when:** the audit has no `typed-output` finding:

<!-- check -->
```bash
uv run treaty audit examples.tutorial.todo_exit_codes:app \
  | jq -e '[.data.rules[].findings[] | select(.rule == "typed-output")] == []'
```

The chapter uses `todo` as [Declare exit codes](exit-codes.md) left it,
[`examples/tutorial/todo_exit_codes.py`](../../../examples/tutorial/todo_exit_codes.py).

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

## What a return type gives an agent

treaty turns a handler's return annotation into a JSON Schema, the command's
`output_schema`. An agent reads it before it calls, so it knows which fields `data` will
have, their types, and which may be `null`, without running the command first and guessing
from one sample. The same schema reaches every place an agent looks:

- `todo <command> --output-schema` prints it alone, and `--schema` includes it
- the manifest carries it on every command, and MCP clients get it as the tool's
  `outputSchema`
- `--fields` and `--format tsv` use its field names, and `schema-lock` records it, so the
  audit can tell when a release changes it

A handler annotated `-> dict[str, object]` gives all of that up: its schema is "an object",
and the audit says so:

```bash
$ uv run treaty audit myapp.cli:app --format plain
...
  1. (advice) typed-output [stats]: return type is an untyped dict, so output_schema tells agents nothing
     fix: return a frozen dataclass; its fields become output_schema automatically
```

The finding is `advice`, so it does not fail `--strict`. It is still the difference between
an agent that knows what it will get and one that has to find out.

A list of such dicts, `-> list[dict[str, object]]` from `model_dump()`, is flagged too. It
also costs order: treaty sorts every array inside untyped content by its JSON text, so the
rule `stable-order` asks for a typed output or `ordered=True` there.

## Step 1: Return a dataclass

Every `todo` command already returns one. `add` returns `Changed`, which holds an `Item`,
and the schema follows the fields:

<!-- file: examples/tutorial/todo_exit_codes.py -->
```python
@dataclass(frozen=True, slots=True)
class Item:
    id: int
    text: str
    priority: Priority
    done: bool
```

Each annotation becomes a schema type:

| Annotation | In `output_schema` |
| --- | --- |
| `int`, `float`, `str`, `bool` | `integer`, `number`, `string`, `boolean` |
| `Literal["low", "high"]`, a `StrEnum` | `string` with `enum` |
| `X \| None` | `anyOf` X and `null` |
| `list[X]`, `tuple[X, ...]` | `array` of X |
| a nested dataclass | `object` with its own `properties` |
| `datetime` | `string` with `format: date-time` |
| `Decimal` | `string` with a fixed-point `pattern`, written such as `"12.30"` |
| `Path` | `string`; written absolute, joined to the working directory |

Every field of an output dataclass is written on every call, so the schema lists all of
them as `required` and allows no others. That is the point: an agent can read `data.item.id`
without checking whether the key is there.

Some types are refused when the app is built, each for a reason:

- **`Any`, a `TypedDict`, or a bare `str` or `int` as the whole result**: `data` must be a
  JSON object, an array, or `null`, and its shape must be declared
- **`list[X] | None`**: an empty collection is `[]`, never `null`, so a caller never has two
  ways to read "nothing". Write `tags: list[str] = Out(default_factory=list)`

`Out(...)` is to an output field what `Flag` is to an argument: it gives the field a default,
and settings for how treaty writes it, such as the order of a list (`sort_key=`) or whether
its content came from outside the tool (`external=True`), which later chapters use.

A `list[X]` or `tuple[X, ...]` of dataclasses needs its order declared. treaty sorts every
output array so `data` is the same for the same result, and without a declared order it
sorts objects by each item's JSON text: invoice lines with amounts `"5.00"` and `"10.00"`
come back `"10.00"` first. Declare `lines: list[Line] = Out(sort_key="line_no")` to sort by
a field, or `Out(ordered=True)` to keep the order the handler built, for a ranking or any
list whose order is the data. A command that returns the list itself takes the same
`sort_key=` or `ordered=True`. The audit rule `stable-order` reports an array of objects
with neither as a warning, so `treaty audit --strict` fails until it is declared. A list of
scalars, such as `list[str]`, needs nothing: strings and numbers sort by value. An array
nested in another list or a dict, such as `dict[str, list[Line]]`, cannot take a declared
order, so the rule warns there too: hold it in a dataclass field that declares one.

**Check:** `add`'s schema names every field of the result, and of the item inside it

<!-- check -->
```bash
todo add --output-schema | jq -e '.data.required == ["effect", "item"]
  and .data.properties.item.required == ["id", "text", "priority", "done"]
  and .data.properties.item.properties.priority.enum == ["low", "normal", "high"]'
```

## Step 2: Turn maps into lists of objects

A map whose keys are data, such as a count per priority, is typed as
`dict[str, int]`. treaty accepts it, but the schema can only say "an object of integers":
it cannot name keys that exist only at run time, so the audit reports it like an untyped
dict. Return a list of objects instead:

```python
@dataclass(frozen=True, slots=True)
class Count:
    priority: Priority
    count: int


@app.command("stats", ..., sort_key="priority")
def stats(args: StatsArgs, ctx: Ctx, store: Store) -> list[Count]: ...
```

Now the schema names both fields, `sort_key` gives the list a stable order, and the
framework's list features work on it: `--fields`, `--format tsv`, and pagination all act on
arrays of objects. `todo list` shows what that buys, with no code of its own:

**Check:** two items, listed as TSV with a header row from the dataclass, and narrowed to two
fields with `--fields`

<!-- check -->
```bash
todo add "Buy milk" --priority high --db tmp/tutorial/todo.json > /dev/null
todo add "Walk dog" --db tmp/tutorial/todo.json > /dev/null
diff <(todo list --db tmp/tutorial/todo.json --format tsv) - <<'EOF'
id	text	priority	done
1	Buy milk	high	false
2	Walk dog	normal	false
EOF
todo list --db tmp/tutorial/todo.json --fields text,priority \
  | jq -e '.data == [{"text": "Buy milk", "priority": "high"}, {"text": "Walk dog", "priority": "normal"}]'
```

## Step 3: Test that handlers keep the promise

The annotation is a promise that treaty publishes but does not check. A handler annotated
`-> Changed` that returns a plain dict, or an `Item` whose `id` is the string `"1"`, exits 0
and ships exactly what it returned: the envelope is written from the value, not the
annotation. The agent that trusted the schema is the one that breaks.

Two checks keep the promise. `mypy --strict` (`uv add --dev mypy`, then
`uv run mypy --strict src tests`) catches most of it before anything runs: a
dict where a `Changed` belongs, or `str` passed where `Item.id` is `int`. A test catches the
rest by running each command and validating `data` against the schema the manifest
publishes:

<!-- file: tests/test_tutorial.py -->
```python
def test_every_result_matches_its_output_schema(tmp_path: Path) -> None:
    """treaty does not check a handler's return value against its annotation; this does"""
    app = todo_exit_codes.app
    commands = app.manifest()["commands"]
    assert isinstance(commands, dict)
    db = str(tmp_path / "todo.json")
    calls: list[tuple[str, dict[str, object]]] = [
        ("add", {"text": "Buy milk", "priority": "high", "db": db}),
        ("done", {"id": 1, "db": db}),
        ("list", {"all": True, "db": db}),
        ("purge", {"dry_run": True, "db": db}),
        ("purge", {"confirm_destructive": True, "db": db}),
    ]
    for name, args in calls:
        env = app.call(name, args, env={"TODO_AUDIT_LOG": "0"})
        assert env.ok, env.error
        jsonschema.validate(env.data, commands[name]["output_schema"])
```

It needs `jsonschema` as a test dependency, and its type stubs for `mypy --strict tests`:
`uv add --dev jsonschema types-jsonschema`. If mypy then says your package is "installed,
but missing library stubs or py.typed marker", add an empty `py.typed` beside your package's
`__init__.py` (`src/todo/py.typed` in a src layout). Cover each shape a command can return:
`purge` returns a preview with `would_affect` on a dry run and the deleted items on a real
one, so it is called both ways.

In your own project the test is the same with your app in it: import it (`from todo.cli
import app` in a project `treaty init` made), use it where the test says
`todo_exit_codes.app`, and list one call per command with arguments that work against a
scratch directory, as `calls` does above. Keep the `env=` on each call, with your app's own
variable, `<APP>_AUDIT_LOG` (`TODO_AUDIT_LOG` for `todo`), set to `0`, so the test never
writes your real audit log.

**Check:** the test passes for `todo`

<!-- check -->
```bash
uv run pytest -q tests/test_tutorial.py -k test_every_result_matches_its_output_schema
```

## Next

The audit's next rule is `paginated-list`, for commands that return a list:
[Page long lists](pagination.md).
