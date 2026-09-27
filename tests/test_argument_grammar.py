"""Argument grammar: REQ-C-020, C-026, C-027, F-049, F-059, F-067, F-075, O-006, O-009"""

import io
import json
from dataclasses import dataclass
from typing import Any

import pytest
from conftest import spec_validator

from treaty import App, Arg, Ctx, Flag, RegistrationError
from treaty._audit import audit


def run(
    app: App, argv: list[str], *, stdin: str | None = None, env: dict[str, str] | None = None
) -> tuple[int, dict[str, Any]]:
    out = io.StringIO()
    code = app.run(
        argv,
        stdin=None if stdin is None else io.StringIO(stdin),
        stdout=out,
        stderr=io.StringIO(),
        env=env or {},
        isatty=False,
    )
    envelope = json.loads(out.getvalue())
    spec_validator("response-envelope").validate(envelope)
    return code, envelope


def findings(app: App, rule: str) -> list[Any]:
    [result] = [r for r in audit(app, "t", limit=3).rules if r.id == rule]
    return list(result.findings)


# REQ-C-020


@dataclass(frozen=True, slots=True)
class DeployArgs:
    cluster_id: str = Arg(description="Target cluster", pattern_type="alphanumeric_id")
    version: str = Flag(default="1.0.0", description="Version", pattern_type="semver")
    ticket: str | None = Flag(default=None, description="Change ticket", pattern="[a-z0-9-]{3,64}")


def id_app() -> App:
    app = App("ids", version="1.0.0")

    @app.command("deploy", description="Deploy", danger_level="safe", exit_codes=())
    def deploy(args: DeployArgs, ctx: Ctx) -> dict[str, str]:
        return {"cluster": args.cluster_id}

    return app


@pytest.mark.parametrize("bad", ["prod/east", "prod.east", "prod?x", "prod#x", "prod%2F"])
def test_an_alphanumeric_id_argument_rejects_slash_dot_question_hash_and_percent(bad: str) -> None:
    code, envelope = run(id_app(), ["deploy", bad])
    assert code == 2 and envelope["error"]["context"]["pattern_type"] == "alphanumeric_id"
    assert run(id_app(), ["deploy", "prod-east_1"])[0] == 0


def test_a_custom_regex_argument_rejects_inputs_that_do_not_match() -> None:
    code, envelope = run(id_app(), ["deploy", "prod", "--ticket", "AB"])
    assert code == 2 and envelope["error"]["context"]["pattern"] == "[a-z0-9-]{3,64}"
    assert run(id_app(), ["deploy", "prod", "--ticket", "chg-42"])[0] == 0


def test_a_resource_id_field_with_no_pattern_triggers_a_registration_warning() -> None:
    @dataclass(frozen=True, slots=True)
    class Loose:
        user_id: str = Flag(description="User")
        name: str = Flag(default="x", description="Name")

    app = App("loose", version="1.0.0")

    @app.command("get", description="Get", danger_level="safe", exit_codes=())
    def get(args: Loose, ctx: Ctx) -> dict[str, str]:
        return {}

    [finding] = findings(app, "id-pattern")
    assert finding.severity == "warning" and 'pattern_type="alphanumeric_id"' in finding.fix
    assert findings(id_app(), "id-pattern") == []


def test_pattern_validation_failures_exit_2_naming_the_argument_and_the_pattern() -> None:
    code, envelope = run(id_app(), ["deploy", "prod", "--version", "not-semver"])
    context = envelope["error"]["context"]
    assert code == 2 and context["flag"] == "version" and context["pattern_type"] == "semver"
    assert context["pattern"].startswith("^") and "version" in envelope["error"]["message"]


def test_pattern_type_is_listed_in_the_manifest_and_checked_on_json_input() -> None:
    app = id_app()
    entry = app.manifest()["commands"]["deploy"]
    assert entry["flags"]["cluster-id"]["pattern_type"] == "alphanumeric_id"
    assert entry["flags"]["version"]["pattern_type"] == "semver"
    envelope = app.call("deploy", {"cluster_id": "a/b"}, env={})
    assert envelope.exit_code == 2


def test_pattern_type_is_refused_where_it_cannot_apply() -> None:
    with pytest.raises(RegistrationError, match="mutually exclusive"):
        Flag(description="x", pattern="a", pattern_type="uuid")
    with pytest.raises(RegistrationError, match="not one of"):
        Flag(description="x", pattern_type="filepath")

    @dataclass(frozen=True, slots=True)
    class Counted:
        n: int = Flag(description="N", pattern_type="uuid")

    app = App("bad", version="1.0.0")
    with pytest.raises(RegistrationError, match="pattern_type is for str fields"):

        @app.command("n", description="N", danger_level="safe", exit_codes=())
        def n(args: Counted, ctx: Ctx) -> dict[str, str]:
            return {}
