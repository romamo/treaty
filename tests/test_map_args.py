"""Mapping arguments (#299): ``dict[str, V]`` as an object's field or a flag's value"""

import io
import json
import types
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal

import pytest
from conftest import spec_validator

from treaty import App, Ctx, Flag, NoArgs, RegistrationError
from treaty._tools import input_schema


class Tier(StrEnum):
    GOLD = "gold"
    SILVER = "silver"


@dataclass(frozen=True, slots=True)
class Code:
    value: str


@dataclass(frozen=True, slots=True)
class Posting:
    account: str
    meta: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class AddArgs:
    postings: tuple[Posting, ...] = Flag(default=(), description="Postings")
    labels: dict[str, str | int | Decimal | bool] | None = Flag(default=None, description="Labels")
    flags: dict[str, bool] = Flag(
        default=types.MappingProxyType({"draft": False}), description="Switches"
    )
    tiers: dict[str, Tier] | None = Flag(default=None, description="Tiers")
    codes: dict[str, Code] | None = Flag(default=None, description="Codes")
    counts: dict[str, int | bool] | None = Flag(default=None, description="Counts")


def make_app() -> App:
    app = App("ledger", version="1.0.0")
    app.scalar(Code, parse=Code, pattern=r"[A-Z]{3}")

    @app.command(
        "add",
        description="Add a transaction",
        danger_level="mutating",
        exit_codes=(),
        supports_raw_payload=True,
    )
    def add(args: AddArgs, ctx: Ctx) -> dict[str, object]:
        labels = args.labels or {}
        return {
            "effect": "created",
            "metas": [p.meta for p in args.postings],
            "labels": {k: f"{type(v).__name__}:{v}" for k, v in labels.items()},
            "flags": dict(args.flags),
            "tiers": {k: type(v).__name__ for k, v in (args.tiers or {}).items()},
            "codes": {k: v.value for k, v in (args.codes or {}).items()},
            "counts": {k: type(v).__name__ for k, v in (args.counts or {}).items()},
            "is_dict": all(isinstance(p.meta, dict | None) for p in args.postings),
        }

    return app


def run(argv: list[str], *, app: App | None = None, env: dict[str, str] | None = None) -> tuple:
    out = io.StringIO()
    code = (app or make_app()).run(
        argv, stdout=out, stderr=io.StringIO(), env=env or {}, isatty=False
    )
    envelope = json.loads(out.getvalue())
    spec_validator("response-envelope").validate(envelope)
    return code, envelope


def fields_of(envelope: dict) -> list[str]:
    return [e["field"] for e in envelope["error"]["errors"]]


POSTING = '{"account": "cash", "meta": {"ref": "INV-1", "note": "rent"}}'


def test_an_object_field_takes_a_mapping_of_strings() -> None:
    """The issue's shape registers, and each posting's meta arrives as a dict"""
    code, env = run(["add", "--postings", POSTING, "--postings", '{"account": "bank"}'])
    assert code == 0, env
    metas = sorted(env["data"]["metas"], key=lambda m: m is None)
    assert metas == [{"ref": "INV-1", "note": "rent"}, None]
    assert env["data"]["is_dict"] is True


def test_a_flag_takes_a_json_object_with_union_values() -> None:
    labels = '{"a": "12.30", "b": 7, "c": true, "d": 1.5}'
    code, env = run(["add", "--labels", labels])
    assert code == 0, env
    # Declaration order decides: a string stays a str, 1.5 is no str or int, so a Decimal
    assert env["data"]["labels"] == {
        "a": "str:12.30",
        "b": "int:7",
        "c": "bool:True",
        "d": "Decimal:1.5",
    }


def test_values_are_parsed_by_their_type_and_errors_point_at_the_key() -> None:
    bad = '{"account": "cash", "meta": {"ok": "x", "n": 1, "line": "a\\nb"}}'
    code, env = run(["add", "--postings", POSTING, "--postings", bad])
    assert code == 2
    assert fields_of(env) == ["postings[1].meta.n", "postings[1].meta.line"]
    code, env = run(["add", "--postings", '{"account": "cash", "meta": ["x"]}'])
    assert code == 2 and env["error"]["context"] == {
        "field": "postings[0].meta",
        "type": "array",
    }


def test_booleans_and_integers_keep_their_json_types() -> None:
    """A boolean takes no 0, 1, or "true", an integer no true, and an int | bool union
    puts each JSON value where it belongs, whatever the order"""
    for value in ("1", "0", '"true"', "null"):
        code, env = run(["add", "--flags", f'{{"draft": {value}}}'])
        assert code == 2 and fields_of(env) == ["flags.draft"], value
    code, env = run(["add", "--counts", '{"n": 3, "b": true}'])
    assert code == 0 and env["data"]["counts"] == {"n": "int", "b": "bool"}
    code, env = run(["add", "--labels", '{"x": null, "y": [1]}'])
    assert code == 2 and fields_of(env) == ["labels.x", "labels.y"]
    error = env["error"]["errors"][0]
    assert error["message"] == "'labels.x' expects string or integer or decimal or boolean."


