"""Arguments ``mcp serve`` fixes for the run (#285): ``McpServe(bind=bind)`` fills a field
of every served command tool that has it, the field leaves the tool's input schema, and a
call that passes it anyway, by any spelling, is refused as an unknown field. A bind that
names what no served command has, a secret, or a value its field refuses fails before
serving."""

import io
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_mcp_serve import Server

from treaty import App, McpServe, RegistrationError
from treaty._mcp import call_tool, tool_entries
from treaty._mcp_serve import NO_BINDINGS, Bindings, bound_values
from treaty._values import CommandPath

pytest.importorskip("mcp")

HERE = Path(__file__).resolve().parent
BINDCTL = HERE / "fixture_mcp_bind_app.py"
PROJECT = "/srv/fleet"
OTHER = "/srv/other"
ENV = {"BINDCTL_REGION_NAME": "us-west-2", "BINDCTL_REGION": "ap-south-1"}

# Every way a call might try to reach a bound field: its name and other spellings, the
# command line's, another command's selector, and a payload nested in the arguments
OVERRIDES: list[dict[str, object]] = [
    {"project": OTHER},
    {"project": PROJECT},  # even the bound value itself: the field is not the call's
    {"project": None},
    {"--project": OTHER},
    {"-project": OTHER},
    {"--project=" + OTHER: True},
    {"project_": OTHER},
    {"project-": OTHER},
    {"Project": OTHER},
    {"PROJECT": OTHER},
    {"project_from_env": "HOME"},
    {"project_from_file": "/etc/hostname"},
    {"inventory": ["prod"]},
    {"options": {"parallel": 64}},
    {"region": "us-west-2"},
    {"_cmd": "deploy", "project": OTHER},
    {"_cmd": "approve"},
    {"raw_payload": {"project": OTHER}},
    {"raw_payload": json.dumps({"project": OTHER})},
    {"arguments": {"project": OTHER}},
    {"args": {"project": OTHER}},
    {"payload": {"project": OTHER}},
]


def _bindctl() -> App:
    sys.path.insert(0, str(HERE))
    try:
        from fixture_mcp_bind_app import app
    finally:
        sys.path.remove(str(HERE))
    return app


def _result(answer: dict[str, object]) -> dict[str, object]:
    result = answer.get("result")
    assert isinstance(result, dict), answer
    return result


def _content(result: dict[str, object]) -> dict[str, object]:
    content = result["structuredContent"]
    assert isinstance(content, dict)
    return content


def _call(server: Server, name: str, arguments: dict[str, object]) -> dict[str, object]:
    return _result(server.request("tools/call", {"name": name, "arguments": arguments}))


@pytest.fixture
def started() -> Iterator[list[Server]]:
    servers: list[Server] = []
    yield servers
    for server in servers:
        if server.proc.poll() is None:
            server.proc.kill()
            server.wait()


def _serve(started: list[Server], tmp: Path, *args: str) -> Server:
    # Server logs to tmp/audit.jsonl; the bound app reads its own variable
    env = {**ENV, "BINDCTL_AUDIT_LOG": str(tmp / "audit.jsonl")}
    server = Server(list(args), tmp, env=env, script=BINDCTL)
    started.append(server)
    return server


# Over the real stdio server


