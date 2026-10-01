"""Args models through ``app.args_adapter`` (#102): a pydantic ``BaseModel`` as a
command's arguments, with no pydantic import in treaty"""

import io
import json
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import pytest
from conftest import spec_validator
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from treaty import Affects, App, ArgsAdapter, AuditLog, Ctx, RegistrationError
from treaty._tools import input_schema


class Side(Enum):
    BUY = "buy"
    SELL = "sell"


class SearchArgs(BaseModel):
    """Shared by several CLIs, as ``pydantic_market_data.cli_models`` is"""

    query: str = Field(
        description="Symbol or ISIN", json_schema_extra={"treaty": {"positional": True}}
    )
    limit: int = Field(10, gt=0, description="Most results")


class LookupArgs(SearchArgs):
    report_price: bool = Field(False, alias="reportPrice", description="Include the price")
    price: Decimal | None = Field(None, description="Price cap")
    side: Side = Field(Side.BUY, description="Order side")
    mode: Literal["fast", "full"] = Field(
        "fast", description="Mode", json_schema_extra={"treaty": {"short": "m"}}
    )
    tags: list[str] = Field(default_factory=lambda: ["default"], description="Tags")
    out: Path = Field(Path("out.json"), description="Output file")
    api_token: SecretStr | None = Field(None, description="API token")

    @model_validator(mode="after")
    def not_both(self) -> LookupArgs:
        if self.mode == "full" and self.report_price:
            raise ValueError("--mode full already reports the price")
        return self


def schema_of(cls: type[BaseModel]) -> dict[str, Any]:
    return cls.model_json_schema(by_alias=False)


def validate(cls: type[BaseModel], data: dict[str, object]) -> BaseModel:
    return cls.model_validate(data, by_name=True, by_alias=False)


def make_app(**kw: Any) -> App:
    app = App("market", version="1.0.0", **kw)
    app.args_adapter(BaseModel, schema=schema_of, validate=validate)

    @app.command(
        "lookup",
        description="Look up a security",
        danger_level="safe",
        exit_codes=(),
        supports_raw_payload=True,
    )
    def lookup(args: LookupArgs, ctx: Ctx) -> dict[str, object]:
        assert isinstance(args, LookupArgs)
        token = args.api_token
        return {
            "query": args.query,
            "limit": args.limit,
            "report_price": args.report_price,
            "price": None if args.price is None else str(args.price),
            "side": args.side.value,
            "mode": args.mode,
            "tags": list(args.tags),
            "out": str(args.out),
            "token_length": None if token is None else len(token.get_secret_value()),
        }

    return app


ENV = {"MARKET_API_TOKEN": "s3cr3t-t0ken-value"}


def run(argv: list[str], *, app: App | None = None) -> tuple[int, dict]:
    out = io.StringIO()
    code = (app or make_app()).run(argv, stdout=out, stderr=io.StringIO(), env=ENV, isatty=False)
    envelope = json.loads(out.getvalue())
    spec_validator("response-envelope").validate(envelope)
    return code, envelope


EXPECTED = {
    "query": "AAPL",
    "limit": 3,
    "report_price": True,
    "price": "12.30",
    "side": "sell",
    "mode": "fast",
    "tags": ["a", "b"],
    "out": "out.json",
    "token_length": 18,
}


def test_argv_reaches_the_handler_as_the_model() -> None:
    argv = ["lookup", "AAPL", "--limit", "3", "--report-price", "--price", "12.30"]
    code, env = run([*argv, "--side", "sell", "--tags", "a", "--tags", "b"])
    assert code == 0, env
    assert env["data"] == EXPECTED


def test_defaults_come_from_the_model() -> None:
    code, env = run(["lookup", "AAPL", "-m", "full"])
    assert code == 0, env
    assert env["data"]["limit"] == 10 and env["data"]["tags"] == ["default"]
    assert env["data"]["side"] == "buy" and env["data"]["mode"] == "full"


