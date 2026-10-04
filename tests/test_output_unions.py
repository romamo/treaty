"""A command returning one of several output types (#328): a union whose members a tag
field or their own required keys tell apart is published as ``oneOf``; an untagged one is
refused at registration"""

import io
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

import pytest
from jsonschema import Draft7Validator
from pydantic import BaseModel

from treaty import App, Arg, CommandPath, Ctx, NoArgs, Out, RegistrationError
from treaty._audit import Severity, audit
from treaty._mcp import tool_entries
from treaty._tools import tool_fields


def run(app: App, argv: list[str]) -> tuple[int, dict]:
    out = io.StringIO()
    code = app.run(argv, stdin=io.StringIO(), stdout=out, stderr=io.StringIO(), env={})
    return code, json.loads(out.getvalue())


def plain(app: App, argv: list[str]) -> str:
    out = io.StringIO()
    app.run([*argv, "--format", "plain"], stdin=io.StringIO(), stdout=out, stderr=io.StringIO())
    return out.getvalue()


def adapted() -> App:
    app = App("probe", version="0.1.0")
    app.output_adapter(
        BaseModel,
        schema=lambda cls: cls.model_json_schema(mode="serialization"),
        dump=lambda obj: obj.model_dump(mode="json", by_alias=True),
    )
    return app


class Market(BaseModel):
    condition_id: str


class Event(BaseModel):
    slug: str


@dataclass(frozen=True, slots=True)
class Card:
    kind: Literal["card"]
    last4: str
    token: str
    seen_at: str = Out(volatile=True, default="2026-10-04T00:00:00Z")


@dataclass(frozen=True, slots=True)
class Transfer:
    kind: Literal["transfer"]
    iban: str
    token: str = Out(high_entropy=False)


TOKEN = "Zq8xV3mK7pL2nR9tW4yB6cD1fH5jA0sE"
"""High entropy, under a name that says credential: masked unless declared otherwise"""


@dataclass(frozen=True, slots=True)
class Pick:
    ident: str = Arg(description="What to show")


def test_the_issue_example_registers_and_writes_the_member_returned() -> None:
    app = adapted()

    @app.command("market", description="Show", danger_level="safe", exit_codes=())
    def market(args: Pick, ctx: Ctx) -> Market | Event:
        if args.ident.startswith("0x"):
            return Market(condition_id=args.ident)
        return Event(slug=args.ident)

    code, env = run(app, ["market", "0x1"])
    assert code == 0 and env["data"] == {"condition_id": "0x1"}
    code, env = run(app, ["market", "election"])
    assert code == 0 and env["data"] == {"slug": "election"}
    schema = app.commands[CommandPath("market")].output_schema
    assert [b["title"] for b in schema["oneOf"]] == ["Market", "Event"]
    validator = Draft7Validator(schema)
    validator.validate({"condition_id": "0x1"})
    validator.validate({"slug": "election"})
    assert not validator.is_valid({"condition_id": "0x1", "slug": "election"})


def tagged_app() -> App:
    app = App("probe", version="0.1.0")

    @app.command("pay", description="Show", danger_level="safe", exit_codes=())
    def pay(args: Pick, ctx: Ctx) -> Card | Transfer | None:
        if args.ident == "none":
            return None
        if args.ident == "card":
            return Card("card", "4242", TOKEN)
        return Transfer("transfer", "DE89", TOKEN)

    @app.command("all", description="List", danger_level="safe", exit_codes=(), sort_key="token")
    def all_(args: NoArgs, ctx: Ctx) -> list[Card | Transfer]:
        return [Card("card", "4242", "b"), Transfer("transfer", "DE89", "a")]

    return app


def test_a_literal_tag_publishes_one_of_with_null_beside_it() -> None:
    schema = tagged_app().commands[CommandPath("pay")].output_schema
    union, null = schema["anyOf"]
    assert null == {"type": "null"}
    card, transfer = union["oneOf"]
    assert card["properties"]["kind"] == {"type": "string", "enum": ["card"]}
    assert transfer["properties"]["kind"] == {"type": "string", "enum": ["transfer"]}
    assert card["required"] == ["kind", "last4", "token"]  # seen_at is volatile


def test_each_member_keeps_its_own_output_rules() -> None:
    app = tagged_app()
    _, env = run(app, ["pay", "card"])
    assert env["data"]["token"] != TOKEN  # masked by name
    assert env["data"]["seen_at"] == "2026-10-04T00:00:00Z"
    _, env = run(app, ["pay", "card", "--stable-output"])
    assert "seen_at" not in env["data"]
    _, env = run(app, ["pay", "transfer"])
    assert env["data"]["token"] == TOKEN  # high_entropy=False
    _, env = run(app, ["pay", "none"])
    assert env["data"] is None
    _, env = run(app, ["pay", "card", "--fields", "kind,last4"])
    assert env["data"] == {"kind": "card", "last4": "4242"}


def test_a_list_of_a_union_is_sorted_and_rendered_as_one_table() -> None:
    app = tagged_app()
    code, env = run(app, ["all"])
    assert code == 0
    assert [d["kind"] for d in env["data"]] == ["transfer", "card"]
    items = app.commands[CommandPath("all")].output_schema["items"]
    assert [b["title"] for b in items["oneOf"]] == ["Card", "Transfer"]
    lines = plain(app, ["all"]).splitlines()
    assert lines[0].split() == ["kind", "last4", "token", "seen_at", "iban"]


