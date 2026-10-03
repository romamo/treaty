"""#298: an output type that holds itself, such as a tree node, has a schema: it is inlined
where it first appears, each place it holds itself is a ``$ref`` to its ``$defs`` entry, and
every walk of the schema or the value follows the reference"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from jsonschema import Draft7Validator, ValidationError
from pydantic import BaseModel, Field

from treaty import App, Arg, Batch, Ctx, NoArgs, Out, RegistrationError
from treaty import Item as BatchItem
from treaty._audit import Change, audit, schema_change, schema_lock
from treaty._mcp import call_tool, tool_entries
from treaty._refs import merge_defs
from treaty._schema import MAX_OUTPUT_DEPTH


def run(app: App, argv: list[str]) -> tuple[int, dict]:
    out = io.StringIO()
    code = app.run(argv, stdin=io.StringIO(), stdout=out, stderr=io.StringIO(), env={})
    return code, json.loads(out.getvalue())


def output_schema(app: App, command: str) -> dict:
    code, env = run(app, [command, "--output-schema"])
    assert code == 0, env
    schema = env["data"]
    Draft7Validator.check_schema(schema)
    return schema


@dataclass(frozen=True, slots=True)
class Node:
    path: str
    children: list[Node] = Out(default_factory=list, ordered=True)
    token: str = Out(default="", high_entropy=True)


def tree() -> Node:
    return Node(
        "/",
        [Node("/b", [Node("/b/c", token="sk_live_deep0123456789")]), Node("/a")],
        token="sk_live_root0123456789",
    )


def tree_app() -> App:
    app = App("trees", version="1.0.0")

    @app.command("tree", description="Show the tree", danger_level="safe", exit_codes=())
    def show(args: NoArgs, ctx: Ctx) -> Node:
        return tree()

    return app


def test_the_issue_example_registers_with_a_ref_to_its_definition() -> None:
    schema = output_schema(tree_app(), "tree")
    definition = schema["$defs"]["Node"]
    assert schema["title"] == "Node" and definition["title"] == "Node"
    assert schema["properties"]["children"] == {
        "type": "array",
        "items": {"$ref": "#/$defs/Node"},
        "x-ordered": True,
    }
    assert definition["properties"] == schema["properties"]
    assert list(schema["$defs"]) == ["Node"]


def test_the_data_validates_and_a_deep_mistake_is_caught() -> None:
    app = tree_app()
    schema = output_schema(app, "tree")
    code, env = run(app, ["tree", "--unmask"])
    assert code == 0
    data = env["data"]
    assert [c["path"] for c in data["children"]] == ["/b", "/a"]  # ordered=True kept
    Draft7Validator(schema).validate(data)
    data["children"][0]["children"][0]["extra"] = 1
    with pytest.raises(ValidationError):
        Draft7Validator(schema).validate(data)


def test_a_secret_is_masked_at_every_depth() -> None:
    _, env = run(tree_app(), ["tree"])
    deep = env["data"]["children"][0]["children"][0]
    assert "sk_live_deep0123456789" not in json.dumps(env)
    assert deep["token"].startswith("[KEY: ")


def test_plain_and_fields_serve_a_tree() -> None:
    app = tree_app()
    out = io.StringIO()
    app.run(["tree", "--format", "plain"], stdin=io.StringIO(), stdout=out, stderr=io.StringIO())
    assert "children.0.children.0.path: /b/c" in out.getvalue()
    _, picked = run(app, ["tree", "--fields", "path"])
    assert picked["data"] == {"path": "/"}


@dataclass(frozen=True, slots=True)
class Dept:
    name: str
    head: Person | None = None
    teams: tuple[Dept, ...] = ()


@dataclass(frozen=True, slots=True)
class Person:
    name: str
    dept: Dept | None = None
    reports: dict[str, Person] = Out(default_factory=dict)


def test_mutual_recursion_through_optional_tuple_and_dict_values() -> None:
    app = App("org", version="1.0.0")

    @app.command("org", description="Org chart", danger_level="safe", exit_codes=())
    def org(args: NoArgs, ctx: Ctx) -> Dept:
        lead = Person("ada", Dept("inner"), {"bob": Person("bob")})
        return Dept("root", lead, (Dept("sub", Person("cy")),))

    schema = output_schema(app, "org")
    assert sorted(schema["$defs"]) == ["Dept", "Person"]
    person = schema["properties"]["head"]["anyOf"][0]
    assert person["properties"]["dept"]["anyOf"][0] == {"$ref": "#/$defs/Dept"}
    assert person["properties"]["reports"]["additionalProperties"] == {"$ref": "#/$defs/Person"}
    assert schema["properties"]["teams"]["items"] == {"$ref": "#/$defs/Dept"}
    code, env = run(app, ["org"])
    assert code == 0
    Draft7Validator(schema).validate(env["data"])
    assert env["data"]["head"]["reports"]["bob"]["name"] == "bob"


class Other:
    @dataclass(frozen=True, slots=True)
    class Node:
        size: int
        parts: list[Other.Node] = Out(default_factory=list, ordered=True)


OtherNode = Other.Node


@dataclass(frozen=True, slots=True)
class Both:
    tree: Node
    other: Other.Node


def test_two_classes_of_one_name_get_two_definitions() -> None:
    app = App("both", version="1.0.0")

    @app.command("both", description="Both trees", danger_level="safe", exit_codes=())
    def both(args: NoArgs, ctx: Ctx) -> Both:
        return Both(Node("/", [Node("/a")]), OtherNode(1, [OtherNode(2)]))

    schema = output_schema(app, "both")
    assert sorted(schema["$defs"]) == ["Node", "Node_2"]
    assert schema["$defs"]["Node_2"]["properties"]["parts"]["items"] == {"$ref": "#/$defs/Node_2"}
    _, env = run(app, ["both"])
    Draft7Validator(schema).validate(env["data"])
    env["data"]["other"]["parts"][0]["size"] = "big"
    with pytest.raises(ValidationError):
        Draft7Validator(schema).validate(env["data"])


class Folder(BaseModel):
    name: str
    api_token: str = ""
    children: list[Folder] = Field(default_factory=list, json_schema_extra={"x-ordered": True})


class Loose(BaseModel):
    name: str
    kids: list[Loose] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Folder2:
    """A dataclass holding itself and a model that holds itself"""

    label: str
    model: Folder
    kids: list[Folder2] = Out(default_factory=list, ordered=True)


def model_app() -> App:
    app = App("models", version="1.0.0")
    app.output_adapter(
        BaseModel,
        schema=lambda cls: cls.model_json_schema(mode="serialization"),
        dump=lambda obj: obj.model_dump(mode="json", by_alias=True),
    )

    def folder() -> Folder:
        return Folder(
            name="root",
            api_token="sk_live_root0123456789",
            children=[
                Folder(name="z", children=[Folder(name="y", api_token="sk_live_deep0123456789")]),
                Folder(name="a"),
            ],
        )

    @app.command("folder", description="Folder", danger_level="safe", exit_codes=())
    def show(args: NoArgs, ctx: Ctx) -> Folder:
        return folder()

    @app.command("mixed", description="Mixed", danger_level="safe", exit_codes=())
    def mixed(args: NoArgs, ctx: Ctx) -> Folder2:
        return Folder2("top", folder(), [Folder2("kid", Folder(name="k"))])

    @app.command("loose", description="Loose", danger_level="safe", exit_codes=())
    def loose(args: NoArgs, ctx: Ctx) -> Loose:
        return Loose(name="r", kids=[Loose(name="z"), Loose(name="a")])

    return app


def test_a_model_that_holds_itself_keeps_its_definition_as_a_dataclass_does() -> None:
    app = model_app()
    schema = output_schema(app, "folder")
    assert list(schema["$defs"]) == ["Folder"]
    assert schema["properties"]["children"]["items"] == {"$ref": "#/$defs/Folder"}
    # Normalized as an inlined model is: every key required, no other key allowed
    assert schema["$defs"]["Folder"]["additionalProperties"] is False
    assert schema["$defs"]["Folder"]["properties"]["api_token"]["x-high-entropy"] is True
    _, env = run(app, ["folder"])
    data = env["data"]
    assert [c["name"] for c in data["children"]] == ["z", "a"]
    assert "sk_live_deep0123456789" not in json.dumps(env)
    Draft7Validator(schema).validate(run(app, ["folder", "--unmask"])[1]["data"])
    mixed = output_schema(app, "mixed")
    assert sorted(mixed["$defs"]) == ["Folder", "Folder2"]
    Draft7Validator(mixed).validate(run(app, ["mixed", "--unmask"])[1]["data"])


def test_an_unordered_array_of_a_recursive_model_is_flagged() -> None:
    report = audit(model_app(), "x:app", limit=100)
    found = [f for r in report.rules for f in r.findings if f.rule == "stable-order"]
    assert [f.command for f in found] == ["loose"]
    assert "kids of Loose" in found[0].message


def test_mcp_lists_the_definitions_at_the_root_of_the_output_schema() -> None:
    app = tree_app()
    entries = {e.name: e for e in tool_entries(app)}
    schema = entries["tree"].output_schema  # type: ignore[attr-defined]
    assert list(schema["$defs"]) == ["Node"]
    Draft7Validator.check_schema(schema)
    envelope = call_tool(app, entries, "tree", {})
    assert envelope.ok
    _, env = run(app, ["tree", "--unmask"])
    Draft7Validator(schema).validate(env)


def test_schema_lock_diffs_a_definition_field_by_field() -> None:
    lock = schema_lock(tree_app())
    assert json.dumps(lock, sort_keys=True) == json.dumps(schema_lock(tree_app()), sort_keys=True)
    old = output_schema(tree_app(), "tree")
    added = json.loads(json.dumps(old))
    added["$defs"]["Node"]["properties"]["size"] = {"type": "integer"}
    assert schema_change(old, added) is Change.ADDITIVE
    removed = json.loads(json.dumps(old))
    del removed["$defs"]["Node"]["properties"]["token"]
    assert schema_change(old, removed) is Change.BREAKING
    assert schema_change(old, old) is Change.NONE


def test_merging_definitions_renames_a_clash() -> None:
    first = {"items": {"$ref": "#/$defs/N"}, "$defs": {"N": {"title": "one"}}}
    second = {"items": {"$ref": "#/$defs/N"}, "$defs": {"N": {"title": "two"}}}
    same = {"items": {"$ref": "#/$defs/N"}, "$defs": {"N": {"title": "one"}}}
    bodies, defs = merge_defs([first, second, same])
    assert defs == {"N": {"title": "one"}, "N_2": {"title": "two"}}
    assert [b["items"]["$ref"] for b in bodies] == ["#/$defs/N", "#/$defs/N_2", "#/$defs/N"]


def test_a_tree_deeper_than_the_limit_or_holding_itself_is_refused_whole() -> None:
    app = App("bad", version="1.0.0")

    @app.command("deep", description="Deep", danger_level="safe", exit_codes=())
    def deep(args: NoArgs, ctx: Ctx) -> Node:
        node = Node("leaf")
        for i in range(MAX_OUTPUT_DEPTH):
            node = Node(str(i), [node])
        return node

    @app.command("loop", description="Loop", danger_level="safe", exit_codes=())
    def loop(args: NoArgs, ctx: Ctx) -> Node:
        children: list[Node] = []
        node = Node("/", children)
        children.append(node)
        return node

    code, env = run(app, ["deep"])
    assert code == 1 and env["data"] is None
    assert env["error"]["code"] == "INVALID_OUTPUT"
    assert f"more than {MAX_OUTPUT_DEPTH}" in env["error"]["message"]
    code, env = run(app, ["loop"])
    assert code == 1 and env["data"] is None
    assert env["error"]["code"] == "INVALID_OUTPUT"
    assert "contains itself" in env["error"]["message"]


def test_a_shared_child_is_not_a_cycle() -> None:
    app = App("shared", version="1.0.0")

    @app.command("shared", description="Shared", danger_level="safe", exit_codes=())
    def shared(args: NoArgs, ctx: Ctx) -> Node:
        leaf = Node("/leaf")
        return Node("/", [leaf, Node("/x", [leaf])])

    code, env = run(app, ["shared"])
    assert code == 0 and env["data"]["children"][1]["children"][0]["path"] == "/leaf"


@dataclass(frozen=True, slots=True)
class Filter:
    field: str
    sub: Filter | None = None


@dataclass(frozen=True, slots=True)
class Query:
    where: Filter = Arg(description="Filter")


def test_a_recursive_argument_type_is_still_refused() -> None:
    app = App("args", version="1.0.0")
    with pytest.raises(RegistrationError, match="Filter contains itself"):

        @app.command("find", description="Find", danger_level="safe", exit_codes=())
        def find(args: Query, ctx: Ctx) -> Node:
            return Node("/")


@dataclass(frozen=True, slots=True)
class Made:
    effect: str
    tree: Node


@dataclass(frozen=True, slots=True)
class FlatTree:
    paths: list[str]


def flatten(node: Node) -> FlatTree:
    return FlatTree([node.path])


def test_a_batch_and_a_compat_shape_keep_the_definitions_at_the_root() -> None:
    app = App("wrapped", version="1.0.0")

    @app.command("make", description="Make", danger_level="mutating", exit_codes=())
    def make(args: NoArgs, ctx: Ctx) -> Batch[Made]:
        return Batch([BatchItem(1, Made("created", tree()))])

    @app.command(
        "show",
        description="Show",
        danger_level="safe",
        exit_codes=(),
        schema_version="2.0",
        compat={"1.0": flatten},
    )
    def show(args: NoArgs, ctx: Ctx) -> Node:
        return tree()

    schema = output_schema(app, "make")
    assert list(schema["$defs"]) == ["Node"]
    assert '"$defs":' not in json.dumps({k: v for k, v in schema.items() if k != "$defs"})
    code, env = run(app, ["make", "--unmask"])
    assert code == 0, env
    Draft7Validator(schema).validate(env["data"])
    entries = {e.name: e for e in tool_entries(app)}
    envelope_schema = entries["show"].output_schema  # type: ignore[attr-defined]
    assert list(envelope_schema["$defs"]) == ["Node"]
    Draft7Validator(envelope_schema).validate(run(app, ["show", "--unmask"])[1])
    Draft7Validator(envelope_schema).validate(run(app, ["show", "--schema-version", "1"])[1])


def test_a_streaming_command_masks_a_deep_secret_and_refuses_a_too_deep_event() -> None:
    app = App("streams", version="1.0.0")

    @app.command("walk", description="Walk", danger_level="safe", exit_codes=(), streaming=True)
    def walk(args: NoArgs, ctx: Ctx) -> Iterator[Node]:
        yield tree()
        yield tree()

    @app.command("deep", description="Deep", danger_level="safe", exit_codes=(), streaming=True)
    def deep(args: NoArgs, ctx: Ctx) -> Iterator[Node]:
        node = Node("leaf")
        for i in range(MAX_OUTPUT_DEPTH):
            node = Node(str(i), [node])
        yield node

    for argv in (["walk"], ["walk", "--format", "plain"]):
        out = io.StringIO()
        code = app.run(argv, stdin=io.StringIO(), stdout=out, stderr=io.StringIO(), env={})
        assert code == 0 and "sk_live_deep0123456789" not in out.getvalue()
    entries = {e.name: e for e in tool_entries(app)}
    schema = entries["walk"].output_schema  # type: ignore[attr-defined]
    assert list(schema["$defs"]) == ["Node"]
    envelope = call_tool(app, entries, "walk", {}).to_json()
    assert "sk_live_deep0123456789" not in json.dumps(envelope)
    Draft7Validator(schema).validate(envelope)
    code, env = run(app, ["deep"])
    assert code == 1 and env["error"]["code"] == "INVALID_OUTPUT"
    assert f"more than {MAX_OUTPUT_DEPTH}" in env["error"]["message"]
