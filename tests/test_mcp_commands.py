"""Which commands an MCP server serves (#281): ``McpServe(commands=select)`` per startup
mode, ``mcp=False`` on a command whatever ``select`` returns, and the built-ins that are
no tools. A command left off is not callable through the server by any name."""

import asyncio
import io
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import spec_validator
from test_mcp_serve import Server

from treaty import App, Ctx, McpServe, NoArgs, RegistrationError
from treaty._mcp import call_tool, tool_entries

pytest.importorskip("mcp")

HERE = Path(__file__).resolve().parent
SELECTCTL = HERE / "fixture_mcp_select_app.py"

LEFT_OFF = [
    "approve",
    "Approve",
    "APPROVE",
    " approve",
    "approve ",
    "approve​",
    "ａpprove",  # fullwidth a, which NFKC folds to approve
    "ok",  # the approval's old name, redirected to it
    "deploy",  # a command the repository mode does not select
    "manifest",
    "cleanup",
    "generate-skills",
    "audit-log",
    "exec",
    "mcp_serve",
    "mcp.serve",
    "observe.logs",  # the command path, not the tool name
]


def _selectctl() -> App:
    sys.path.insert(0, str(HERE))
    try:
        from fixture_mcp_select_app import app
    finally:
        sys.path.remove(str(HERE))
    return app


def _ran(marker: Path) -> list[str]:
    return marker.read_text(encoding="utf-8").splitlines() if marker.exists() else []


def _result(answer: dict[str, object]) -> dict[str, object]:
    result = answer.get("result")
    assert isinstance(result, dict), answer
    return result


def _call(server: Server, name: str, arguments: dict[str, object]) -> dict[str, object]:
    return _result(server.request("tools/call", {"name": name, "arguments": arguments}))


def _code(result: dict[str, object]) -> object:
    content = result["structuredContent"]
    assert isinstance(content, dict)
    error = content["error"]
    assert isinstance(error, dict)
    return error["code"]


@pytest.fixture
def started(tmp_path: Path) -> Iterator[list[Server]]:
    servers: list[Server] = []
    yield servers
    for server in servers:
        if server.proc.poll() is None:
            server.proc.kill()
            server.wait()


def _serve(started: list[Server], tmp: Path, *args: str) -> Server:
    marker = tmp / "ran.txt"
    server = Server(list(args), tmp, env={"SELECTCTL_RAN": str(marker)}, script=SELECTCTL)
    started.append(server)
    return server


# Over the real stdio server


def test_a_command_left_off_answers_unknown_by_every_name(
    tmp_path: Path, started: list[Server]
) -> None:
    server = _serve(started, tmp_path, "--mode", "repository")
    server.initialize()
    tools = _result(server.request("tools/list"))["tools"]
    assert isinstance(tools, list)
    # select named the approval too: mcp=False keeps it off whatever select returns
    assert sorted(t["name"] for t in tools) == ["fleet", "observe_logs"]
    for name in LEFT_OFF:
        called = _call(server, name, {})
        assert called["isError"] is True, name
        assert _code(called) == "UNKNOWN_TOOL", name
    # A served tool cannot reach another command through its arguments
    smuggled = _call(server, "fleet", {"_cmd": "approve", "command": "approve"})
    assert smuggled["isError"] is True
    assert _call(server, "observe_logs", {})["isError"] is False
    assert server.close() == 0
    assert _ran(tmp_path / "ran.txt") == ["observe.logs"]


def test_every_command_but_the_mcp_false_ones_without_a_selection(
    tmp_path: Path, started: list[Server]
) -> None:
    server = _serve(started, tmp_path, "--mode", "project")
    server.initialize()
    tools = _result(server.request("tools/list"))["tools"]
    assert isinstance(tools, list)
    names = {t["name"] for t in tools}
    assert {"deploy", "fleet", "observe_logs", "manifest", "doctor"} <= names
    assert names.isdisjoint({"approve", "cleanup", "generate-skills", "audit-log"})
    assert _code(_call(server, "approve", {})) == "UNKNOWN_TOOL"
    assert server.close() == 0
    assert _ran(tmp_path / "ran.txt") == []


def test_an_empty_selection_serves_only_the_provided_tools(
    tmp_path: Path, started: list[Server]
) -> None:
    server = _serve(started, tmp_path, "--mode", "none")
    server.initialize()
    tools = _result(server.request("tools/list"))["tools"]
    assert isinstance(tools, list)
    assert [t["name"] for t in tools] == ["preview"]
    assert _code(_call(server, "fleet", {})) == "UNKNOWN_TOOL"
    assert _call(server, "preview", {})["isError"] is False
    assert server.close() == 0
    assert _ran(tmp_path / "ran.txt") == ["preview"]


