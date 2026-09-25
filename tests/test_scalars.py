"""Custom scalars: app.scalar(...) lets a domain class annotate fields and outputs."""

import io
import json
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import spec_validator

from treaty import App, Arg, Ctx, Flag, RegistrationError, SchemaError

_ID = re.compile(r"[a-z][a-z0-9-]{0,62}")


@dataclass(frozen=True, slots=True)
class ResourceId:
    value: str

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.value):
            raise ValueError(f"invalid resource id: {self.value!r}")

    @classmethod
    def from_boundary(cls, value: object) -> ResourceId:
        if not isinstance(value, str):
            raise TypeError(f"resource id must be a string, got {type(value).__name__}")
        return cls(value)


@dataclass(frozen=True, slots=True)
class TcpPort:
    value: int

    def __post_init__(self) -> None:
        if self.value % 2:
            raise ValueError("only even ports in this test")


class Release:
    """Not a dataclass: serialization must be given explicitly"""

    def __init__(self, tag: str) -> None:
        self.tag = tag

    @classmethod
    def parse(cls, raw: str) -> Release:
        return cls(raw)


@dataclass(frozen=True, slots=True)
class DeployArgs:
    service: ResourceId = Arg(description="Service to deploy")
    port: TcpPort = Flag(default=TcpPort(8080), description="Listen port")
    release: Release | None = Flag(default=None, description="Release tag")
    peers: tuple[ResourceId, ...] = Flag(default=(), description="Peer services")
    dry_run: bool = Flag(default=False, description="Preview only")


@dataclass(frozen=True, slots=True)
class Receipt:
    effect: str
    service: ResourceId
    port: TcpPort
    release: Release | None
    peers: tuple[ResourceId, ...]


def scalar_app() -> App:
    app = App("fleet", version="1")
    app.scalar(ResourceId, parse=ResourceId.from_boundary, pattern=_ID.pattern)
    app.scalar(TcpPort, parse=TcpPort, base=int, minimum=1, maximum=65535)
    app.scalar(Release, parse=Release.parse, serialize=lambda r: r.tag)

    @app.command(
        "deploy", description="Deploy a service", danger_level="mutating", supports_raw_payload=True
    )
    def deploy(args: DeployArgs, ctx: Ctx) -> Receipt:
        assert isinstance(args.service, ResourceId)
        assert isinstance(args.port, TcpPort)
        assert all(isinstance(p, ResourceId) for p in args.peers)
        effect = "would_update" if args.dry_run else "updated"
        return Receipt(effect, args.service, args.port, args.release, args.peers)

    return app


def run(argv: list[str], stdin: str = "") -> tuple[int, dict]:
    out = io.StringIO()
    code = scalar_app().run(
        argv, stdin=io.StringIO(stdin), stdout=out, stderr=io.StringIO(), env={}, isatty=False
    )
    return code, json.loads(out.getvalue())


def errors_of(envelope: dict) -> dict[str, dict]:
    return {e["field"]: e for e in envelope["error"]["errors"]}


# Parsing


def test_argv_values_reach_the_handler_as_instances_and_serialize_back() -> None:
    code, env = run(
        ["deploy", "api", "--port", "9000", "--release", "v2", "--peers", "db", "--peers", "cache"]
    )
    assert code == 0
    assert env["data"] == {
        "effect": "updated",
        "service": "api",
        "port": 9000,
        "release": "v2",
        "peers": ["db", "cache"],
    }


def test_defaults_of_scalar_type_are_used_and_serialized() -> None:
    code, env = run(["deploy", "api"])
    assert code == 0
    assert env["data"]["port"] == 8080
    assert env["data"]["release"] is None


def test_pattern_violation_is_a_field_error_with_the_pattern() -> None:
    code, env = run(["deploy", "Bad_Name"])
    assert code == 2
    error = errors_of(env)["service"]
    assert error["message"] == "value for 'service' does not match pattern"
    assert error["context"]["pattern"] == _ID.pattern
    assert error["context"]["scalar"] == "ResourceId"
    assert error["context"]["value"] == "Bad_Name"


def test_parser_value_error_is_a_field_error_with_its_cause() -> None:
    code, env = run(["deploy", "api", "--port", "8081"])
    assert code == 2
    error = errors_of(env)["port"]
    assert (
        error["message"] == "value for 'port' is not a valid TcpPort: only even ports in this test"
    )
    assert error["context"]["cause"] == "only even ports in this test"


