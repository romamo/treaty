"""Tools from runtime data beside the command tools of ``mcp serve`` (#240): listed with
their own hints, called with their arguments checked against their input schema, and
refused before serving when a name clashes or a schema is invalid."""

import io
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from test_mcp_serve import Server

from treaty import App, Ctx, Flag, McpServe, McpTool, RegistrationError

pytest.importorskip("mcp")

CATALOG = [
    {"name": "restart-web", "description": "Restart the web tier", "risk": "low"},
    {"name": "drop-db", "description": "Drop the database", "risk": "destructive"},
]


def _catalog(tmp: Path, entries: Sequence[Mapping[str, str]]) -> Path:
    path = tmp / "catalog.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    return root


@pytest.fixture
def server(tmp_path: Path, project: Path) -> Iterator[Server]:
    catalog = _catalog(tmp_path, CATALOG)
    started = Server(["--project", str(project), "--catalog", str(catalog)], tmp_path)
    yield started
    if started.proc.poll() is None:
        started.proc.kill()
        started.wait()


def _result(answer: dict[str, object]) -> dict[str, object]:
    result = answer.get("result")
    assert isinstance(result, dict), answer
    return result


def test_provided_tools_are_listed_beside_the_commands_with_their_hints(
    server: Server,
) -> None:
    init = _result(server.initialize())
    # instructions= as a function of the startup arguments
    assert init["instructions"] == "Preview operations in proj; a person approves them."
    tools = {t["name"]: t for t in _result(server.request("tools/list"))["tools"]}  # type: ignore[union-attr]
    assert {"echo", "ping", "restart-web", "drop-db"} <= set(tools)
    drop = tools["drop-db"]
    # Read-only, since a call only previews, yet destructive, for what it stands for
    assert drop["annotations"]["readOnlyHint"] is True
    assert drop["annotations"]["destructiveHint"] is True
    assert tools["restart-web"]["annotations"]["destructiveHint"] is False
    assert drop["inputSchema"]["required"] == ["target"]
    # The destructive tool's schema gains the confirmation treaty requires
    assert drop["inputSchema"]["properties"]["confirm_destructive"]["type"] == "boolean"
    assert "confirm_destructive" not in tools["restart-web"]["inputSchema"]["properties"]
    data = drop["outputSchema"]["else"]["then"]["properties"]["data"]["anyOf"][0]
    assert set(data["properties"]) == {"operation", "target", "applied"}
    assert server.close() == 0


def test_a_provided_tool_call_is_enveloped_and_its_print_misses_the_protocol(
    server: Server,
) -> None:
    server.initialize()
    confirmed = {"target": "db1", "confirm_destructive": True}
    called = _result(server.request("tools/call", {"name": "drop-db", "arguments": confirmed}))
    assert called["isError"] is False
    body = called["structuredContent"]
    assert isinstance(body, dict)
    assert body["data"] == {"operation": "drop-db", "target": "db1", "applied": False}
    assert body["meta"]["tool"] == "drop-db"
    missing = _result(
        server.request(
            "tools/call",
            {"name": "drop-db", "arguments": {"target": "missing", "confirm_destructive": True}},
        )
    )
    assert missing["isError"] is True
    assert missing["structuredContent"]["error"]["code"] == "NOT_FOUND"  # type: ignore[index]
    assert server.close() == 0
    for line in server.lines:
        assert json.loads(line)["jsonrpc"] == "2.0"
    assert server.envelope()["data"] == {"stopped_by": "eof", "tool_calls": 2}
    entries = [e for e in server.audit_entries() if e["command"] == "mcp.serve"]
    calls = [e["args"] for e in entries if "tool" in e["args"]]  # type: ignore[operator]
    assert calls == [
        {"tool": "drop-db", "arguments": {"target": "db1"}, "confirm_destructive": True},
        {"tool": "drop-db", "arguments": {"target": "missing"}, "confirm_destructive": True},
    ]