def test_a_value_of_no_member_fails_the_run() -> None:
    app = App("probe", version="0.1.0")

    @app.command("pay", description="Show", danger_level="safe", exit_codes=())
    def pay(args: NoArgs, ctx: Ctx) -> Card | Transfer:
        return Pick("x")  # type: ignore[return-value]

    code, env = run(app, ["pay"])
    assert code != 0 and env["error"]["code"] == "INVALID_OUTPUT"
    assert "none of Card, Transfer" in env["error"]["message"]


class Kind(StrEnum):
    MARKET = "market"
    EVENT = "event"


@dataclass(frozen=True, slots=True)
class Shared:
    name: str
    kind: Kind


@dataclass(frozen=True, slots=True)
class Other:
    name: str
    kind: Kind


@dataclass(frozen=True, slots=True)
class Narrow:
    name: str


def test_an_untagged_union_is_refused_with_how_to_tag_it() -> None:
    app = App("probe", version="0.1.0")
    with pytest.raises(RegistrationError) as caught:

        @app.command("x", description="Show", danger_level="safe", exit_codes=())
        def x(args: NoArgs, ctx: Ctx) -> Shared | Other:
            return Shared("a", Kind.MARKET)

    message = str(caught.value)
    assert message.startswith("x: output union")
    assert "cannot tell Shared, Other apart" in message
    assert 'kind: Literal["shared"] on Shared' in message


def test_a_member_whose_keys_another_has_is_refused() -> None:
    """Shared has a key of its own, but every key of Narrow is Shared's too"""
    app = App("probe", version="0.1.0")
    with pytest.raises(RegistrationError, match="cannot tell Shared, Narrow apart"):

        @app.command("x", description="Show", danger_level="safe", exit_codes=())
        def x(args: NoArgs, ctx: Ctx) -> list[Shared | Narrow]:
            return []


def test_a_union_of_scalars_or_an_argument_union_stays_refused() -> None:
    app = App("probe", version="0.1.0")
    with pytest.raises(RegistrationError, match=r"only 'X \| None' is allowed"):

        @app.command("x", description="Show", danger_level="safe", exit_codes=())
        def x(args: NoArgs, ctx: Ctx) -> Card | list[str]:
            return []

    @dataclass(frozen=True, slots=True)
    class Either:
        value: int | str = Arg(description="Either")

    with pytest.raises(RegistrationError, match=r"only 'X \| None' is allowed"):

        @app.command("y", description="Show", danger_level="safe", exit_codes=())
        def y(args: Either, ctx: Ctx) -> Card:
            return Card("card", "1", "t")


def test_the_mcp_tool_and_a_strict_audit_accept_the_union() -> None:
    app = tagged_app()
    tool = next(t for t in tool_entries(app) if t.name == "all")
    fields = tool_fields({"outputSchema": tool.output_schema})
    assert {"data.kind", "data.last4", "data.iban", "data.token"} <= fields.keys()
    report = audit(app, "x:app", limit=100)
    warnings = [
        f
        for r in report.rules
        for f in r.findings
        if f.severity is not Severity.ADVICE and f.rule != "stable-order"
    ]
    assert warnings == []


@dataclass(frozen=True, slots=True)
class Opened:
    kind: Literal["opened"]
    effect: Literal["created", "noop"]


@dataclass(frozen=True, slots=True)
class Queued:
    kind: Literal["queued"]
    effect: Literal["created"]


def test_a_mutating_union_needs_effect_on_every_member() -> None:
    app = App("probe", version="0.1.0")

    @app.command("open", description="Open", danger_level="mutating", exit_codes=())
    def open_(args: NoArgs, ctx: Ctx) -> Opened | Queued:
        return Queued("queued", "created")

    code, env = run(app, ["open"])
    assert code == 0 and env["data"] == {"kind": "queued", "effect": "created"}
    branches = app.commands[CommandPath("open")].output_schema["oneOf"]
    assert [b["properties"]["effect"]["enum"] for b in branches] == [
        ["created", "noop"],
        ["created", "noop"],
    ]
    with pytest.raises(RegistrationError, match="effect"):

        @app.command("close", description="Close", danger_level="mutating", exit_codes=())
        def close(args: NoArgs, ctx: Ctx) -> Opened | Card:
            return Card("card", "1", "t")


def test_a_dict_of_a_union_is_one_of_too() -> None:
    app = tagged_app()

    @app.command("by-id", description="Map", danger_level="safe", exit_codes=())
    def by_id(args: NoArgs, ctx: Ctx) -> dict[str, Card | Transfer]:
        return {"x": Transfer("transfer", "DE89", "a")}

    schema = app.commands[CommandPath("by-id")].output_schema
    assert [b["title"] for b in schema["additionalProperties"]["oneOf"]] == ["Card", "Transfer"]
    code, env = run(app, ["by-id"])
    assert code == 0 and env["data"]["x"]["iban"] == "DE89"


@dataclass(frozen=True, slots=True)
class Folder:
    kind: Literal["folder"]
    id: str


@dataclass(frozen=True, slots=True)
class File:
    kind: Literal["file"]
    id: str
    size: int


def test_format_id_writes_the_id_every_member_has() -> None:
    app = App("probe", version="0.1.0")

    @app.command("ls", description="List", danger_level="safe", exit_codes=(), sort_key="id")
    def ls(args: NoArgs, ctx: Ctx) -> list[Folder | File]:
        return [File("file", "b", 1), Folder("folder", "a")]

    assert app.commands[CommandPath("ls")].id_field == "id"
    assert plain(app, ["ls"]).splitlines()[0].split() == ["kind", "id", "size"]
    out = io.StringIO()
    app.run(["ls", "--format", "id"], stdin=io.StringIO(), stdout=out, stderr=io.StringIO())
    assert out.getvalue() == "a\nb\n"