def test_bounds_are_checked_before_the_parser() -> None:
    code, env = run(["deploy", "api", "--port", "0"])
    assert code == 2
    error = errors_of(env)["port"]
    assert error["message"] == "value for 'port' must be at least 1"
    assert error["context"]["minimum"] == 1
    code, env = run(["deploy", "api", "--port", "70000"])
    assert errors_of(env)["port"]["context"]["maximum"] == 65535


def test_base_type_failure_is_reported_as_the_base_type() -> None:
    code, env = run(["deploy", "api", "--port", "http"])
    assert code == 2
    assert errors_of(env)["port"]["message"] == "'port' expects an integer"


def test_all_scalar_errors_are_reported_in_one_run() -> None:
    code, env = run(["deploy", "Bad", "--port", "0", "--peers", "ok", "--peers", "NO"])
    assert code == 2
    assert env["error"]["message"] == "Validation failed: 3 errors"
    assert set(errors_of(env)) == {"service", "port", "peers"}


def test_raw_payload_route_applies_the_same_checks() -> None:
    payload = json.dumps({"service": "api", "port": 9000, "peers": ["db"]})
    code, env = run(["deploy", "--raw-payload", payload])
    assert code == 0
    assert env["data"]["peers"] == ["db"]
    payload = json.dumps({"service": "Bad", "port": 8081})
    code, env = run(["deploy", "--raw-payload", payload])
    assert code == 2
    assert set(errors_of(env)) == {"service", "port"}


def test_raw_payload_base_type_mismatch_is_reported_as_the_base_type() -> None:
    code, env = run(["deploy", "--raw-payload", json.dumps({"service": 7})])
    assert code == 2
    assert errors_of(env)["service"]["message"] == "'service' expects a string"


def test_exec_route_parses_scalars() -> None:
    line = json.dumps({"_cmd": "deploy", "service": "api", "_opts": {"port": 9000}})
    code, env = run(["exec"], stdin=line + "\n")
    assert code == 0
    assert env["data"]["port"] == 9000


