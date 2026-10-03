"""Resources and settings for what ``McpServe(tools=)`` provides (#302): the provider takes
them by annotation after ``ctx`` as ``setup`` does, acquired once as serving starts and
released when it ends; an ``McpTool`` handler takes them per call, released when the call
ends however it ends. Two-parameter providers and handlers run as before."""

import asyncio
import io
import json
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from treaty import App, Ctx, Exit, Flag, McpServe, McpTool, NoArgs, RegistrationError
from treaty._mcp_serve import Provided, provided_tools, provider_resources

pytest.importorskip("mcp")

SECRET = "sk-live-302-abcdef"


@dataclass(frozen=True, slots=True)
class Settings:
    region: str = "us-east-1"
    api_token: str = Flag(default="", description="API token", secret=True)


@dataclass(frozen=True, slots=True)
class Start:
    prefix: str = Flag(default="t", description="Tool name prefix")


EVENTS: list[str] = []


class Client:
    """A resource that records when it is acquired and released"""

    def __init__(self, region: str) -> None:
        self.region = region

    @classmethod
    def acquire(cls, args: object, ctx: Ctx, settings: Settings) -> Client:
        EVENTS.append(f"acquire {settings.region}")
        return cls(settings.region)

    def release(self) -> None:
        EVENTS.append("release")


class AsyncClient:
    @classmethod
    async def acquire(cls, args: object, ctx: Ctx) -> AsyncClient:
        EVENTS.append("acquire async")
        return cls()

    async def release(self) -> None:
        EVENTS.append("release async")


@dataclass(frozen=True, slots=True)
class Where:
    region: str
    token_set: bool


def where(arguments: Mapping[str, object], ctx: Ctx, settings: Settings, client: Client) -> Where:
    EVENTS.append(f"call {client.region}")
    if arguments.get("fail"):
        raise Exit.NOT_FOUND(f"nothing in {settings.region}")
    return Where(settings.region, bool(settings.api_token))


def say(arguments: Mapping[str, object], ctx: Ctx) -> Where:
    return Where("plain", False)


def _tool(name: str, handler: object) -> McpTool:
    return McpTool(
        name=name,
        description=f"Tool {name}",
        input_schema={"type": "object", "properties": {"fail": {"type": "boolean"}}},
        handler=handler,  # type: ignore[arg-type]
        exit_codes=("NOT_FOUND",),
    )


def provide(args: Start, ctx: Ctx, settings: Settings, client: Client) -> list[McpTool]:
    EVENTS.append(f"provide {settings.region} {client.region}")
    return [_tool(f"{args.prefix}-{settings.region}", where), _tool(f"{args.prefix}-say", say)]


def _app(tools: object = provide) -> App:
    return App(
        "x",
        version="1.0.0",
        settings=Settings,
        mcp=McpServe(args=Start, tools=tools, exit_codes=("NOT_FOUND",)),  # type: ignore[arg-type]
    )


ENV = {"X_REGION": "eu-west-2", "X_API_TOKEN": SECRET}