def test_enums_and_registered_scalars_as_values() -> None:
    code, env = run(["add", "--tiers", '{"a": "gold"}', "--codes", '{"x": "EUR"}'])
    assert code == 0, env
    assert env["data"]["tiers"] == {"a": "Tier"} and env["data"]["codes"] == {"x": "EUR"}
    code, env = run(["add", "--tiers", '{"a": "bronze"}', "--codes", '{"x": "eur"}'])
    assert code == 2 and fields_of(env) == ["tiers.a", "codes.x"]


def test_raw_payload_exec_and_call_carry_mappings() -> None:
    payload = {"postings": [json.loads(POSTING)], "flags": {"draft": True}}
    code, env = run(["add", "--raw-payload", json.dumps(payload)])
    assert code == 0 and env["data"]["metas"] == [{"ref": "INV-1", "note": "rent"}]
    assert env["data"]["flags"] == {"draft": True}
    out = io.StringIO()
    line = json.dumps({"_cmd": "add", **payload})
    make_app().run(
        ["exec"], stdin=io.StringIO(line + "\n"), stdout=out, stderr=io.StringIO(), env={}
    )
    assert json.loads(out.getvalue())["data"]["flags"] == {"draft": True}
    called = make_app().call("add", {"labels": {"k": 1}})
    assert called.ok and called.data["labels"] == {"k": "int:1"}  # type: ignore[index]
    refused = make_app().call("add", {"labels": {"k": {"nested": 1}}})
    assert not refused.ok


def test_a_read_only_default_is_shared_safely_and_listed() -> None:
    code, env = run(["add"])
    assert code == 0 and env["data"]["flags"] == {"draft": False}
    entry = make_app().manifest()["commands"]["add"]["flags"]["flags"]
    assert entry["default"] == {"draft": False}
    with pytest.raises(RegistrationError, match=r"types\.MappingProxyType"):
        Flag(default={"draft": False}, description="Switches")

    @dataclass(frozen=True, slots=True)
    class WrongDefault:
        flags: dict[str, bool] = Flag(
            default=types.MappingProxyType({"draft": "no"}), description="Switches"
        )

    with pytest.raises(RegistrationError, match=r"WrongDefault\.flags\.draft: default 'no'"):
        register(WrongDefault)


def test_schema_manifest_and_mcp_carry_additional_properties() -> None:
    app = make_app()
    manifest = app.manifest()
    spec_validator("manifest-response").validate(manifest)
    flags = manifest["commands"]["add"]["flags"]
    assert flags["flags"]["type"] == "object"
    assert flags["flags"]["schema"]["additionalProperties"] == {"type": "boolean"}
    labels = flags["labels"]["schema"]["additionalProperties"]["anyOf"]
    assert [b["type"] for b in labels] == ["string", "integer", "string", "boolean"]
    meta = flags["postings"]["schema"]["properties"]["meta"]
    assert meta == {
        "anyOf": [
            {"type": "object", "additionalProperties": {"type": "string"}},
            {"type": "null"},
        ]
    }
    tool = input_schema(next(c for p, c in app.commands.items() if p.value == "add"))
    assert tool["properties"]["flags"]["additionalProperties"] == {"type": "boolean"}
    assert tool["properties"]["postings"]["items"]["properties"]["meta"] == meta


def test_help_shows_the_shape_and_completion_offers_no_values() -> None:
    out = io.StringIO()
    make_app().run(["add", "--help", "--format", "plain"], stdout=out, stderr=io.StringIO())
    text = out.getvalue()
    assert "{account: string, meta?: {<key>: string}}" in text
    assert "a JSON object: {<key>: string|integer|decimal|boolean}" in text
    out = io.StringIO()
    make_app().run(["completion", "bash", "--format", "plain"], stdout=out, stderr=io.StringIO())
    assert "--labels" in out.getvalue() and "gold" not in out.getvalue()


def register(args_type: type) -> None:
    app = App("bad", version="1.0.0")

    @app.command("go", description="Go", danger_level="safe", exit_codes=())
    def go(args: args_type, ctx: Ctx) -> None:  # type: ignore[valid-type]
        return None


@pytest.mark.parametrize(
    "annotation",
    [
        dict[str, dict[str, str]],
        dict[int, str],
        dict,
        dict[str, str | None],
        dict[str, Posting],
        dict[str, tuple[str, ...]],
    ],
    ids=["nested", "int-keys", "bare", "null-values", "object-values", "array-values"],
)
def test_shapes_outside_the_supported_mapping_are_refused(annotation: object) -> None:
    @dataclass(frozen=True, slots=True)
    class Holder:
        meta: annotation  # type: ignore[valid-type]

    @dataclass(frozen=True, slots=True)
    class Args:
        holder: Holder = Flag(description="Holder")

    with pytest.raises(RegistrationError, match=r"Holder\.meta: .*a mapping is dict\[str, V\]"):
        register(Args)


