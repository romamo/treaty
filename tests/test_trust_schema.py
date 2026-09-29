"""Every shape an external command's data takes validates against the schema an MCP client
checks it with: the trust tags are listed wherever the runtime puts them"""

from dataclasses import dataclass
from pathlib import Path

import jsonschema
import pytest

from treaty import App, Ctx, NoArgs, Out
from treaty._tools import output_schema
from treaty._values import CommandPath


@dataclass(frozen=True, slots=True)
class Item:
    text: str = Out(external=True)


@dataclass(frozen=True, slots=True)
class Outer:
    item: Item


def _checked(app: App, name: str, arguments: dict[str, object] | None = None) -> None:
    envelope = app.call(name, arguments or {}, env={})
    assert envelope.ok, envelope.error
    body = envelope.to_json()
    assert isinstance(body["data"], dict) and body["data"]["_trusted"] is False
    command = app.commands[CommandPath(name)]
    jsonschema.validate(body, output_schema(command))
    jsonschema.validate(body["data"], app.manifest()["commands"][name]["output_schema"])  # type: ignore[index]


def test_an_optional_output_with_an_external_field_validates() -> None:
    app = App("x", version="1.0.0")

    @app.command("opt", description="Opt", danger_level="safe", exit_codes=())
    def opt(args: NoArgs, ctx: Ctx) -> Outer | None:
        return Outer(Item("from the web"))

    _checked(app, "opt")


def test_a_dict_output_of_external_items_validates() -> None:
    app = App("x", version="1.0.0")

    @app.command("map", description="Map", danger_level="safe", exit_codes=())
    def by_key(args: NoArgs, ctx: Ctx) -> dict[str, Item]:
        return {"a": Item("from the web")}

    _checked(app, "map")


def test_a_compat_shape_validates_against_the_mcp_output_schema() -> None:
    """schema_version picks a shim's older shape, which MCP must accept too"""

    @dataclass(frozen=True, slots=True)
    class Old:
        body: str = Out(external=True)

    def to_v1(new: Outer) -> Old:
        return Old(new.item.text)

    app = App("x", version="1.0.0")

    @app.command(
        "shim",
        description="Shim",
        danger_level="safe",
        exit_codes=(),
        schema_version="2.0",
        compat={"1.0": to_v1},
    )
    def shim(args: NoArgs, ctx: Ctx) -> Outer:
        return Outer(Item("from the web"))

    envelope = app.call("shim", {"schema_version": "1"}, env={})
    assert envelope.ok, envelope.error
    jsonschema.validate(envelope.to_json(), output_schema(app.commands[CommandPath("shim")]))


@pytest.mark.parametrize("name", ["opt", "map"])
def test_the_published_schema_lists_the_tags(name: str) -> None:
    app = App("x", version="1.0.0")

    @app.command("opt", description="Opt", danger_level="safe", exit_codes=())
    def opt(args: NoArgs, ctx: Ctx) -> Outer | None:
        return None

    @app.command("map", description="Map", danger_level="safe", exit_codes=())
    def by_key(args: NoArgs, ctx: Ctx) -> dict[str, Item]:
        return {}

    assert "_trusted" in str(app.manifest()["commands"][name]["output_schema"])  # type: ignore[index]


def test_a_replayed_call_in_a_compat_shape_validates_over_mcp(tmp_path: Path) -> None:
    from typing import Literal

    @dataclass(frozen=True, slots=True)
    class New:
        effect: Literal["created"]
        title: str

    @dataclass(frozen=True, slots=True)
    class Old:
        effect: Literal["created"]
        name: str

    def to_v1(new: New) -> Old:
        return Old(new.effect, new.title)

    app = App("x", version="1.0.0")

    @app.command(
        "make",
        description="Make",
        danger_level="mutating",
        exit_codes=(),
        schema_version="2.0",
        compat={"1.0": to_v1},
    )
    def make(args: NoArgs, ctx: Ctx) -> New:
        return New("created", "x")

    call = {"schema_version": "1", "idempotency_key": "k1"}
    schema = output_schema(app.commands[CommandPath("make")])
    for _ in range(2):
        envelope = app.call("make", call, env={"X_STATE_DIR": str(tmp_path)})
        assert envelope.ok, envelope.error
        jsonschema.validate(envelope.to_json(), schema)


def test_a_fixed_length_tuple_output_registers() -> None:
    app = App("x", version="1.0.0")

    @app.command("pair", description="Pair", danger_level="safe", exit_codes=())
    def pair(args: NoArgs, ctx: Ctx) -> tuple[Item, Item]:
        return (Item("a"), Item("b"))

    assert app.call("pair", {}, env={}).ok
