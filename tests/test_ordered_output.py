"""``ordered=True`` on a command keeps the handler's order of every array in its output,
whatever the return type holds (#329)"""

import io
import json
from dataclasses import dataclass
from typing import Any

import pytest
from jsonschema import Draft7Validator
from pydantic import BaseModel, Field

from treaty import App, Ctx, NoArgs, Out, RegistrationError
from treaty._audit import audit


def run(app: App, argv: list[str]) -> dict:
    out = io.StringIO()
    code = app.run(argv, stdin=io.StringIO(), stdout=out, stderr=io.StringIO(), env={})
    assert code == 0, out.getvalue()
    return json.loads(out.getvalue())


def adapted() -> App:
    app = App("demo", version="0.1.0")
    app.output_adapter(
        BaseModel,
        schema=lambda cls: cls.model_json_schema(mode="serialization"),
        dump=lambda obj: obj.model_dump(mode="json", by_alias=True),
    )
    return app


def stable_order(app: App) -> list:
    report = audit(app, "x:app", limit=100)
    return next(list(r.findings) for r in report.rules if r.id == "stable-order")


def array_nodes(node: object) -> list[dict]:
    """Every array node of a schema, at any depth"""
    found: list[dict] = []
    if isinstance(node, dict):
        if node.get("type") == "array":
            found.append(node)
        for value in node.values():
            found += array_nodes(value)
    elif isinstance(node, list):
        for value in node:
            found += array_nodes(value)
    return found


class Step(BaseModel):
    name: str
    tags: list[str]


class Run(BaseModel):
    steps: list[int]
    trail: list[Step]
    grid: list[list[int]]
    lines: list[Step] = Field(json_schema_extra={"x-sort-key": "name"})
    seen: str = Field(json_schema_extra={"x-volatile": True})


def a_run() -> Run:
    steps = [Step(name="b", tags=["z", "a"]), Step(name="a", tags=["y", "b"])]
    return Run(steps=[3, 1, 2], trail=steps, grid=[[2, 1], [0]], lines=steps, seen="now")


@dataclass(frozen=True, slots=True)
class Leg:
    name: str
    stops: list[str]


@dataclass(frozen=True, slots=True)
class Trip:
    legs: list[Leg]
    by_day: dict[str, list[int]]
    run: Run
    keyed: list[Leg] = Out(sort_key="name")


def a_trip() -> Trip:
    legs = [Leg("y", ["c", "a"]), Leg("x", ["b"])]
    return Trip(legs=legs, by_day={"mon": [2, 1]}, run=a_run(), keyed=legs)


def ordered_app(ordered: bool) -> App:
    app = adapted()
    declare: dict[str, Any] = {"ordered": True} if ordered else {}

    @app.command("run", description="Run", danger_level="safe", exit_codes=(), **declare)
    def run_(args: NoArgs, ctx: Ctx) -> Run:
        return a_run()

    @app.command("runs", description="Runs", danger_level="safe", exit_codes=(), **declare)
    def runs(args: NoArgs, ctx: Ctx) -> list[Run]:
        return [a_run(), a_run().model_copy(update={"steps": [9, 0]})]

    @app.command("trip", description="Trip", danger_level="safe", exit_codes=(), **declare)
    def trip(args: NoArgs, ctx: Ctx) -> Trip:
        return a_trip()

    @app.command("nested", description="Nested", danger_level="safe", exit_codes=(), **declare)
    def nested(args: NoArgs, ctx: Ctx) -> dict[str, list[Run]]:
        return {"x": [a_run(), a_run().model_copy(update={"steps": [9, 0]})]}

    return app


def test_the_issue_example_registers_and_keeps_the_order() -> None:
    data = run(ordered_app(True), ["run"])["data"]
    assert data["steps"] == [3, 1, 2]
    assert [s["name"] for s in data["trail"]] == ["b", "a"]
    assert data["trail"][0]["tags"] == ["z", "a"]
    assert data["grid"] == [[2, 1], [0]]
    # A property's x-sort-key is the finer-grained declaration, and still orders its array
    assert [s["name"] for s in data["lines"]] == ["a", "b"]


def test_without_ordered_every_undeclared_array_is_still_sorted() -> None:
    data = run(ordered_app(False), ["run"])["data"]
    assert data["steps"] == [1, 2, 3]
    assert [s["name"] for s in data["trail"]] == ["a", "b"]
    assert data["trail"][0]["tags"] == ["b", "y"]
    assert data["grid"] == [[0], [1, 2]]


def test_a_list_of_models_keeps_the_list_and_every_array_inside() -> None:
    data = run(ordered_app(True), ["runs"])["data"]
    assert [r["steps"] for r in data] == [[3, 1, 2], [9, 0]]
    sorted_data = run(ordered_app(False), ["runs"])["data"]
    assert [r["steps"] for r in sorted_data] == [[0, 9], [1, 2, 3]]


def test_a_dataclass_and_a_model_in_a_native_container_keep_their_arrays() -> None:
    trip = run(ordered_app(True), ["trip"])["data"]
    assert [leg["stops"] for leg in trip["legs"]] == [["c", "a"], ["b"]]
    assert trip["by_day"] == {"mon": [2, 1]}
    assert trip["run"]["steps"] == [3, 1, 2]
    assert [leg["name"] for leg in trip["keyed"]] == ["x", "y"]  # Out(sort_key=) still sorts
    nested = run(ordered_app(True), ["nested"])["data"]
    assert [r["steps"] for r in nested["x"]] == [[3, 1, 2], [9, 0]]
    sorted_trip = run(ordered_app(False), ["trip"])["data"]
    assert [leg["stops"] for leg in sorted_trip["legs"]] == [["b"], ["a", "c"]]
    assert sorted_trip["by_day"] == {"mon": [1, 2]}


def test_stable_output_still_drops_volatile_fields() -> None:
    data = run(ordered_app(True), ["run", "--stable-output"])["data"]
    assert "seen" not in data and data["steps"] == [3, 1, 2]


@pytest.mark.parametrize("command", ["run", "runs", "trip", "nested"])
def test_the_schema_marks_every_array_it_keeps(command: str) -> None:
    app = ordered_app(True)
    schema = run(app, [command, "--output-schema"])["data"]
    assert schema["x-ordered"] is True
    arrays = array_nodes(schema)
    assert arrays and all(
        a.get("x-ordered") is True or a.get("x-sort-key") is not None for a in arrays
    )
    Draft7Validator.check_schema(schema)
    Draft7Validator(schema).validate(run(app, [command])["data"])
    default = run(ordered_app(False), [command, "--output-schema"])["data"]
    assert "x-ordered" not in default
    assert not [a for a in array_nodes(default) if a.get("x-ordered")]


def test_the_audit_asks_nothing_of_an_ordered_command() -> None:
    assert stable_order(ordered_app(True)) == []
    unordered = stable_order(ordered_app(False))
    # The advice for a model's array names the command-level route beside x-ordered
    on_models = [f for f in unordered if "json_schema_extra" in f.fix]
    assert on_models and all("ordered=True on the command" in f.fix for f in on_models)


def test_out_ordered_on_a_field_that_is_not_an_array_names_x_ordered() -> None:
    @dataclass(frozen=True, slots=True)
    class Wrap:
        run: Run = Out(ordered=True)

    app = adapted()
    with pytest.raises(RegistrationError, match=r'json_schema_extra=\{"x-ordered": True\}'):

        @app.command("w", description="W", danger_level="safe", exit_codes=())
        def w(args: NoArgs, ctx: Ctx) -> Wrap:
            return Wrap(a_run())