@pytest.mark.parametrize(
    ("mode", "code", "context"),
    [
        ("unknown", "MCP_COMMAND_UNKNOWN", {"command": "observe.missing"}),
        # A provided tool never answers for a command left off the server
        ("clash", "MCP_TOOL_NAME_TAKEN", {"tool": "approve"}),
    ],
)
def test_a_bad_selection_is_refused_before_serving(
    tmp_path: Path, started: list[Server], mode: str, code: str, context: dict[str, str]
) -> None:
    server = _serve(started, tmp_path, "--mode", mode)
    assert server.close() == 4
    assert server.lines == []
    envelope = server.envelope()
    assert envelope["ok"] is False
    error = envelope["error"]
    assert isinstance(error, dict)
    assert error["code"] == code and error["context"] == context


def test_treaty_mcp_leaves_the_mcp_false_commands_off(tmp_path: Path) -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    marker = tmp_path / "ran.txt"

    async def scenario() -> tuple[list[str], list[object]]:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "treaty._mcp", "fixture_mcp_select_app:app"],
            cwd=str(HERE),
            env={**os.environ, "SELECTCTL_RAN": str(marker)},
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            codes = []
            for name in ("approve", "Approve", "ok", "cleanup", "audit-log"):
                called = await session.call_tool(name, {})
                assert called.is_error is True, name
                content = called.structured_content
                assert isinstance(content, dict)
                codes.append(content["error"]["code"])
            return sorted(t.name for t in tools.tools), codes

    names, codes = asyncio.run(scenario())
    assert "deploy" in names and "fleet" in names
    assert {"approve", "cleanup", "generate-skills", "audit-log"}.isdisjoint(names)
    assert codes == ["UNKNOWN_TOOL"] * 5
    assert _ran(marker) == []


# In-process


def _run(app: App, argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = app.run(argv, stdout=out, stderr=err, stdin=io.StringIO(), env={})
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("repository", ["fleet", "observe_logs"]), ("none", ["preview"])],
)
def test_list_tools_lists_the_selection(mode: str, expected: list[str]) -> None:
    code, out, _ = _run(_selectctl(), ["mcp", "serve", "--mode", mode, "--list-tools"])
    assert code == 0
    assert [t["name"] for t in json.loads(out)["tools"]] == expected


def test_list_tools_refuses_an_unknown_path() -> None:
    code, out, err = _run(_selectctl(), ["mcp", "serve", "--mode", "unknown", "--list-tools"])
    assert code == 4 and out == ""
    assert json.loads(err.strip().splitlines()[-1])["error"]["code"] == "MCP_COMMAND_UNKNOWN"


def test_mcp_validate_compares_the_selection_the_serve_args_make(tmp_path: Path) -> None:
    app = _selectctl()
    _, out, _ = _run(app, ["mcp", "serve", "--mode", "repository", "--list-tools"])
    saved = tmp_path / "mcp.json"
    saved.write_text(out, encoding="utf-8")

    def validate(serve_args: str) -> dict[str, object]:
        argv = ["mcp-validate", "--mcp-schema-file", str(saved), "--serve-args", serve_args]
        loaded = json.loads(_run(app, argv)[1])
        assert isinstance(loaded, dict)
        return loaded

    assert validate('{"mode": "repository"}')["ok"] is True
    drifted = validate('{"mode": "project"}')
    assert drifted["error"]["code"] == "SCHEMA_DRIFT_DETECTED"  # type: ignore[index]
    missing = drifted["data"]["drift"]["missing_from_mcp"]  # type: ignore[index]
    assert "deploy" in missing and "approve" not in missing
    unknown = validate('{"mode": "unknown"}')
    assert unknown["error"]["code"] == "MCP_COMMAND_UNKNOWN"  # type: ignore[index]


def test_the_manifest_marks_a_command_that_is_no_tool() -> None:
    manifest = _selectctl().manifest()
    spec_validator("manifest-response").validate(manifest)
    commands = manifest["commands"]
    assert isinstance(commands, dict)
    for path in ("approve", "cleanup", "generate-skills", "audit-log"):
        assert commands[path]["description"].endswith(" (not an MCP tool)"), path
    assert not commands["fleet"]["description"].endswith("(not an MCP tool)")


def test_an_app_command_shadowing_a_builtin_is_served_unless_it_says_otherwise() -> None:
    app = App("x", version="1.0.0")

    @app.command("cleanup", description="Tidy the queue", danger_level="safe", exit_codes=())
    def cleanup(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    assert "cleanup" in {e.name for e in tool_entries(app)}


def test_call_tool_answers_a_redirect_only_to_a_served_tool() -> None:
    app = _selectctl()
    app.redirect("logs", to="observe.logs")
    entries = {e.name: e for e in tool_entries(app)}
    moved = call_tool(app, entries, "logs", {})
    assert moved.error is not None and moved.error.code == "REDIRECTED"
    off = call_tool(app, entries, "ok", {})
    assert off.error is not None and off.error.code == "UNKNOWN_TOOL"


def test_registration_refuses_what_is_no_selection() -> None:
    app = App("x", version="1.0.0")
    with pytest.raises(RegistrationError, match="mcp is True or False"):
        app.command("y", description="Y", danger_level="safe", exit_codes=(), mcp="no")  # type: ignore[arg-type]
    with pytest.raises(RegistrationError, match="commands"):
        McpServe(commands=("fleet",))  # type: ignore[arg-type]