def test_raw_payload_exec_and_call_reach_the_same_model() -> None:
    payload = {
        "query": "AAPL",
        "limit": 3,
        "report_price": True,
        "price": "12.30",
        "side": "sell",
        "tags": ["a", "b"],
    }
    code, env = run(["lookup", "--raw-payload", json.dumps(payload)])
    assert code == 0 and env["data"] == EXPECTED
    out = io.StringIO()
    line = json.dumps({"_cmd": "lookup", **payload})
    make_app().run(["exec"], stdin=io.StringIO(line + "\n"), stdout=out, env=ENV)
    assert json.loads(out.getvalue())["data"] == EXPECTED
    called = make_app().call("lookup", payload, env=ENV)
    assert called.ok and called.data == EXPECTED


def test_validation_errors_are_phase_one_errors() -> None:
    code, env = run(["lookup", "AAPL", "--limit", "0"])
    assert code == 2 and env["error"]["code"] == "ARG_ERROR"
    assert env["error"]["errors"] == [
        {
            "message": "Input should be greater than 0",
            "field": "limit",
            "context": {"field": "limit", "type": "greater_than", "value": 0},
        }
    ]
    # A model_validator's error names no field
    code, env = run(["lookup", "AAPL", "--mode", "full", "--report-price"])
    assert code == 2
    assert env["error"]["errors"] == [
        {
            "message": "Value error, --mode full already reports the price.",
            "context": {"type": "value_error"},
        }
    ]
    # treaty's own parsing still comes first, strictly
    code, env = run(["lookup", "--raw-payload", '{"query": "x", "limit": "3"}'])
    assert code == 2 and env["error"]["context"]["field"] == "limit"
    code, env = run(["lookup", "AAPL", "--validate-only", "--limit", "0"])
    assert code == 2 and env["error"]["context"]["field"] == "limit"


def test_a_secret_field_is_a_treaty_secret(tmp_path: Path) -> None:
    code, env = run(["lookup", "AAPL", "--api-token", "x"])
    assert code == 2  # never on argv (REQ-C-016)
    path = tmp_path / "audit.jsonl"
    code, env = run(["lookup", "AAPL"], app=make_app(audit_log=AuditLog(path=path)))
    assert code == 0 and env["data"]["token_length"] == 18
    entry = json.loads(path.read_text().splitlines()[-1])
    assert entry["args"]["api_token"] == "[REDACTED]"
    assert "s3cr3t" not in path.read_text()


def test_schema_manifest_help_and_mcp_come_from_the_model() -> None:
    app = make_app()
    spec_validator("manifest-response").validate(app.manifest())
    entry = app.manifest()["commands"]["lookup"]
    assert entry["positionals"][0]["name"] == "query"
    flags = entry["flags"]
    assert flags["limit"] == {
        "type": "integer",
        "required": False,
        "description": "Most results",
        "default": 10,
    }
    assert flags["mode"]["short"] == "m" and flags["mode"]["enum_values"] == ["fast", "full"]
    assert flags["out"]["pattern_type"] == "filepath"
    assert "api-token-from-env" in flags and "api-token" not in flags
    out = io.StringIO()
    app.run(["lookup", "--schema"], stdout=out, stderr=io.StringIO(), env={}, isatty=False)
    raw = json.loads(out.getvalue())["data"]["raw_payload_schema"]
    assert raw["required"] == ["query"]
    assert raw["properties"]["price"]["anyOf"][0]["format"] == "decimal"
    command = next(c for p, c in app.commands.items() if p.value == "lookup")
    assert input_schema(command)["properties"]["side"]["enum"] == ["buy", "sell"]
    out = io.StringIO()
    app.run(["lookup", "--help", "--format", "plain"], stdout=out, stderr=io.StringIO())
    assert "market lookup <query> [flags]" in out.getvalue()
    assert "--report-price" in out.getvalue()


def test_a_class_needs_the_adapter() -> None:
    app = App("market", version="1.0.0")
    with pytest.raises(RegistrationError, match="app.args_adapter"):

        @app.command("lookup", description="Look up", danger_level="safe", exit_codes=())
        def lookup(args: LookupArgs, ctx: Ctx) -> dict[str, object]:
            return {}


