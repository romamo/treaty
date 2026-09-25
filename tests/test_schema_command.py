import io
import json

from conftest import spec_validator
from jsonschema import Draft7Validator

from treaty import App


def run_json(app: App, argv: list[str], *, isatty: bool = False) -> tuple[int, dict]:
    out = io.StringIO()
    code = app.run(argv, stdout=out, stderr=io.StringIO(), env={}, isatty=isatty)
    text = out.getvalue()
    if isatty:
        return code, json.loads(text)
    envelope = json.loads(text)
    spec_validator("response-envelope").validate(envelope)
    return code, envelope["data"]


def test_command_schema_has_parameters_and_output_schema(app: App) -> None:
    code, data = run_json(app, ["deploy", "rollback", "--schema"])
    assert code == 0
    assert data["parameters"] == data["flags"]
    assert "service" in data["parameters"] and "timeout" in data["parameters"]
    Draft7Validator.check_schema(data["output_schema"])
    assert data["output_schema"]["title"] == "Plan"
    assert data["danger_level"] == "destructive" and "exit_codes" in data


def test_raw_payload_schema_only_when_supported(app: App) -> None:
    _, rollback = run_json(app, ["deploy", "rollback", "--schema"])
    assert "raw_payload_schema" not in rollback

    from dataclasses import dataclass

    from treaty import Arg, Ctx, Flag

    @dataclass(frozen=True, slots=True)
    class CreateArgs:
        name: str = Arg(description="Name")
        count: int = Flag(default=1, description="How many")

    @app.command("create", description="Create", danger_level="mutating", supports_raw_payload=True)
    def create(args: CreateArgs, ctx: Ctx) -> dict[str, object]:
        return {}

    _, create_schema = run_json(app, ["create", "--schema"])
    raw = create_schema["raw_payload_schema"]
    Draft7Validator.check_schema(raw)
    assert raw["required"] == ["name"]
    assert raw["properties"]["name"] == {"type": "string", "description": "Name"}
    assert raw["properties"]["count"] == {"type": "integer", "description": "How many"}
    # The framework keys a payload may carry are part of the schema it is checked against
    assert set(raw["properties"]) == {"name", "count", "idempotency_key"}
    assert raw["additionalProperties"] is False


def test_root_schema_is_manifest_with_extended_entries(app: App) -> None:
    code, data = run_json(app, ["--schema"])
    assert code == 0 and data["etag"] == app.manifest()["etag"]
    assert all("parameters" in entry for entry in data["commands"].values())
    assert set(data["commands"]) == set(app.manifest()["commands"])


def test_schema_is_json_even_on_a_tty(app: App) -> None:
    code, data = run_json(app, ["deploy", "status", "--schema"], isatty=True)
    assert code == 0 and data["parameters"]["service"]["required"] is True


def test_schema_on_a_group_returns_its_subtree(app: App) -> None:
    code, data = run_json(app, ["deploy", "--schema"])
    assert code == 0 and set(data["commands"]) == {"deploy.rollback", "deploy.status"}
    spec_validator("manifest-response").validate(app.manifest())