def test_a_bound_field_is_fixed_and_cannot_be_passed_by_any_spelling(
    tmp_path: Path, started: list[Server]
) -> None:
    server = _serve(started, tmp_path, "--project", PROJECT)
    server.initialize()
    tools = _result(server.request("tools/list"))["tools"]
    assert isinstance(tools, list)
    schemas = {t["name"]: t["inputSchema"] for t in tools}
    deploy = schemas["deploy"]
    assert {"project", "inventory", "options", "region"}.isdisjoint(deploy["properties"])
    assert deploy["required"] == ["target"]  # the positional project left required too
    assert "project" not in schemas["audit"]["properties"]
    assert "required" not in schemas["audit"]
    # A provided tool is no command: its own project property stays
    assert "project" in schemas["look"]["properties"]

    ran = _call(server, "deploy", {"target": "web1"})
    assert ran["isError"] is False
    # The bound region wins over both variables that would fill it (#194)
    assert _content(ran)["data"] == {
        "project": PROJECT,
        "target": "web1",
        "inventory": ["db", "web"],
        "options": {"parallel": 2},
        "region": "eu-west-1",
    }
    for arguments in OVERRIDES:
        refused = _call(server, "deploy", {"target": "web1", **arguments})
        assert refused["isError"] is True, arguments
        content = _content(refused)
        assert content["data"] is None, arguments
        error = content["error"]
        assert isinstance(error, dict)
        assert error["code"] == "ARG_ERROR", arguments
        each = error.get("errors") or [error]
        assert all("unknown field" in e["message"].lower() for e in each), arguments
    audited = _call(server, "audit", {})
    assert _content(audited)["data"] == {"project": PROJECT, "policy": "lenient", "reason": None}
    looked = _call(server, "look", {"project": OTHER})
    assert _content(looked)["data"] == {"seen": {"project": OTHER}}
    assert _call(server, "ping", {})["isError"] is False
    assert server.close() == 0
    # The audit log records the bound values a call ran with, and never an override
    entries = [e for e in server.audit_entries() if e["command"] in ("deploy", "audit")]
    ran = [e["args"] for e in entries if e["exit_code"] == 0]
    assert len(ran) == 2 and all(a["project"] == PROJECT for a in ran)
    assert len(entries) == 2 + len(OVERRIDES)
    assert OTHER not in json.dumps(entries)


def test_a_rule_naming_a_bound_field_sees_the_bound_value(
    tmp_path: Path, started: list[Server]
) -> None:
    server = _serve(started, tmp_path, "--mode", "strict")
    server.initialize()
    missing = _content(_call(server, "audit", {}))
    error = missing["error"]
    assert isinstance(error, dict)
    assert error["code"] == "ARG_ERROR" and error["context"]["flag"] == "reason"
    given = _content(_call(server, "audit", {"reason": "quarterly"}))
    assert given["data"] == {"project": PROJECT, "policy": "strict", "reason": "quarterly"}
    loosened = _content(_call(server, "audit", {"policy": "lenient", "reason": "r"}))
    assert loosened["data"] is None
    assert loosened["error"]["context"]["field"] == "policy"  # type: ignore[index]
    assert server.close() == 0


def test_a_bound_value_goes_through_post_init_on_each_call(
    tmp_path: Path, started: list[Server]
) -> None:
    server = _serve(started, tmp_path, "--mode", "escape")
    server.initialize()
    for _ in range(2):
        refused = _content(_call(server, "deploy", {"target": "web1"}))
        error = refused["error"]
        assert isinstance(error, dict)
        assert error["code"] == "ARG_ERROR"
        assert "root directory" in error["message"]
    assert server.close() == 0


@pytest.mark.parametrize(
    ("mode", "exit_code", "code", "context"),
    [
        ("unknown", 4, "MCP_BIND_UNKNOWN", {"field": "repository"}),
        # A command registered mcp=False is no served tool, so its field is unknown
        ("off", 4, "MCP_BIND_UNKNOWN", {"field": "approval"}),
        ("secret", 4, "MCP_BIND_INVALID", {"field": "token", "command": "deploy"}),
        ("wrong-type", 4, "MCP_BIND_INVALID", {"field": "inventory", "command": "deploy"}),
        ("not-json", 4, "MCP_BIND_INVALID", {"field": "project", "type": None}),
        ("not-mapping", 1, "HANDLER_CRASHED", {"exception": "TypeError"}),
        ("raises", 1, "HANDLER_CRASHED", {"exception": "RuntimeError"}),
    ],
)
def test_a_bad_bind_is_refused_before_serving(
    tmp_path: Path,
    started: list[Server],
    mode: str,
    exit_code: int,
    code: str,
    context: dict[str, object],
) -> None:
    server = _serve(started, tmp_path, "--mode", mode)
    assert server.close() == exit_code
    assert server.lines == []
    envelope = server.envelope()
    error = envelope["error"]
    assert isinstance(error, dict)
    assert error["code"] == code
    for key, value in context.items():
        assert key in error["context"]
        if value is not None:
            assert error["context"][key] == value
    # A secret bind never echoes the value, on stderr or in the audit log
    assert "bound-secret" not in server.stderr()
    if server.audit.exists():
        assert "bound-secret" not in server.audit.read_text(encoding="utf-8")


