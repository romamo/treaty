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
        "audit-log",
        "cleanup",
        "completion",
        "doctor",
        "generate-skills",
        "manifest",
        "mcp-validate",
        "status",
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
    # 6: idempotency key reuse; 4, shared by every command: a stray input(), or an
    # unusable state directory or record; 12, a network command's ctx.http failure
    assert set(entry["exit_codes"]) == {"6", "12", "79", "80"}
    assert flags["idempotency-key"]["type"] == "string"
    assert set(manifest["exit_codes"]) == {"0", "1", "2", "4", "10", "130", "141", "143"}
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
        "would_affect",
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

    @app.command("ping", description="Reply", danger_level="safe", exit_codes=())
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
        "12",
        "79",
        "80",
        "130",
        "141",
        "143",
    }


def test_builtins_are_marked_and_app_commands_are_not(app: App) -> None:
    manifest = app.manifest()
    spec_validator("manifest-response").validate(manifest)
    assert manifest["schema_version"] == "3.1"
    marked = {path for path, entry in manifest["commands"].items() if entry.get("builtin")}
    assert marked == {p.value for p in app.builtins}
    assert "manifest" in marked and "audit-log" in marked
    # REQ-O-041: an application command omits the marker, which reads as false
    assert "builtin" not in manifest["commands"]["deploy.rollback"]
    assert "builtin" not in manifest["commands"]["deploy.status"]


def test_a_builtins_subcommands_are_marked(app: App) -> None:
    from treaty._manifest import build_manifest
    from treaty._values import CommandPath

    # No built-in is a group today; the marker follows the registered path set, so a
    # built-in group's children carry it too
    group = frozenset({CommandPath("deploy.rollback"), CommandPath("deploy.status")})
    manifest = build_manifest(
        app.commands, app.exits, app.formats, app.name, builtins=app.builtins | group
    )
    spec_validator("manifest-response").validate(manifest)
    assert manifest["commands"]["deploy.rollback"]["builtin"] is True
    assert manifest["commands"]["deploy.status"]["builtin"] is True


def test_an_app_command_taking_a_builtins_name_is_not_marked(app: App) -> None:
    from treaty import Ctx, NoArgs

    assert app.manifest()["commands"]["doctor"]["builtin"] is True

    @app.command(
        "doctor", description="Check the deploy targets", danger_level="safe", exit_codes=()
    )
    def doctor(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"targets": "ok"}

    manifest = app.manifest()
    spec_validator("manifest-response").validate(manifest)
    assert manifest["commands"]["doctor"]["description"] == "Check the deploy targets"
    assert "builtin" not in manifest["commands"]["doctor"]
    assert manifest["commands"]["status"]["builtin"] is True


def test_schema_of_one_command_carries_the_marker(app: App) -> None:
    assert run_json(app, ["doctor", "--schema"])[1]["data"]["builtin"] is True
    assert "builtin" not in run_json(app, ["deploy", "rollback", "--schema"])[1]["data"]
    subtree = run_json(app, ["deploy", "--schema"])[1]["data"]
    assert subtree["schema_version"] == "3.1"
    assert not any("builtin" in entry for entry in subtree["commands"].values())