def _serve(app: App, argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = app.run(["mcp", "serve", *argv], stdout=out, stderr=err, stdin=io.StringIO(), env=env)
    return code, out.getvalue(), err.getvalue()


def _provided(app: App, env: Mapping[str, str]) -> dict[str, Provided]:
    """The provided tools as ``mcp serve`` builds them, with a real run's ctx"""
    ctxs: list[Ctx] = []

    @app.command("grab", description="Grab the ctx", danger_level="safe", exit_codes=())
    def grab(args: NoArgs, ctx: Ctx, settings: Settings) -> dict[str, int]:
        ctxs.append(ctx)
        return {}

    app.call("grab", {}, env=env)
    assert app.mcp is not None
    held: dict[type, object] = {
        Settings: Settings("eu-west-2", SECRET),
        Client: Client("eu-west-2"),
    }
    taken = [held[c] for c in provider_resources(app, app.mcp)]
    return provided_tools(app, app.mcp, Start(), ctxs[0], taken)


def test_the_provider_takes_settings_and_resources_held_while_serving() -> None:
    EVENTS.clear()
    code, out, err = _serve(_app(), ["--prefix", "p", "--list-tools"], ENV)
    assert code == 0, err
    names = [t["name"] for t in json.loads(out)["tools"]]
    assert names[-2:] == ["p-eu-west-2", "p-say"]
    # Acquired once as serving starts, released when the server stops
    assert EVENTS == ["acquire eu-west-2", "provide eu-west-2 eu-west-2", "release"]
    assert SECRET not in out and SECRET not in err


def test_a_tool_handler_acquires_its_resources_per_call_and_releases_them() -> None:
    app = _app()
    tools = _provided(app, ENV)
    EVENTS.clear()
    first = app._call_provided(tools["t-eu-west-2"], {}, env=ENV)
    assert first.ok and first.data == {"region": "eu-west-2", "token_set": True}
    second = app._call_provided(tools["t-eu-west-2"], {}, env=ENV)
    assert second.ok
    once = ["acquire eu-west-2", "call eu-west-2", "release"]
    assert EVENTS == once * 2
    # The settings are read for the call, from its own environment
    other = app._call_provided(tools["t-eu-west-2"], {}, env={"X_REGION": "ap-south-1"})
    assert other.data == {"region": "ap-south-1", "token_set": False}


def test_a_failing_call_still_releases_its_resources() -> None:
    app = _app()
    tools = _provided(app, ENV)
    EVENTS.clear()
    failed = app._call_provided(tools["t-eu-west-2"], {"fail": True}, env=ENV)
    assert failed.error is not None and failed.error.code == "NOT_FOUND"
    assert EVENTS == ["acquire eu-west-2", "call eu-west-2", "release"]


def test_a_two_parameter_handler_runs_as_before() -> None:
    app = _app()
    tools = _provided(app, ENV)
    EVENTS.clear()
    called = app._call_provided(tools["t-say"], {}, env=ENV)
    assert called.ok and called.data == {"region": "plain", "token_set": False}
    assert EVENTS == []


def test_an_async_handler_takes_async_resources() -> None:
    async def awaited(
        arguments: Mapping[str, object], ctx: Ctx, settings: Settings, client: AsyncClient
    ) -> Where:
        EVENTS.append("call async")
        return Where(settings.region, False)

    async def bare(arguments: Mapping[str, object], ctx: Ctx) -> Where:
        return Where(str(arguments.get("fail")), False)

    app = _app(lambda args, ctx: [_tool("a", awaited), _tool("b", bare)])
    tools = _provided(app, ENV)
    EVENTS.clear()
    called = app._call_provided(tools["a"], {}, env=ENV)
    assert called.ok and called.data == {"region": "eu-west-2", "token_set": False}
    assert EVENTS == ["acquire async", "call async", "release async"]
    plain = app._call_provided(tools["b"], {"fail": False}, env=ENV)
    assert plain.ok and plain.data == {"region": "False", "token_set": False}


@pytest.mark.parametrize("crash", [False, True])
def test_a_secret_setting_stays_out_of_schemas_errors_and_logs(
    capsys: pytest.CaptureFixture[str], crash: bool
) -> None:
    def leak(arguments: Mapping[str, object], ctx: Ctx, settings: Settings) -> Where:
        ctx.log(f"token {settings.api_token}")
        if crash:
            raise RuntimeError(f"auth failed for {settings.api_token}")
        raise Exit.NOT_FOUND(f"no key {settings.api_token}", context={"t": settings.api_token})

    app = _app(lambda args, ctx: [_tool("leak", leak)])
    tools = _provided(app, ENV)
    assert "api_token" not in json.dumps(tools["leak"].entry().input_schema)
    envelope = app._call_provided(tools["leak"], {}, env=ENV)
    assert envelope.error is not None
    assert SECRET not in json.dumps(envelope.to_json())
    err = capsys.readouterr().err
    assert SECRET not in err and "[REDACTED]" in json.dumps(envelope.to_json()) + err


def test_mcp_validate_acquires_the_providers_resources(tmp_path: Path) -> None:
    app = _app()
    _, listed, _ = _serve(app, ["--list-tools"], ENV)
    path = tmp_path / "mcp.json"
    path.write_text(listed, encoding="utf-8")
    EVENTS.clear()
    out = io.StringIO()
    code = app.run(
        ["mcp-validate", "--mcp-schema-file", str(path), "--serve-args", "{}"],
        stdout=out,
        stderr=io.StringIO(),
        env=ENV,
    )
    assert code == 0, out.getvalue()
    assert EVENTS == ["acquire eu-west-2", "provide eu-west-2 eu-west-2", "release"]


def test_a_provider_parameter_that_is_no_resource_fails_at_registration() -> None:
    def odd(args: Start, ctx: Ctx, region: str) -> list[McpTool]:
        return []

    with pytest.raises(RegistrationError, match="parameter 'region' is annotated with str"):
        _app(odd)


def test_a_provider_with_resources_needs_ctx_annotated() -> None:
    def loose(args, ctx, client: Client) -> list[McpTool]:  # type: ignore[no-untyped-def]
        return []

    with pytest.raises(RegistrationError, match="annotated with Ctx"):
        _app(loose)


def test_a_handler_parameter_that_is_no_resource_is_refused_before_serving() -> None:
    def odd(arguments: Mapping[str, object], ctx: Ctx, region: str) -> Where:
        return Where(region, False)

    code, out, err = _serve(_app(lambda args, ctx: [_tool("odd", odd)]), [], ENV)
    envelope = json.loads(err.strip().splitlines()[-1])
    assert code == 4 and out == ""
    assert envelope["error"]["code"] == "MCP_TOOL_INVALID"
    assert "parameter 'region'" in envelope["error"]["message"]


def test_a_handler_taking_resources_needs_ctx_annotated() -> None:
    def loose(arguments, ctx, client: Client) -> Where:  # type: ignore[no-untyped-def]
        return Where("", False)

    with pytest.raises(RegistrationError, match="annotated with Ctx"):
        _tool("loose", loose)


class Gate:
    """A resource whose release a test can wait for"""

    released = threading.Event()

    @classmethod
    def acquire(cls, args: object, ctx: Ctx) -> Gate:
        cls.released.clear()
        EVENTS.append("acquire gate")
        return cls()

    def release(self) -> None:
        EVENTS.append("release gate")
        Gate.released.set()


@pytest.mark.parametrize("is_async", [False, True])
def test_a_call_that_times_out_still_releases_its_resources(is_async: bool) -> None:
    def slow(arguments: Mapping[str, object], ctx: Ctx, gate: Gate) -> Where:
        time.sleep(1.0)
        return Where("late", False)

    async def slow_async(arguments: Mapping[str, object], ctx: Ctx, gate: Gate) -> Where:
        await asyncio.sleep(30)
        return Where("late", False)

    handler = slow_async if is_async else slow
    app = App(
        "x",
        version="1.0.0",
        settings=Settings,
        default_timeout=0.2,
        mcp=McpServe(args=Start, tools=lambda args, ctx: [_tool("slow", handler)]),
    )
    tools = _provided(app, ENV)
    EVENTS.clear()
    called = app._call_provided(tools["slow"], {}, env=ENV)
    assert called.error is not None and called.error.code == "TIMEOUT"
    # An async call is cancelled at the deadline; a sync one is released when it returns
    assert Gate.released.wait(5)
    assert EVENTS == ["acquire gate", "release gate"]
