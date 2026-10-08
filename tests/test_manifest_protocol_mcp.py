"""ManifestResponse 3.16 and 3.17 keys at 3.19 (#362): ``mcp serve`` says ``stdout:
protocol`` and ``protocol: mcp-stdio`` (REQ-C-032), and every command no MCP server offers
says ``mcp: false``, since an absent key claims stdout carries envelopes and a server may
offer the command"""

import io
import json
from dataclasses import dataclass
from typing import Any

import pytest
from conftest import spec_validator

from treaty import App, Ctx, Flag, McpServe, NoArgs
from treaty._audit import schema_lock
from treaty._manifest import SCHEMA_VERSION
from treaty._mode import Format
from treaty._tools import tool_entries
from treaty._values import InvalidValue, StdioProtocol


@dataclass(frozen=True, slots=True)
class ServeArgs:
    project: str = Flag(default=".", description="Project directory")


def make_app() -> App:
    app = App("srv", version="1.0.0", mcp=McpServe(args=ServeArgs))
    # A format beyond the defaults: every envelope command lists it in output_formats
    app.format(Format.CSV, render=lambda data: "")

    @app.command("ping", description="Answer pong", danger_level="safe", exit_codes=())
    def ping(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"pong": "pong"}

    @app.command(
        "approve", description="Approve", danger_level="mutating", exit_codes=(), mcp=False
    )
    def approve(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    @app.command("git", description="Run git", danger_level="mutating", exit_codes=(),
                 passthrough=True, option_placement="strict")  # fmt: skip
    def git(args: NoArgs, ctx: Ctx) -> int:
        return 0

    return app


def manifest(app: App) -> dict[str, Any]:
    built = app.manifest()
    spec_validator("manifest-response").validate(built)
    assert built["schema_version"] == SCHEMA_VERSION == "3.19"
    return built


def schema(app: App, argv: list[str]) -> dict[str, Any]:
    out = io.StringIO()
    code = app.run([*argv, "--schema"], stdout=out, stderr=io.StringIO(), env={}, isatty=False)
    assert code == 0
    envelope = json.loads(out.getvalue())
    spec_validator("response-envelope").validate(envelope)
    data: dict[str, Any] = envelope["data"]
    return data


def test_mcp_serve_declares_its_stdout_a_protocol() -> None:
    entry = manifest(make_app())["commands"]["mcp.serve"]
    assert entry["stdout"] == "protocol"
    assert entry["protocol"] == "mcp-stdio"
    assert entry["mcp"] is False  # the server is not one of its own tools
    # The schema rejects these beside stdout: protocol, which has no envelope to describe
    for key in ("output_schema", "output_formats", "output_media_types", "stdin", "stderr"):
        assert key not in entry, key


def test_an_envelope_command_keeps_its_output_keys_and_no_protocol() -> None:
    entry = manifest(make_app())["commands"]["ping"]
    assert "stdout" not in entry and "protocol" not in entry and "mcp" not in entry
    assert entry["output_schema"]["type"] == "object"
    assert entry["output_formats"] == ["csv"]


def test_mcp_serve_schema_says_the_same() -> None:
    data = schema(make_app(), ["mcp", "serve"])
    assert (data["stdout"], data["protocol"], data["mcp"]) == ("protocol", "mcp-stdio", False)
    assert "output_schema" not in data and "output_formats" not in data


def test_a_command_registered_mcp_false_says_mcp_false() -> None:
    commands = manifest(make_app())["commands"]
    assert commands["approve"]["mcp"] is False
    # The #281 marker stays beside the key
    assert commands["approve"]["description"].endswith(" (not an MCP tool)")
    assert schema(make_app(), ["approve"])["mcp"] is False


def test_mcp_false_marks_exactly_the_commands_no_tool_list_offers() -> None:
    app = make_app()
    commands = manifest(app)["commands"]
    kept_off = {path for path, entry in commands.items() if entry.get("mcp") is False}
    tools = {e.path.value for e in tool_entries(app)}
    assert kept_off.isdisjoint(tools)
    assert kept_off | tools == set(commands)
    # exec, completion, and passthrough are never served either
    assert {"mcp.serve", "approve", "git", "exec", "completion", "cleanup"} <= kept_off
    assert "ping" in tools and "manifest" in tools


def test_an_app_command_shadowing_a_builtin_is_offered_and_unmarked() -> None:
    app = App("x", version="1.0.0")

    @app.command("completion", description="Complete", danger_level="safe", exit_codes=())
    def completion(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    assert "mcp" not in manifest(app)["commands"]["completion"]
    assert "completion" in {e.path.value for e in tool_entries(app)}


def test_without_mcp_serve_no_command_has_a_protocol() -> None:
    app = App("plain", version="1.0.0")
    commands = manifest(app)["commands"]
    assert "mcp.serve" not in commands
    assert not any("stdout" in e or "protocol" in e for e in commands.values())


def test_the_schema_lock_records_only_versions_and_output_schemas() -> None:
    # The lock reads the registry, not the manifest: the new keys add nothing to lock
    lock = schema_lock(make_app())["commands"]
    assert isinstance(lock, dict)
    assert "approve" in lock and "output_schema" in lock["approve"]
    assert set(lock["approve"]) == {"schema_version", "output_schema"}


@pytest.mark.parametrize("name", ["", "MCP", "mcp_stdio", "-lsp", "lsp-", "a--b"])
def test_a_protocol_name_is_lowercase_kebab_case(name: str) -> None:
    with pytest.raises(InvalidValue):
        StdioProtocol(name)
    assert StdioProtocol("mcp-stdio").value == "mcp-stdio"
