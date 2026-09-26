import io
import json

from conftest import spec_validator

from treaty import App


def run_json(app: App, argv: list[str]) -> tuple[int, dict]:
    out = io.StringIO()
    code = app.run(argv, stdout=out, stderr=io.StringIO(), env={}, isatty=False)
    return code, json.loads(out.getvalue())


def test_manifest_validates_against_spec(app: App) -> None:
    code, envelope = run_json(app, ["manifest"])
    assert code == 0
    spec_validator("response-envelope").validate(envelope)
    manifest = envelope["data"]
    spec_validator("manifest-response").validate(manifest)
    assert set(manifest["commands"]) == {
        "manifest",
        "version",
        "exec",
        "deploy.rollback",
        "deploy.status",
    }


def test_rollback_entry_contents(app: App) -> None:
    manifest = app.manifest()
    entry = manifest["commands"]["deploy.rollback"]
    assert entry["danger_level"] == "destructive"
    assert entry["required_scopes"] == ["deploy:write"]
    assert entry["has_network_io"] is True
    flags = entry["flags"]
    assert flags["service"] == {
        "type": "string",
        "required": True,
        "description": "Service name as shown in deployctl ls",
    }
    assert (
        flags["to"]["required"] is False
        and "default" not in flags["to"]
        and flags["to"]["short"] == "t"
    )
    assert flags["strategy"] == {
        "type": "enum",
        "required": False,
        "description": "Rollout strategy",
        "default": "safe",
        "enum_values": ["fast", "safe"],
    }
    assert flags["tags"]["type"] == "array" and flags["tags"]["default"] == []
    # 6: idempotency key reuse; 4: unusable state directory or record
    assert set(entry["exit_codes"]) == {"4", "6", "79", "80"}
    assert flags["idempotency-key"]["type"] == "string"
    assert set(manifest["exit_codes"]) == {"0", "1", "2", "10", "130", "141", "143"}
    assert manifest["exit_codes"]["143"] == {
        "name": "CANCELLED_SIGTERM",
        "description": "Cancelled by SIGTERM; external state may be partially modified",
        "retryable": False,
        "side_effects": "partial",
    }
    assert entry["exit_codes"]["80"]["retryable"] is True
    assert entry["output_schema"]["required"] == [
        "effect",
        "service",
        "release",
        "strategy",
        "replicas",
        "tags",
        "dry_run",
    ]
    assert entry["examples"] == [
        {
            "description": "Plan a rollback",
            "command": "deployctl deploy rollback api --to 1.3.9 --dry-run",
        }
    ]


def test_etag_is_stable_and_changes_with_registrations(app: App) -> None:
    first = app.manifest()["etag"]
    assert first == app.manifest()["etag"]

    from treaty import Ctx, NoArgs

    @app.command("ping", description="Reply")
    def ping(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"pong": "yes"}

    assert app.manifest()["etag"] != first


def test_schema_entry_keeps_full_exit_table(app: App) -> None:
    code, envelope = run_json(app, ["deploy", "rollback", "--schema"])
    assert code == 0
    assert set(envelope["data"]["exit_codes"]) == {
        "0",
        "1",
        "2",
        "4",
        "6",
        "10",
        "79",
        "80",
        "130",
        "141",
        "143",
    }