# In-process


def _run(app: App, argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = app.run(argv, stdout=out, stderr=err, stdin=io.StringIO(), env={})
    return code, out.getvalue(), err.getvalue()


def test_a_rule_naming_a_bound_field_leaves_the_description() -> None:
    def audit_description(mode: str) -> str:
        code, out, _ = _run(_bindctl(), ["mcp", "serve", "--mode", mode, "--list-tools"])
        assert code == 0
        tools = {t["name"]: t for t in json.loads(out)["tools"]}
        description = tools["audit"]["description"]
        assert isinstance(description, str)
        return description

    # Policy left to the call: the rule tells the agent what a strict audit needs
    assert "Rules: --policy strict requires --reason." in audit_description("fleet")
    # Policy bound: the call cannot pass it, so the rule is not described, though a call
    # without a reason is still refused (test_a_rule_naming_a_bound_field_sees_...)
    assert "Rules:" not in audit_description("strict")


@pytest.mark.parametrize("extra", [[], ["--list-tools"]])
def test_treaty_mcp_refuses_an_app_that_binds(extra: list[str]) -> None:
    # treaty-mcp has no startup arguments to bind from: serving or listing would leave
    # every bound field free for the call
    done = subprocess.run(
        [sys.executable, "-m", "treaty._mcp", "fixture_mcp_bind_app:app", *extra],
        cwd=HERE,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 4
    assert done.stdout == b""
    stderr = done.stderr.decode("utf-8")
    assert "MCP_BIND_NEEDS_SERVE" in stderr and "bindctl mcp serve" in stderr


def test_list_tools_and_mcp_validate_show_the_bound_schemas(tmp_path: Path) -> None:
    app = _bindctl()
    code, out, _ = _run(app, ["mcp", "serve", "--list-tools"])
    assert code == 0
    tools = {t["name"]: t for t in json.loads(out)["tools"]}
    assert "project" not in tools["deploy"]["inputSchema"]["properties"]
    saved = tmp_path / "mcp.json"
    saved.write_text(out, encoding="utf-8")

    def validate(serve_args: str) -> dict[str, object]:
        argv = ["mcp-validate", "--mcp-schema-file", str(saved), "--serve-args", serve_args]
        loaded = json.loads(_run(app, argv)[1])
        assert isinstance(loaded, dict)
        return loaded

    assert validate("{}")["ok"] is True
    # Strict binds fewer fields: the ones it leaves to the call are drift
    drifted = validate('{"mode": "strict"}')
    assert drifted["error"]["code"] == "SCHEMA_DRIFT_DETECTED"  # type: ignore[index]
    added = drifted["data"]["drift"]["added"]  # type: ignore[index]
    assert {"command": "deploy", "field": "input.region"} in added
    unknown = validate('{"mode": "unknown"}')
    assert unknown["error"]["code"] == "MCP_BIND_UNKNOWN"  # type: ignore[index]


def test_a_command_without_bindings_is_called_as_before() -> None:
    app = _bindctl()
    envelope = app.call("deploy", {"project": OTHER, "target": "h"}, env={})
    assert envelope.ok and envelope.to_json()["data"]["project"] == OTHER  # type: ignore[index]
    entries = {e.name: e for e in tool_entries(app)}
    unbound = call_tool(app, entries, "deploy", {"project": OTHER, "target": "h"}, env={})
    assert unbound.ok


def test_bindings_apply_only_to_the_fields_a_command_has() -> None:
    app = _bindctl()
    spec = app.mcp
    assert spec is not None and spec.args is not None
    bindings = bound_values(app, spec, spec.args(), None)
    assert dict(bindings.for_command(app.commands[CommandPath("audit")])) == {"project": PROJECT}
    assert dict(bindings.for_command(app.commands[CommandPath("ping")])) == {}
    assert NO_BINDINGS == Bindings()
    with pytest.raises(TypeError):
        bindings.values["project"] = OTHER  # type: ignore[index]


def test_registration_refuses_a_bind_that_is_no_function() -> None:
    with pytest.raises(RegistrationError, match="bind"):
        McpServe(bind={"project": "."})  # type: ignore[arg-type]