def test_pattern_and_secret_are_refused_on_a_mapping() -> None:
    @dataclass(frozen=True, slots=True)
    class Patterned:
        meta: dict[str, str] = Flag(description="Meta", pattern=r"[a-z]+")

    with pytest.raises(RegistrationError, match="pattern= is for str fields"):
        register(Patterned)

    @dataclass(frozen=True, slots=True)
    class Secret:
        tokens: dict[str, str] = Flag(description="Tokens", secret=True)

    with pytest.raises(RegistrationError, match="cannot hold a secret"):
        register(Secret)

    @dataclass(frozen=True, slots=True)
    class Leg:
        account: str
        keys: dict[str, str] | None = Flag(default=None, description="Keys", secret=True)

    @dataclass(frozen=True, slots=True)
    class Inside:
        leg: Leg = Flag(description="Leg")

    with pytest.raises(RegistrationError, match=r"leg\.keys would be a secret inside"):
        register(Inside)


def test_an_object_field_default_factory_gives_each_value_its_own_dict() -> None:
    @dataclass(frozen=True, slots=True)
    class Item:
        name: str
        tags: dict[str, str] = field(default_factory=dict)

    @dataclass(frozen=True, slots=True)
    class Args:
        items: tuple[Item, ...] = Flag(default=(), description="Items")

    app = App("d", version="1.0.0")

    @app.command("show", description="Show", danger_level="safe", exit_codes=())
    def show(args: Args, ctx: Ctx) -> dict[str, object]:
        return {"same": args.items[0].tags is args.items[1].tags, "tags": args.items[0].tags}

    code, env = run(["show", "--items", '{"name": "a"}', "--items", '{"name": "b"}'], app=app)
    assert code == 0 and env["data"] == {"same": False, "tags": {}}


@dataclass(frozen=True, slots=True)
class Settings:
    limits: dict[str, int] | None = None
    region: Literal["eu", "us"] = "eu"
    api_keys: dict[str, str] | None = None


def settings_app() -> App:
    app = App("conf", version="1.0.0", settings=Settings)

    @app.command("show", description="Show", danger_level="safe", exit_codes=())
    def show(args: NoArgs, ctx: Ctx, settings: Settings) -> dict[str, object]:
        return {"limits": settings.limits, "keys": len(settings.api_keys or {})}

    return app


def test_a_setting_takes_a_toml_table_or_a_json_object_variable(tmp_path: Path) -> None:
    path = tmp_path / "conf.toml"
    path.write_text('[limits]\ncpu = 2\nmem = 512\n\n[api_keys]\nprod = "sk-live-0123456789"\n')
    code, env = run(["show"], app=settings_app(), env={"CONF_CONFIG": str(path)})
    assert code == 0, env
    assert env["data"] == {"limits": {"cpu": 2, "mem": 512}, "keys": 1}
    code, env = run(["show"], app=settings_app(), env={"CONF_LIMITS": '{"cpu": 4}'})
    assert code == 0 and env["data"]["limits"] == {"cpu": 4}
    code, env = run(["show"], app=settings_app(), env={"CONF_LIMITS": '{"cpu": "four"}'})
    assert code == 2 and env["error"]["code"] == "CONFIG_INVALID"
    assert "limits.cpu" in env["error"]["message"]
    path.write_text("[limits]\ncpu = true\n")
    code, env = run(["show"], app=settings_app(), env={"CONF_CONFIG": str(path)})
    assert code == 2 and "limits.cpu" in env["error"]["message"]


def test_path_values_are_checked_and_rooted_under_cwd(tmp_path: Path) -> None:
    @dataclass(frozen=True, slots=True)
    class Args:
        files: dict[str, Path] = Flag(description="Files by role")

    app = App("p", version="1.0.0")

    @app.command("show", description="Show", danger_level="safe", exit_codes=())
    def show(args: Args, ctx: Ctx) -> dict[str, str]:
        return {k: str(v) for k, v in args.files.items()}

    code, env = run(["show", "--cwd", str(tmp_path), "--files", '{"in": "a.txt"}'], app=app)
    assert code == 0 and env["data"] == {"in": str(tmp_path.resolve() / "a.txt")}
    code, env = run(["show", "--files", '{"in": "a%2e%2e/b"}'], app=app)
    assert code == 2 and fields_of(env) == ["files.in"]


def test_a_secret_mapping_setting_is_redacted_value_by_value(tmp_path: Path) -> None:
    path = tmp_path / "conf.toml"
    path.write_text('[api_keys]\nprod = "sk-live-0123456789"\n')
    out = io.StringIO()
    settings_app().run(
        ["--show-config"],
        stdout=out,
        stderr=io.StringIO(),
        env={"CONF_CONFIG": str(path)},
        isatty=False,
    )
    text = out.getvalue()
    assert "sk-live-0123456789" not in text
    assert json.loads(text)["data"]["effective_config"]["api_keys"] == "[REDACTED]"