def test_registration_refuses_what_the_parser_cannot_take() -> None:
    app = App("market", version="1.0.0")
    adapter = app.args_adapter(BaseModel, schema=schema_of, validate=validate)
    assert isinstance(adapter, ArgsAdapter)
    with pytest.raises(RegistrationError, match="overlaps"):
        app.args_adapter(SearchArgs, schema=schema_of, validate=validate)
    with pytest.raises(RegistrationError, match="scalar or an args model"):
        app.scalar(LookupArgs, parse=str)

    async def later(cls: type, data: dict[str, object]) -> object:
        return None

    with pytest.raises(RegistrationError, match="async def"):
        App("x", version="1.0.0").args_adapter(BaseModel, schema=schema_of, validate=later)

    class Inner(BaseModel):
        x: int

    class Nested(BaseModel):
        inner: Inner

    class Dashed(BaseModel):
        model_config = ConfigDict(populate_by_name=True)
        report_price: bool = Field(False, alias="report-price")

    class Unknown(BaseModel):
        x: int = Field(1, json_schema_extra={"treaty": {"colour": "red"}})

    for model, said in (
        (Nested, "Nested.inner: a JSON Schema of type 'object' has no flag form"),
        (Unknown, r"treaty options \['colour'\]"),
    ):
        with pytest.raises(RegistrationError, match=said):
            register(app, model)
    by_alias = App("market", version="1.0.0")
    by_alias.args_adapter(BaseModel, schema=lambda cls: cls.model_json_schema(), validate=validate)
    with pytest.raises(RegistrationError, match="named by a Python identifier"):
        register(by_alias, Dashed)


def register(app: App, model: type) -> None:
    @app.command("go", description="Go", danger_level="safe", exit_codes=())
    def go(args: model, ctx: Ctx) -> dict[str, object]:  # type: ignore[valid-type]
        return {}


class WipeArgs(BaseModel):
    force: bool = Field(False, description="Skip the safety check")
    dry_run: bool = Field(False, description="Preview only")

    @model_validator(mode="after")
    def no_forced_preview(self) -> WipeArgs:
        if self.force and self.dry_run:
            raise ValueError("--force makes no sense with --dry-run")
        return self


@dataclass(frozen=True, slots=True)
class Wiped:
    effect: str
    would_affect: Affects | None = None


def wipe_app(seen: list[WipeArgs]) -> App:
    app = App("market", version="1.0.0")
    app.args_adapter(BaseModel, schema=schema_of, validate=validate)

    @app.command("wipe", description="Wipe", danger_level="destructive", exit_codes=())
    def wipe(args: WipeArgs, ctx: Ctx) -> Wiped:
        seen.append(args)
        if args.dry_run:
            return Wiped("would_delete", Affects("Wipes everything", ("all",), 1))
        return Wiped("deleted")

    return app


def test_a_forced_preview_rebuilds_the_model_in_phase_one() -> None:
    """A destructive command run without --confirm-destructive is a dry run treaty switches
    on: the model is validated again with dry_run=True before anything runs (#161)"""
    seen: list[WipeArgs] = []
    code, env = run(["wipe"], app=wipe_app(seen))
    assert code == 2 and env["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert [type(a) for a in seen] == [WipeArgs] and seen[0].dry_run is True
    # The model_validator refusing the rebuilt model is exit 2, as at parse time
    seen.clear()
    code, env = run(["wipe", "--force"], app=wipe_app(seen))
    explicit_code, explicit = run(["wipe", "--force", "--dry-run"], app=wipe_app(seen))
    assert seen == []
    assert code == explicit_code == 2
    assert env["error"]["code"] == explicit["error"]["code"] == "ARG_ERROR"
    refusal = {
        "message": "Value error, --force makes no sense with --dry-run",
        "context": {"type": "value_error"},
    }
    assert explicit["error"]["errors"] == [refusal]
    # The forced run also names the flag that applies instead (#161)
    suggestion = env["error"]["errors"][0].pop("suggestion")
    assert "--confirm-destructive" in suggestion
    assert env["error"]["errors"] == [refusal]
    called = wipe_app(seen).call("wipe", {"force": True}, env=ENV)
    assert called.exit_code == 2 and called.error is not None
    assert list(called.error.errors) == [{**refusal, "suggestion": suggestion}] and seen == []
    confirmed = wipe_app(seen).call("wipe", {"force": True, "confirm_destructive": True}, env=ENV)
    assert confirmed.ok and confirmed.data == {"effect": "deleted", "would_affect": None}
