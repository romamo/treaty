"""Object arguments (#6): a frozen dataclass, or a tuple of them, as a flag's value"""

import io
import json
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Literal

import pytest
from conftest import spec_validator

from treaty import App, Arg, Ctx, Flag, RegistrationError, Subprocess
from treaty._tools import input_schema


class Side(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


@dataclass(frozen=True, slots=True)
class AccountRef:
    value: str


@dataclass(frozen=True, slots=True)
class Amount:
    number: Decimal
    currency: Literal["EUR", "USD"] = "EUR"


@dataclass(frozen=True, slots=True)
class Posting:
    account: AccountRef
    amount: Amount
    side: Side = Side.DEBIT
    memo: str | None = Flag(default=None, description="Free text", multiline=True)
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AddArgs:
    postings: tuple[Posting, ...] = Flag(default=(), description="Postings", from_stdin=True)
    primary: Posting | None = Flag(default=None, description="The primary posting")


def make_app(**kw: object) -> App:
    app = App("ledger", version="1.0.0", **kw)  # type: ignore[arg-type]
    app.scalar(AccountRef, parse=AccountRef, pattern=r"[a-z][a-z0-9:]*")

    @app.command(
        "add",
        description="Add a transaction",
        danger_level="mutating",
        exit_codes=(),
        supports_raw_payload=True,
    )
    def add(args: AddArgs, ctx: Ctx) -> dict[str, object]:
        return {
            "effect": "created",
            "postings": [
                {
                    "account": p.account.value,
                    "number": str(p.amount.number),
                    "currency": p.amount.currency,
                    "side": p.side.value,
                    "memo": p.memo,
                    "tags": list(p.tags),
                }
                for p in args.postings
            ],
            "primary": None if args.primary is None else args.primary.account.value,
        }

    return app


def run(argv: list[str], *, stdin: str | None = None, app: App | None = None) -> tuple[int, dict]:
    out = io.StringIO()
    code = (app or make_app()).run(
        argv,
        stdout=out,
        stderr=io.StringIO(),
        env={},
        isatty=False,
        stdin=io.StringIO(stdin) if stdin is not None else None,
    )
    envelope = json.loads(out.getvalue())
    spec_validator("response-envelope").validate(envelope)
    return code, envelope


CASH = '{"account": "cash", "amount": {"number": "12.30"}}'
BANK = '{"account": "bank", "amount": {"number": -12.3, "currency": "USD"}, "side": "credit"}'
EXPECTED = [
    {
        "account": "cash",
        "number": "12.30",
        "currency": "EUR",
        "side": "debit",
        "memo": None,
        "tags": [],
    },
    {
        "account": "bank",
        "number": "-12.3",
        "currency": "USD",
        "side": "credit",
        "memo": None,
        "tags": [],
    },
]


def postings(env: dict) -> list[dict]:
    """The handler's postings in argv order; data's unordered lists come back sorted"""
    return sorted(env["data"]["postings"], key=lambda p: p["account"] != "cash")


def test_argv_takes_one_json_object_per_repeat() -> None:
    code, env = run(["add", "--postings", CASH, "--postings", BANK])
    assert code == 0, env
    assert postings(env) == EXPECTED


def test_a_single_object_flag_takes_one_json_object() -> None:
    code, env = run(["add", "--primary", "{account: 'cash', amount: {number: 1}}"])
    assert code == 0, env
    assert env["data"]["primary"] == "cash"


def test_raw_payload_exec_and_call_carry_json_objects() -> None:
    payload = {"postings": [json.loads(CASH), json.loads(BANK)]}
    code, env = run(["add", "--raw-payload", json.dumps(payload)])
    assert code == 0 and postings(env) == EXPECTED
    out = io.StringIO()
    line = json.dumps({"_cmd": "add", **payload})
    make_app().run(
        ["exec"], stdin=io.StringIO(line + "\n"), stdout=out, stderr=io.StringIO(), env={}
    )
    assert postings(json.loads(out.getvalue())) == EXPECTED
    called = make_app().call("add", {"postings": [{"account": "cash", "amount": {"number": 5}}]})
    assert called.ok and called.data["postings"][0]["number"] == "5"  # type: ignore[index]


def test_stdin_reads_one_object_per_line() -> None:
    code, env = run(["add", "--postings", "-"], stdin=f"{CASH}\n{BANK}\n")
    assert code == 0 and postings(env) == EXPECTED


def test_nested_errors_point_into_the_structure_and_are_all_collected() -> None:
    bad = '{"account": "cash", "amount": {"number": "1e3", "currency": "GBP"}, "extra": 1}'
    missing = '{"amount": {"number": "1"}, "side": "sideways", "tags": ["a", 7]}'
    code, env = run(["add", "--postings", CASH, "--postings", bad, "--postings", missing])
    assert code == 2
    fields = [e["field"] for e in env["error"]["errors"]]
    assert fields == [
        "postings[1].amount.number",
        "postings[1].amount.currency",
        "postings[1].extra",
        "postings[2].side",
        "postings[2].tags[1]",
        "postings[2].account",
    ]
    by_field = {e["field"]: e for e in env["error"]["errors"]}
    assert by_field["postings[1].extra"]["message"] == "Unknown field 'postings[1].extra'"
    assert by_field["postings[2].account"]["message"] == "Missing required: postings[2].account"
    # The same locations from a JSON payload
    payload = {"postings": [json.loads(CASH), json.loads(bad), json.loads(missing)]}
    code, env = run(["add", "--raw-payload", json.dumps(payload)])
    assert code == 2 and [e["field"] for e in env["error"]["errors"]] == fields


def test_nested_scalars_go_through_the_registered_parser() -> None:
    code, env = run(["add", "--postings", '{"account": "Bad Name", "amount": {"number": "1"}}'])
    assert code == 2
    assert env["error"]["errors"][0]["field"] == "postings[0].account"
    assert env["error"]["context"]["scalar"] == "AccountRef"


def test_malformed_json_on_argv_names_the_field() -> None:
    code, env = run(["add", "--postings", CASH, "--postings", "{not json"])
    assert code == 2 and env["error"]["code"] == "INVALID_JSON"
    assert env["error"]["context"]["flag"] == "postings[1]"
    code, env = run(["add", "--primary", "[1, 2]"])
    assert code == 2 and env["error"]["context"] == {"field": "primary", "type": "array"}


@dataclass(frozen=True, slots=True)
class Signer:
    name: str
    pin: str = Flag(description="Signing PIN", secret=True)


@dataclass(frozen=True, slots=True)
class Leg:
    account: str
    api_token: str | None = None


@dataclass(frozen=True, slots=True)
class Batch:
    legs: tuple[Leg, ...] = ()
    signer: Signer | None = None


def test_a_secret_inside_an_object_is_refused_naming_its_path() -> None:
    """REQ-C-016: an object travels on argv, so a secret in one, declared or by its name,
    must be a top-level flag read from --x-from-env or --x-from-file"""

    @dataclass(frozen=True, slots=True)
    class ByName:
        postings: tuple[Leg, ...] = Flag(default=(), description="Legs")

    with pytest.raises(RegistrationError, match=r"go: .*postings\[\]\.api_token .*--x-from-env"):
        register(ByName)

    @dataclass(frozen=True, slots=True)
    class Declared:
        signer: Signer = Flag(description="Signer")

    with pytest.raises(RegistrationError, match=r"signer\.pin"):
        register(Declared)

    @dataclass(frozen=True, slots=True)
    class Deep:
        batch: Batch = Flag(description="Batch")

    with pytest.raises(RegistrationError, match=r"batch\.legs\[\]\.api_token, batch\.signer\.pin"):
        register(Deep)

    @dataclass(frozen=True, slots=True)
    class Cleared:
        account: str
        api_token: str = Flag(description="A token's public id", secret=False)

    @dataclass(frozen=True, slots=True)
    class Fine:
        item: Cleared = Flag(description="Item")

    register(Fine)


def test_schema_manifest_and_mcp_carry_the_object_shape() -> None:
    app = make_app()
    spec_validator("manifest-response").validate(app.manifest())
    entry = app.manifest()["commands"]["add"]
    assert entry["flags"]["postings"]["type"] == "array"
    assert entry["flags"]["postings"]["default"] == []
    assert entry["flags"]["primary"]["type"] == "string"
    assert (
        "{account: string, amount: {number: decimal, currency?: EUR|USD}"
        in (entry["flags"]["primary"]["description"])
    )
    out = io.StringIO()
    app.run(["add", "--schema"], stdout=out, stderr=io.StringIO(), env={}, isatty=False)
    schema = json.loads(out.getvalue())["data"]["raw_payload_schema"]
    posting = schema["properties"]["postings"]["items"]
    assert posting["additionalProperties"] is False
    assert posting["required"] == ["account", "amount"]
    assert posting["properties"]["memo"]["description"] == "Free text"
    assert posting["properties"]["amount"]["properties"]["number"]["format"] == "decimal"
    assert posting["properties"]["side"] == {"type": "string", "enum": ["debit", "credit"]}
    tool = input_schema(app.commands[next(iter(c for c in app.commands if c.value == "add"))])
    assert tool["properties"]["postings"]["items"] == posting


def test_help_shows_the_shape_and_completion_offers_no_values() -> None:
    out = io.StringIO()
    make_app().run(["add", "--help", "--format", "plain"], stdout=out, stderr=io.StringIO())
    assert "--postings JSON" in out.getvalue()
    assert "repeat it, one JSON object each: {account: string" in out.getvalue()
    out = io.StringIO()
    make_app().run(["completion", "bash", "--format", "plain"], stdout=out, stderr=io.StringIO())
    script = out.getvalue()
    assert "--postings" in script and "debit" not in script


def test_validate_only_runs_the_nested_checks() -> None:
    code, env = run(["add", "--validate-only", "--postings", CASH])
    assert code == 0, env
    code, env = run(["add", "--validate-only", "--postings", '{"account": "cash"}'])
    assert code == 2 and env["error"]["context"]["field"] == "postings[0].amount"


def test_post_init_errors_of_an_object_are_located() -> None:
    from treaty import ParseError

    @dataclass(frozen=True, slots=True)
    class Range:
        low: int
        high: int

        def __post_init__(self) -> None:
            if self.low > self.high:
                raise ParseError("low is above high", context={"field": "low"})

    @dataclass(frozen=True, slots=True)
    class Args:
        ranges: tuple[Range, ...] = Flag(default=(), description="Ranges")

    app = App("r", version="1.0.0")

    @app.command("check", description="Check", danger_level="safe", exit_codes=())
    def check(args: Args, ctx: Ctx) -> dict[str, int]:
        return {"n": len(args.ranges)}

    code, env = run(
        ["check", "--ranges", '{"low": 1, "high": 2}', "--ranges", '{"low": 3, "high": 2}'],
        app=app,
    )
    assert code == 2 and env["error"]["context"]["field"] == "ranges[1].low"


def test_defaults_and_default_factory() -> None:
    @dataclass(frozen=True, slots=True)
    class Item:
        name: str
        labels: tuple[str, ...] = field(default_factory=tuple)

    @dataclass(frozen=True, slots=True)
    class Args:
        item: Item = Flag(default=Item("x"), description="The item")

    app = App("d", version="1.0.0")

    @app.command("show", description="Show", danger_level="safe", exit_codes=())
    def show(args: Args, ctx: Ctx) -> dict[str, object]:
        return {"name": args.item.name, "labels": list(args.item.labels)}

    entry = app.manifest()["commands"]["show"]["flags"]["item"]
    assert entry["default"] == {"name": "x", "labels": []}
    code, env = run(["show"], app=app)
    assert env["data"] == {"name": "x", "labels": []}
    code, env = run(["show", "--item", '{"name": "y"}'], app=app)
    assert env["data"] == {"name": "y", "labels": []}


def register(args_type: type) -> None:
    app = App("bad", version="1.0.0")

    @app.command("go", description="Go", danger_level="safe", exit_codes=())
    def go(args: args_type, ctx: Ctx) -> None:  # type: ignore[valid-type]
        return None


def test_registration_refuses_shapes_the_parser_cannot_take() -> None:
    @dataclass(slots=True)
    class Loose:
        x: int

    @dataclass(frozen=True, slots=True)
    class LooseArgs:
        loose: Loose = Flag(description="Loose")

    with pytest.raises(RegistrationError, match="declare it @dataclass\\(frozen=True\\)"):
        register(LooseArgs)

    @dataclass(frozen=True, slots=True)
    class Positional:
        point: Amount = Arg(description="Point")

    with pytest.raises(RegistrationError, match="go: .*a dataclass positional"):
        register(Positional)

    @dataclass(frozen=True, slots=True)
    class Short:
        x: int = Flag(description="X", short="x")

    @dataclass(frozen=True, slots=True)
    class ShortArgs:
        short: Short = Flag(description="Short")

    with pytest.raises(RegistrationError, match="Short.x: .*not short"):
        register(ShortArgs)

    @dataclass(frozen=True, slots=True)
    class SecretObject:
        auth: Amount = Flag(description="Auth", secret=True)

    with pytest.raises(RegistrationError, match="cannot hold a secret"):
        register(SecretObject)


@dataclass(frozen=True)
class Node:
    name: str
    children: tuple[Node, ...] = ()


@dataclass(frozen=True)
class D8:
    x: int


@dataclass(frozen=True)
class D7:
    d: D8


@dataclass(frozen=True)
class D6:
    d: D7


@dataclass(frozen=True)
class D5:
    d: D6


@dataclass(frozen=True)
class D4:
    d: D5


@dataclass(frozen=True)
class D3:
    d: D4


@dataclass(frozen=True)
class D2:
    d: D3


@dataclass(frozen=True)
class D1:
    d: D2


@dataclass(frozen=True)
class D0:
    d: D1


def test_recursive_and_too_deep_objects_are_refused() -> None:
    @dataclass(frozen=True, slots=True)
    class TreeArgs:
        tree: Node = Flag(description="Tree")

    with pytest.raises(RegistrationError, match="Node contains itself"):
        register(TreeArgs)

    @dataclass(frozen=True, slots=True)
    class Deep:
        deep: D0 = Flag(description="Deep")

    with pytest.raises(RegistrationError, match="nest at most 8 deep"):
        register(Deep)

    @dataclass(frozen=True, slots=True)
    class Fine:
        deep: D1 = Flag(description="Eight deep")

    register(Fine)


def test_settings_and_subprocess_refuse_object_fields() -> None:
    @dataclass(frozen=True)
    class Settings:
        amount: Amount = Amount(Decimal(1))

    app = App("s", version="1.0.0", settings=Settings)

    with pytest.raises(RegistrationError, match="amount"):

        @app.command("go", description="Go", danger_level="safe", exit_codes=())
        def go(settings: Settings, ctx: Ctx) -> None:
            return None

        app.manifest()

    @dataclass(frozen=True, slots=True)
    class RunArgs:
        amount: Amount = Flag(description="Amount")

    app = App("p", version="1.0.0")
    with pytest.raises(RegistrationError, match="object fields \\['amount'\\]"):

        @app.command(
            "go",
            description="Go",
            danger_level="safe",
            exit_codes=(),
            subprocess=Subprocess("echo", user_controlled_args=("amount",)),
        )
        def run_it(args: RunArgs, ctx: Ctx) -> None:
            return None


def test_post_init_error_on_a_field_named_like_the_flag_is_located() -> None:
    """``window_start`` begins with ``window`` but is not under it, so it is prefixed"""
    from treaty import ParseError

    @dataclass(frozen=True, slots=True)
    class Window:
        window_start: int
        window_end: int

        def __post_init__(self) -> None:
            if self.window_start > self.window_end:
                raise ParseError("start is after end", context={"field": "window_start"})

    @dataclass(frozen=True, slots=True)
    class Args:
        window: Window = Flag(description="The window")

    app = App("w", version="1.0.0")

    @app.command("check", description="Check", danger_level="safe", exit_codes=())
    def check(args: Args, ctx: Ctx) -> dict[str, int]:
        return {"start": args.window.window_start}

    code, env = run(["check", "--window", '{"window_start": 3, "window_end": 2}'], app=app)
    assert code == 2 and env["error"]["context"]["field"] == "window.window_start"
