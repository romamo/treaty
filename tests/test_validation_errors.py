"""Phase 1 reports every validation error in one run (REQ-F-015)."""

import io
import json
from dataclasses import dataclass
from typing import Literal

from conftest import spec_validator

from treaty import App, Arg, Ctx, Flag


@dataclass(frozen=True, slots=True)
class DeployArgs:
    service: str = Arg(description="Service")
    env: Literal["staging", "prod"] = Flag(default="staging", description="Target")
    replicas: int = Flag(default=1, description="Replica count")
    token: str = Flag(default="", description="Deploy token")
    verbose: bool = Flag(default=False, description="Chatty")


def make_app() -> App:
    app = App("deployctl", version="1")

    @app.command("deploy", description="Deploy")
    def deploy(args: DeployArgs, ctx: Ctx) -> dict[str, object]:
        return {"service": args.service, "env": args.env, "replicas": args.replicas}

    return app


def run(
    argv: list[str], *, stdin: str = "", isatty: bool = False, env: dict[str, str] | None = None
) -> tuple[int, dict[str, object], str]:
    out, err = io.StringIO(), io.StringIO()
    code = make_app().run(
        argv, stdin=io.StringIO(stdin), stdout=out, stderr=err, env=env or {}, isatty=isatty
    )
    if isatty:
        return code, {}, err.getvalue()
    return code, json.loads(out.getvalue()), err.getvalue()


def error_of(envelope: dict[str, object]) -> dict[str, object]:
    error = envelope["error"]
    assert isinstance(error, dict)
    return error


def test_two_bad_flags_are_reported_together() -> None:
    code, env, _ = run(["deploy", "api", "--env", "staging2", "--replicas", "-1x"])
    assert code == 2
    error = error_of(env)
    assert error["code"] == "ARG_ERROR" and error["phase"] == "validation"
    assert error["message"] == "Validation failed: 2 errors"
    assert error["context"] == {"error_count": 2, "fields": ["env", "replicas"]}
    errors = error["errors"]
    assert isinstance(errors, list)
    assert [e["field"] for e in errors] == ["env", "replicas"]
    assert errors[0]["message"] == "'env' must be one of staging, prod"
    assert errors[0]["context"]["allowed"] == ["staging", "prod"]
    assert errors[1]["message"] == "'replicas' expects an integer"
    spec_validator("response-envelope").validate(env)


def test_unknown_flag_missing_positional_and_bad_value_in_one_run() -> None:
    code, env, _ = run(["deploy", "--regoin", "eu", "--replicas", "many"])
    errors = error_of(env)["errors"]
    assert isinstance(errors, list)
    assert [e["message"] for e in errors] == [
        "unknown flag '--regoin'",
        "'replicas' expects an integer",
        "missing required: service",
    ]


def test_a_single_error_keeps_its_own_shape_and_lists_itself() -> None:
    code, env, _ = run(["deploy", "api", "--replicas", "x"])
    error = error_of(env)
    assert error["message"] == "'replicas' expects an integer"
    assert error["context"] == {"flag": "replicas", "value": "x"}
    assert error["errors"] == [
        {
            "field": "replicas",
            "message": "'replicas' expects an integer",
            "context": {"flag": "replicas", "value": "x"},
        }
    ]


def test_a_field_that_failed_is_not_also_reported_missing() -> None:
    @dataclass(frozen=True, slots=True)
    class Strict:
        count: int = Flag(description="Required integer")

    app = App("x", version="1")

    @app.command("go", description="Go")
    def go(args: Strict, ctx: Ctx) -> None:
        return None

    out = io.StringIO()
    app.run(["go", "--count", "x"], stdin=io.StringIO(), stdout=out, stderr=io.StringIO(), env={})
    error = error_of(json.loads(out.getvalue()))
    assert [e["message"] for e in error["errors"]] == ["'count' expects an integer"]  # type: ignore[union-attr]


def test_secret_errors_are_collected_with_the_rest() -> None:
    code, env, _ = run(["deploy", "api", "--token", "abc", "--token-from-env", "NOPE", "--env=x"])
    errors = error_of(env)["errors"]
    assert isinstance(errors, list)
    assert [e["field"] for e in errors] == ["token", "env", "token-from-env"]
    assert "abc" not in json.dumps(env)


def test_a_flag_without_a_value_at_the_end_still_stops_at_once() -> None:
    code, env, _ = run(["deploy", "api", "--env", "nope", "--replicas"])
    error = error_of(env)
    assert error["message"] == "'--replicas' needs a value"
    assert error["errors"] == [
        {
            "field": "replicas",
            "message": "'--replicas' needs a value",
            "context": {"flag": "replicas"},
        }
    ]


def test_exec_collects_per_line_field_errors() -> None:
    line = json.dumps({"_cmd": "deploy", "service": 1, "replicas": "3", "bogus": True})
    code, env, _ = run(["exec"], stdin=line + "\n")
    assert code == 1
    errors = error_of(env)["errors"]
    assert isinstance(errors, list)
    assert [e["field"] for e in errors] == ["service", "replicas", "bogus"]


def test_human_mode_lists_every_error() -> None:
    _, _, err = run(["deploy", "api", "--env", "staging2", "--replicas", "-1x"], isatty=True)
    assert "deployctl: ARG_ERROR: Validation failed: 2 errors" in err
    assert "  - env: 'env' must be one of staging, prod" in err
    assert "  - replicas: 'replicas' expects an integer" in err