@pytest.mark.parametrize("confirm", [None, False])
def test_an_unconfirmed_destructive_tool_is_refused_and_never_runs(
    server: Server, confirm: bool | None
) -> None:
    server.initialize()
    arguments: dict[str, object] = {"target": "db1"}
    if confirm is not None:
        arguments["confirm_destructive"] = confirm
    called = _result(server.request("tools/call", {"name": "drop-db", "arguments": arguments}))
    assert called["isError"] is True
    body = called["structuredContent"]
    assert isinstance(body, dict)
    assert body["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert body["meta"]["exit_code"] == 2
    assert body["error"]["context"] == {"tool": "drop-db", "flag": "confirm-destructive"}
    assert server.close() == 0
    assert "previewing" not in server.stderr()  # the handler never ran


@pytest.mark.parametrize(
    ("arguments", "where"),
    [
        ({"target": 5}, "target"),
        ({}, ""),
        ({"target": "db1", "force": True}, ""),
        ({"target": ""}, "target"),
    ],
)
def test_arguments_that_break_the_input_schema_exit_2_before_the_handler_runs(
    server: Server, arguments: dict[str, object], where: str
) -> None:
    server.initialize()
    called = _result(server.request("tools/call", {"name": "restart-web", "arguments": arguments}))
    assert called["isError"] is True
    body = called["structuredContent"]
    assert isinstance(body, dict)
    assert body["meta"]["exit_code"] == 2
    assert body["error"]["context"]["errors"][0]["path"] == where
    assert server.close() == 0
    assert "previewing" not in server.stderr()


def test_a_name_clash_is_refused_before_serving(tmp_path: Path, project: Path) -> None:
    catalog = _catalog(tmp_path, [{"name": "ping", "description": "Clash", "risk": "low"}])
    started = Server(["--project", str(project), "--catalog", str(catalog)], tmp_path)
    assert started.close() == 4
    assert started.lines == []
    envelope = started.envelope()
    assert envelope["error"]["code"] == "MCP_TOOL_NAME_TAKEN"  # type: ignore[index]
    assert envelope["error"]["context"] == {"tool": "ping"}  # type: ignore[index]


# In-process


@dataclass(frozen=True, slots=True)
class Start:
    count: int = Flag(default=1, description="Tools to provide")


@dataclass(frozen=True, slots=True)
class Echoed:
    said: str


def say(arguments: Mapping[str, object], ctx: Ctx) -> Echoed:
    return Echoed(str(arguments.get("text")))


def _tool(name: str, **kw: object) -> McpTool:
    fields: dict[str, object] = {
        "description": f"Tool {name}",
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
        "handler": say,
    }
    fields.update(kw)
    return McpTool(name=name, **fields)  # type: ignore[arg-type]


def _app(tools: object, **kw: object) -> App:
    def provide(args: Start, ctx: Ctx) -> object:
        return tools(args) if callable(tools) else tools

    return App("x", version="1.0.0", mcp=McpServe(args=Start, tools=provide, **kw))  # type: ignore[arg-type]


def _serve(app: App, argv: Sequence[str] = ()) -> tuple[int, str, dict[str, object]]:
    out, err = io.StringIO(), io.StringIO()
    code = app.run(["mcp", "serve", *argv], stdout=out, stderr=err, stdin=io.StringIO(), env={})
    return code, out.getvalue(), json.loads(err.getvalue().strip().splitlines()[-1])


def test_list_tools_lists_what_the_startup_arguments_provide() -> None:
    app = _app(lambda args: [_tool(f"t{n}") for n in range(args.count)])
    code, out, envelope = _serve(app, ["--count", "2", "--list-tools"])
    assert code == 0
    names = [t["name"] for t in json.loads(out)["tools"]]
    assert names[-2:] == ["t0", "t1"]
    assert envelope["data"] == {"stopped_by": "list-tools", "tool_calls": 0}


def test_an_invalid_input_schema_is_refused_before_serving() -> None:
    broken = _tool("bad", input_schema={"type": "object", "properties": {"x": {"type": 3}}})
    code, out, envelope = _serve(_app([broken]))
    assert code == 4 and out == ""
    assert envelope["error"]["code"] == "MCP_TOOL_INVALID"  # type: ignore[index]


def test_a_destructive_tool_defining_confirm_destructive_is_refused() -> None:
    schema = {"type": "object", "properties": {"confirm_destructive": {"type": "boolean"}}}
    clash = _tool("wipe", danger_level="destructive", input_schema=schema)
    code, out, envelope = _serve(_app([clash]))
    assert code == 4 and out == ""
    assert envelope["error"]["code"] == "MCP_TOOL_INVALID"  # type: ignore[index]
    assert "confirm_destructive" in envelope["error"]["message"]  # type: ignore[index]
    # A safe tool may name its own field so
    assert _serve(_app([_tool("keep", input_schema=schema)]), ["--list-tools"])[0] == 0


def test_a_destructive_call_needs_a_boolean_confirmation() -> None:
    from treaty._mcp_serve import provided_tools

    app = _app([_tool("wipe", danger_level="destructive")])
    ctxs: list[Ctx] = []

    @app.command("grab", description="Grab the ctx", danger_level="safe", exit_codes=())
    def grab(args: Start, ctx: Ctx) -> dict[str, int]:
        ctxs.append(ctx)
        return {}

    app.call("grab", {})
    assert app.mcp is not None
    wipe = provided_tools(app, app.mcp, Start(), ctxs[0])["wipe"]
    loose = app._call_provided(wipe, {"text": "a", "confirm_destructive": "yes"}, env={})
    assert loose.exit_code == 2 and loose.error is not None
    assert loose.error.context["field"] == "confirm_destructive"
    refused = app._call_provided(wipe, {"text": "a"}, env={})
    assert refused.error is not None and refused.error.code == "CONFIRMATION_REQUIRED"
    applied = app._call_provided(wipe, {"text": "a", "confirm_destructive": True}, env={})
    assert applied.ok and applied.data == {"said": "a"}


def test_two_provided_tools_of_one_name_are_refused() -> None:
    code, _, envelope = _serve(_app([_tool("twin"), _tool("twin")]))
    assert code == 4
    assert envelope["error"]["code"] == "MCP_TOOL_NAME_TAKEN"  # type: ignore[index]


def test_a_handler_without_a_return_annotation_is_refused() -> None:
    def bare(arguments, ctx):  # type: ignore[no-untyped-def]
        return {}

    code, _, envelope = _serve(_app([_tool("bare", handler=bare)]))
    assert code == 4
    assert "return annotation" in envelope["error"]["message"]  # type: ignore[index]


def test_a_provider_returning_no_tools_crashes_the_run() -> None:
    code, _, envelope = _serve(_app("not tools"))
    assert code == 1
    assert "TypeError" in json.dumps(envelope)


def test_provided_calls_run_through_the_call_path() -> None:
    from treaty._mcp_serve import provided_tools

    app = _app([_tool("say")])
    ctx_holder: list[Ctx] = []

    @app.command("grab", description="Grab the ctx", danger_level="safe", exit_codes=())
    def grab(args: Start, ctx: Ctx) -> dict[str, int]:
        ctx_holder.append(ctx)
        return {}

    app.call("grab", {})
    assert app.mcp is not None
    provided = provided_tools(app, app.mcp, Start(), ctx_holder[0])
    envelope = app._call_provided(provided["say"], {"text": "hi"}, env={})
    assert envelope.ok and envelope.data == {"said": "hi"}
    refused = app._call_provided(provided["say"], {"text": 1}, env={})
    assert refused.exit_code == 2


def test_mcp_validate_compares_provided_tools_given_the_startup_arguments(
    tmp_path: Path,
) -> None:
    app = _app(lambda args: [_tool(f"t{n}") for n in range(args.count)])
    _, out, _ = _serve(app, ["--count", "2", "--list-tools"])
    saved = tmp_path / "mcp.json"
    saved.write_text(out, encoding="utf-8")

    def validate(serve_args: str) -> dict[str, object]:
        stdout = io.StringIO()
        app.run(
            ["mcp-validate", "--mcp-schema-file", str(saved), "--serve-args", serve_args],
            stdout=stdout,
            stderr=io.StringIO(),
            env={},
        )
        loaded = json.loads(stdout.getvalue())
        assert isinstance(loaded, dict)
        return loaded

    assert validate('{"count": 2}')["ok"] is True
    drifted = validate('{"count": 1}')
    assert drifted["error"]["code"] == "SCHEMA_DRIFT_DETECTED"  # type: ignore[index]
    removed = drifted["data"]["drift"]["removed"]  # type: ignore[index]
    assert removed == [{"command": "t1", "field": None}]
    assert "x mcp serve --list-tools" in drifted["error"]["fix_required"]  # type: ignore[index]
    bad = validate("[1]")
    assert bad["meta"]["exit_code"] == 2  # type: ignore[index]


def test_mcp_validate_takes_no_serve_args_without_provided_tools() -> None:
    app = App("x", version="1.0.0", mcp=McpServe())
    schema = json.loads(_run_schema(app))
    assert "serve-args" not in schema["data"]["flags"]


def _run_schema(app: App) -> str:
    out = io.StringIO()
    app.run(["mcp-validate", "--schema"], stdout=out, stderr=io.StringIO(), env={})
    return out.getvalue()


def test_instructions_as_text() -> None:
    from treaty._mcp_serve import instructions_for

    app = App("x", version="1.0.0", mcp=McpServe(instructions="Use the tools."))
    assert app.mcp is not None
    assert instructions_for(app, app.mcp, None) == "Use the tools."


# Declaration


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"name": "has space"}, "tool name"),
        ({"name": ""}, "tool name"),
        ({"input_schema": {"type": "string"}}, "input_schema"),
        ({"description": " "}, "description"),
        ({"handler": "say"}, "handler"),
        ({"danger_level": "risky"}, "danger_level"),
        ({"read_only": "yes"}, "read_only"),
    ],
)
def test_mcp_tool_refuses_a_malformed_declaration(kw: dict[str, object], message: str) -> None:
    name = kw.pop("name", "ok")
    with pytest.raises(ValueError, match=message):
        _tool(name, **kw)  # type: ignore[arg-type]


def test_mcp_serve_refuses_malformed_tools_and_instructions() -> None:
    with pytest.raises(RegistrationError, match="tools"):
        McpServe(tools="catalog")  # type: ignore[arg-type]
    with pytest.raises(RegistrationError, match="instructions"):
        McpServe(instructions="")


def test_hints_follow_the_danger_level_unless_given() -> None:
    destructive = _tool("d", danger_level="destructive")
    assert (destructive.read_only_hint, destructive.destructive_hint) == (False, True)
    preview = _tool("p", danger_level="destructive", read_only=True)
    assert (preview.read_only_hint, preview.destructive_hint) == (True, True)
    safe = _tool("s")
    assert (safe.read_only_hint, safe.destructive_hint) == (True, False)