def test_idempotency_key_fingerprints_scalar_arguments(tmp_path: Path) -> None:
    app = scalar_app()
    app.state_dir = tmp_path

    def go(argv: list[str]) -> dict:
        out = io.StringIO()
        app.run(argv, stdout=out, stderr=io.StringIO(), env={}, isatty=False)
        return json.loads(out.getvalue())

    first = go(["deploy", "api", "--idempotency-key", "k1"])
    assert first["data"]["effect"] == "updated"
    again = go(["deploy", "api", "--idempotency-key", "k1"])
    assert again["data"]["effect"] == "noop"
    assert again["meta"]["idempotency_hit"] is True
    other = go(["deploy", "web", "--idempotency-key", "k1"])
    assert other["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


# Manifest and schema


def test_manifest_carries_pattern_and_base_types_and_validates() -> None:
    app = scalar_app()
    manifest = app.manifest()
    spec_validator("manifest-response").validate(manifest)
    flags = manifest["commands"]["deploy"]["flags"]
    assert flags["service"]["type"] == "string"
    assert flags["service"]["pattern"] == _ID.pattern
    assert flags["port"] == {
        "type": "integer",
        "required": False,
        "description": "Listen port",
        "default": 8080,
    }
    assert flags["peers"]["type"] == "array"
    assert flags["peers"]["pattern"] == _ID.pattern


def test_schema_output_carries_constraints_on_args_and_output() -> None:
    code, env = run(["deploy", "--schema"])
    assert code == 0
    args_schema = env["data"]["raw_payload_schema"]["properties"]
    assert args_schema["service"] == {
        "type": "string",
        "title": "ResourceId",
        "pattern": _ID.pattern,
    }
    assert args_schema["port"] == {
        "type": "integer",
        "title": "TcpPort",
        "minimum": 1,
        "maximum": 65535,
    }
    assert args_schema["release"] == {
        "anyOf": [{"type": "string", "title": "Release"}, {"type": "null"}]
    }
    output = env["data"]["output_schema"]["properties"]
    assert output["service"]["pattern"] == _ID.pattern
    assert output["peers"]["items"]["title"] == "ResourceId"


def test_pattern_type_preset_reaches_manifest_and_schema() -> None:
    @dataclass(frozen=True, slots=True)
    class RunId:
        value: str

    @dataclass(frozen=True, slots=True)
    class ShowArgs:
        run: RunId = Arg(description="Run to show")

    app = App("runs", version="1")
    app.scalar(RunId, parse=RunId, pattern_type="uuid")

    @app.command("show", description="Show a run")
    def show(args: ShowArgs, ctx: Ctx) -> dict[str, RunId]:
        return {"run": args.run}

    entry = app.manifest()["commands"]["show"]["flags"]["run"]
    assert entry["pattern_type"] == "uuid"
    assert "pattern" not in entry
    spec_validator("manifest-response").validate(app.manifest())
    out = io.StringIO()
    app.run(["show", "--schema"], stdout=out, stderr=io.StringIO(), env={}, isatty=False)
    schema = json.loads(out.getvalue())["data"]["output_schema"]
    assert schema["additionalProperties"] == {"type": "string", "title": "RunId", "format": "uuid"}


# Registration


def test_unregistered_class_is_a_registration_time_schema_error() -> None:
    @dataclass(frozen=True, slots=True)
    class Args:
        service: ResourceId = Arg(description="Service")

    app = App("fleet", version="1")
    with pytest.raises(SchemaError, match="register a class with app.scalar"):

        @app.command("deploy", description="Deploy")
        def deploy(args: Args, ctx: Ctx) -> None:
            return None


def test_unregistered_dataclass_in_output_stays_a_nested_object() -> None:
    @dataclass(frozen=True, slots=True)
    class Args:
        name: str = Arg(description="Name")

    @dataclass(frozen=True, slots=True)
    class Out:
        service: ResourceId

    app = App("fleet", version="1")

    @app.command("deploy", description="Deploy")
    def deploy(args: Args, ctx: Ctx) -> Out:
        return Out(ResourceId(args.name))

    out = io.StringIO()
    assert app.run(["deploy", "api"], stdout=out, stderr=io.StringIO(), env={}, isatty=False) == 0
    assert json.loads(out.getvalue())["data"] == {"service": {"value": "api"}}
    schema = app.commands[next(iter(p for p in app.commands if p.value == "deploy"))].output_schema
    assert schema["properties"]["service"]["type"] == "object"


def test_field_pattern_is_refused_on_a_scalar_field() -> None:
    @dataclass(frozen=True, slots=True)
    class Args:
        service: ResourceId = Arg(description="Service", pattern="x+")

    app = App("fleet", version="1")
    app.scalar(ResourceId, parse=ResourceId.from_boundary)
    with pytest.raises(RegistrationError, match="pattern= is not allowed"):

        @app.command("deploy", description="Deploy")
        def deploy(args: Args, ctx: Ctx) -> None:
            return None


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"cls": str, "parse": str}, "built-in scalar type"),
        ({"cls": ResourceId, "parse": ResourceId, "base": bool}, "base must be str, int, or float"),
        (
            {"cls": ResourceId, "parse": ResourceId, "pattern": "a", "pattern_type": "uuid"},
            "mutually exclusive",
        ),
        (
            {"cls": ResourceId, "parse": ResourceId, "pattern_type": "filepath"},
            "pattern_type must be one of",
        ),
        ({"cls": TcpPort, "parse": TcpPort, "base": int, "pattern": "a"}, "needs base=str"),
        ({"cls": ResourceId, "parse": ResourceId, "minimum": 1}, "need base=int or base=float"),
        (
            {"cls": TcpPort, "parse": TcpPort, "base": int, "minimum": 9, "maximum": 1},
            "minimum exceeds maximum",
        ),
        ({"cls": ResourceId, "parse": ResourceId, "pattern": "("}, None),
        ({"cls": Release, "parse": Release.parse, "base": int}, "pass serialize="),
    ],
)
def test_invalid_registrations_fail_fast(kwargs: dict, match: str | None) -> None:
    app = App("fleet", version="1")
    with pytest.raises((RegistrationError, re.error), match=match):
        app.scalar(**kwargs)


def test_duplicate_registration_is_refused() -> None:
    app = App("fleet", version="1")
    app.scalar(ResourceId, parse=ResourceId.from_boundary)
    with pytest.raises(RegistrationError, match="already a registered scalar"):
        app.scalar(ResourceId, parse=ResourceId.from_boundary)
