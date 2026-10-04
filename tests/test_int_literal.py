"""Integer ``Literal`` arguments (#327): ``Literal[0, 1, 2]`` is an integer of those values"""

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest
from conftest import spec_validator

from treaty import App, Arg, Ctx, Flag, NoArgs, RegistrationError
from treaty._mcp import call_tool, tool_entries
from treaty._tools import input_schema

type SigType = Literal[0, 1, 2]


@dataclass(frozen=True, slots=True)
class SignArgs:
    level: Literal[1, 3] = Arg(description="Level")
    sig_type: SigType = Flag(default=0, description="Signature type", env=("SIG_TYPE",))
    retry: Literal[0, 5] | None = Flag(default=None, description="Retry after")
    ports: tuple[Literal[80, 443], ...] = Flag(default=(), description="Ports")


def make_app() -> App:
    app = App("signer", version="1.0.0")

    @app.command(
        "sign",
        description="Sign a payload",
        danger_level="mutating",
        exit_codes=(),
        supports_raw_payload=True,
    )
    def sign(args: SignArgs, ctx: Ctx) -> dict[str, object]:
        return {
            "effect": "created",
            "level": args.level,
            "sig_type": args.sig_type,
            "retry": args.retry,
            "ports": list(args.ports),
            "types": sorted({type(v).__name__ for v in (args.level, args.sig_type)}),
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


def test_argv_text_becomes_an_int_of_the_literal() -> None:
    code, env = run(["sign", "3", "--sig-type", "2", "--retry", "5", "--ports", "443"])
    assert code == 0, env
    assert env["data"] == {
        "effect": "created",
        "level": 3,
        "sig_type": 2,
        "retry": 5,
        "ports": [443],
        "types": ["int"],
    }
    code, env = run(["sign", "1"])
    assert code == 0 and env["data"]["sig_type"] == 0 and env["data"]["retry"] is None


@pytest.mark.parametrize(
    "argv",
    [
        ["sign", "1", "--sig-type", "3"],
        ["sign", "1", "--sig-type", "abc"],
        ["sign", "1", "--sig-type", "1.0"],
        ["sign", "2"],
        ["sign", "1", "--ports", "8080"],
        ["sign", "1", "--retry", "1"],
    ],
)
def test_a_value_outside_the_literal_exits_2_before_the_handler(argv: list[str]) -> None:
    code, env = run(argv)
    assert code == 2 and env["error"]["code"] == "ARG_ERROR", env


def test_the_refusal_names_the_allowed_numbers() -> None:
    code, env = run(["sign", "1", "--sig-type", "3"])
    assert code == 2
    assert env["error"]["message"].startswith("'sig-type' must be one of 0, 1, 2")
    assert env["error"]["context"]["allowed"] == [0, 1, 2]


def test_variables_are_coerced_and_checked() -> None:
    code, env = run(["sign", "1"], env={"SIG_TYPE": "1"})
    assert code == 0 and env["data"]["sig_type"] == 1
    code, env = run(["sign", "1"], env={"SIGNER_SIG_TYPE": "2"})
    assert code == 0 and env["data"]["sig_type"] == 2
    for bad in ("3", "x"):
        code, env = run(["sign", "1"], env={"SIG_TYPE": bad})
        assert code == 2, env


def test_json_routes_take_numbers_and_refuse_strings_and_booleans() -> None:
    payload = {"level": 3, "sig_type": 1, "ports": [80]}
    code, env = run(["sign", "--raw-payload", json.dumps(payload)])
    assert code == 0 and env["data"]["sig_type"] == 1 and env["data"]["ports"] == [80]
    for bad in ({"sig_type": "1"}, {"sig_type": True}, {"sig_type": 3}, {"ports": ["80"]}):
        code, env = run(["sign", "--raw-payload", json.dumps({"level": 1} | bad)])
        assert code == 2, bad
    out = io.StringIO()
    lines = [
        json.dumps({"_cmd": "sign", "level": 1, "sig_type": 2}),
        json.dumps({"_cmd": "sign", "level": 1, "sig_type": "2"}),
    ]
    make_app().run(
        ["exec"], stdin=io.StringIO("\n".join(lines) + "\n"), stdout=out, stderr=io.StringIO()
    )
    first, second = (json.loads(line) for line in out.getvalue().splitlines())
    assert first["ok"] and first["data"]["sig_type"] == 2
    assert not second["ok"] and second["error"]["code"] == "ARG_ERROR"


def test_mcp_takes_numbers_and_refuses_strings_and_booleans() -> None:
    app = make_app()
    entries = {e.name: e for e in tool_entries(app)}
    good = call_tool(app, entries, "sign", {"level": 1, "sig_type": 1}, env={})
    assert good.ok and isinstance(good.data, dict) and good.data["sig_type"] == 1
    for bad in ("1", True, 7):
        refused = call_tool(app, entries, "sign", {"level": 1, "sig_type": bad}, env={})
        assert refused.exit_code == 2, bad


def test_schema_manifest_and_mcp_publish_an_integer_enum() -> None:
    app = make_app()
    command = next(c for p, c in app.commands.items() if p.value == "sign")
    props = input_schema(command)["properties"]
    assert props["sig_type"] == {
        "type": "integer",
        "enum": [0, 1, 2],
        "description": "Signature type",
    }
    assert props["level"]["type"] == "integer" and props["level"]["enum"] == [1, 3]
    assert {"type": "integer", "enum": [0, 5]} in props["retry"]["anyOf"]
    assert props["ports"]["items"] == {"type": "integer", "enum": [80, 443]}
    manifest = app.manifest()
    spec_validator("manifest-response").validate(manifest)
    entry = manifest["commands"]["sign"]["flags"]["sig-type"]
    assert entry["type"] == "integer" and "enum_values" not in entry
    assert entry["description"] == "Signature type (one of 0, 1, 2)"
    ports = manifest["commands"]["sign"]["flags"]["ports"]
    assert ports["description"] == "Ports (each one of 80, 443)"
    [level] = manifest["commands"]["sign"]["positionals"]
    assert level["type"] == "integer" and level["description"] == "Level (one of 1, 3)"


def register(args_type: type) -> None:
    app = App("bad", version="1.0.0")

    @app.command("go", description="Go", danger_level="safe", exit_codes=())
    def go(args: args_type, ctx: Ctx) -> None:  # type: ignore[valid-type]
        return None

    app.manifest()


def test_a_mix_of_strings_and_integers_is_refused_by_name() -> None:
    @dataclass(frozen=True, slots=True)
    class Mixed:
        mode: Literal["a", 1] = Flag(default="a", description="Mode")

    with pytest.raises(RegistrationError, match="not a mix of both"):
        register(Mixed)


@pytest.mark.parametrize("annotation", [Literal[True, False], Literal[0, True], Literal[b"x"]])
def test_booleans_and_other_values_are_refused(annotation: object) -> None:
    @dataclass(frozen=True, slots=True)
    class Bad:
        mode: annotation = Flag(default=None, description="Mode")  # type: ignore[valid-type]

    with pytest.raises(RegistrationError, match="all strings or all integers"):
        register(Bad)


def test_a_default_outside_the_literal_is_refused() -> None:
    @dataclass(frozen=True, slots=True)
    class WrongDefault:
        sig_type: Literal[0, 1] = Flag(default=2, description="Signature type")  # type: ignore[assignment]

    with pytest.raises(RegistrationError, match=r"default 2 is not one of \[0, 1\]"):
        register(WrongDefault)


def test_a_name_that_reads_as_a_secret_is_not_inferred_one() -> None:
    @dataclass(frozen=True, slots=True)
    class Args:
        token_kind: Literal[1, 2] = Flag(default=1, description="Token kind")

    app = App("tok", version="1.0.0")

    @app.command("go", description="Go", danger_level="safe", exit_codes=())
    def go(args: Args, ctx: Ctx) -> dict[str, int]:
        return {"kind": args.token_kind}

    code, env = run(["go", "--token-kind", "2"], app=app)
    assert code == 0 and env["data"] == {"kind": 2}


@dataclass(frozen=True, slots=True)
class Settings:
    sig_type: Literal[0, 1, 2] = 0


def settings_app() -> App:
    app = App("conf", version="1.0.0", settings=Settings)

    @app.command("show", description="Show", danger_level="safe", exit_codes=())
    def show(args: NoArgs, ctx: Ctx, settings: Settings) -> dict[str, object]:
        return {"sig_type": settings.sig_type}

    return app


def test_a_setting_reads_a_toml_number_or_a_variable(tmp_path: Path) -> None:
    path = tmp_path / "conf.toml"
    path.write_text("sig_type = 2\n")
    code, env = run(["show"], app=settings_app(), env={"CONF_CONFIG": str(path)})
    assert code == 0 and env["data"] == {"sig_type": 2}
    code, env = run(["show"], app=settings_app(), env={"CONF_SIG_TYPE": "1"})
    assert code == 0 and env["data"] == {"sig_type": 1}
    for text in ("sig_type = 3\n", 'sig_type = "1"\n', "sig_type = true\n"):
        path.write_text(text)
        code, env = run(["show"], app=settings_app(), env={"CONF_CONFIG": str(path)})
        assert code == 2 and env["error"]["code"] == "CONFIG_INVALID", text
    code, env = run(["show"], app=settings_app(), env={"CONF_SIG_TYPE": "3"})
    assert code == 2 and env["error"]["code"] == "CONFIG_INVALID"


def test_string_literals_are_unchanged() -> None:
    @dataclass(frozen=True, slots=True)
    class Args:
        mode: Literal["fast", "slow"] = Flag(default="fast", description="Mode")

    app = App("mode", version="1.0.0")

    @app.command("go", description="Go", danger_level="safe", exit_codes=())
    def go(args: Args, ctx: Ctx) -> dict[str, str]:
        return {"mode": args.mode}

    entry = app.manifest()["commands"]["go"]["flags"]["mode"]
    assert entry["type"] == "enum" and entry["enum_values"] == ["fast", "slow"]
    assert entry["description"] == "Mode"
    code, env = run(["go", "--mode", "slow"], app=app)
    assert code == 0 and env["data"] == {"mode": "slow"}
