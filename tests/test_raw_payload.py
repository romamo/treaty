import io
import json
from dataclasses import dataclass

from conftest import spec_validator

from treaty import App, Arg, Ctx, Flag


@dataclass(frozen=True, slots=True)
class CreateArgs:
    name: str = Arg(description="Name")
    count: int = Flag(default=1, description="How many")
    tags: tuple[str, ...] = Flag(default=(), description="Labels")


def make_app() -> App:
    app = App("tool", version="1")

    @app.command("create", description="Create", danger_level="mutating", supports_raw_payload=True)
    def create(args: CreateArgs, ctx: Ctx) -> dict[str, object]:
        return {
            "effect": "created",
            "name": args.name,
            "count": args.count,
            "tags": list(args.tags),
        }

    @app.command("plain", description="No raw payload")
    def plain(args: CreateArgs, ctx: Ctx) -> dict[str, object]:
        return {}

    return app


def run_json(argv: list[str]) -> tuple[int, dict]:
    out = io.StringIO()
    code = make_app().run(argv, stdout=out, stderr=io.StringIO(), env={}, isatty=False)
    envelope = json.loads(out.getvalue())
    spec_validator("response-envelope").validate(envelope)
    return code, envelope


def test_raw_payload_equivalent_to_flags() -> None:
    _, via_flags = run_json(["create", "foo", "--count", "3", "--tags", "a"])
    code, via_payload = run_json(
        ["create", "--raw-payload", '{"name": "foo", "count": 3, "tags": ["a"]}']
    )
    assert code == 0 and via_payload["data"] == via_flags["data"]


def test_raw_payload_invalid_json_is_field_level_error() -> None:
    code, env = run_json(["create", "--raw-payload", "{not json"])
    assert code == 2 and env["error"]["code"] == "ARG_ERROR"
    assert (
        env["error"]["context"]["flag"] == "raw-payload" and "position" in env["error"]["context"]
    )
    code, env = run_json(["create", "--raw-payload", '{"name": "x", "count": "three"}'])
    assert code == 2 and env["error"]["context"]["field"] == "count"
    code, env = run_json(["create", "--raw-payload", "[]"])
    assert code == 2


def test_raw_payload_cannot_combine_with_flags() -> None:
    code, env = run_json(["create", "foo", "--raw-payload", '{"name": "foo"}'])
    assert (
        code == 2
        and env["error"]["message"] == "Cannot combine --raw-payload with individual flags"
    )
    assert env["error"]["context"]["also_given"] == ["name"]


def test_raw_payload_rejected_when_not_supported() -> None:
    code, env = run_json(["plain", "--raw-payload", "{}"])
    assert code == 2 and env["error"]["errors"][0]["context"]["flag"] == "raw-payload"


def test_raw_payload_advertised_in_manifest() -> None:
    commands = make_app().manifest()["commands"]
    assert commands["create"]["flags"]["raw-payload"]["type"] == "string"
    assert "raw-payload" not in commands["plain"]["flags"]
    spec_validator("manifest-response").validate(make_app().manifest())
