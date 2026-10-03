"""MCP adapter: one tool per command, served in-process over stdio (``treaty[mcp]``).

Tool schemas come from what ``--schema`` already emits: the args dataclass
schema as ``inputSchema`` plus the framework keys the command declares, and
the response envelope around ``output_schema`` as ``outputSchema``. Every
call goes through ``App.call``, the same path as an ``exec`` line, so
confirmation, idempotency, timeouts, effect validation, and output caps all
apply. An unconfirmed destructive call returns the ``CONFIRMATION_REQUIRED``
envelope with its dry-run preview, exactly as the CLI does.

``treaty-mcp module:app`` serves on the process's streams as they are. An app's own
``mcp serve`` (#239) runs inside its CLI run instead, where descriptor 1 and
``sys.stdout`` lead to stderr: ``serve_wire`` speaks the protocol on the copy of the
original stdout the run holds, so a stray write never reaches the client.

Only ``build_server``, ``serve``, ``serve_wire``, and ``main`` import the ``mcp`` package;
everything else is plain data so it can be inspected and tested without the SDK.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import io
import json
import os
import sys
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import IO, Any, Literal, TextIO, cast

from ._app import App, _closed_pipe, _Run
from ._context import Wire
from ._envelope import Envelope, serialize
from ._errors import CliExit, ParseError
from ._mcp_serve import (
    DEFAULT_INSTRUCTIONS,
    MCP_BIND_NEEDS_SERVE,
    NO_BINDINGS,
    Bindings,
    McpServed,
    Provided,
)
from ._prompt import NoPromptStdin
from ._signals import Cancelled
from ._subprocess import GRACE_SECONDS
from ._tools import (
    DRAFT_07,
    ToolEntry,
    input_schema,
    output_schema,
    tool_description,
    tool_entries,
    tool_list,
    tool_name,
)
from ._values import CommandPath

__all__ = [
    "DRAFT_07",
    "ToolEntry",
    "call_tool",
    "input_schema",
    "output_schema",
    "tool_description",
    "tool_entries",
    "tool_list",
    "tool_name",
]


def without_surrogates(value: object) -> object:
    """A lone surrogate (a non-UTF-8 filename through os.fsdecode) becomes escaped text:
    the SDK serializes structured content as strict UTF-8 and would drop the connection.
    The text content already escapes it, since serialize() writes ASCII."""
    if isinstance(value, str):
        return value.encode("utf-8", "backslashreplace").decode("utf-8")
    if isinstance(value, dict):
        return {without_surrogates(k): without_surrogates(v) for k, v in value.items()}
    if isinstance(value, list):
        return [without_surrogates(v) for v in value]
    return value


def call_tool(
    app: App,
    entries: Mapping[str, ToolEntry],
    name: str,
    arguments: Mapping[str, object],
    *,
    env: Mapping[str, str] | None = None,
    bindings: Bindings = NO_BINDINGS,
    available: Sequence[str] | None = None,
) -> Envelope:
    """Dispatch one tool call; an unknown tool name is an ``UNKNOWN_TOOL`` envelope whose
    ``available`` is ``available``, the names ``tools/list`` answers in its order, or the
    ``entries`` names when None (#289). Only ``entries`` run: a command left off the server
    answers as an unknown tool, and an old name answers ``REDIRECTED`` only when the tool
    it names is among them (#281). A field ``bindings`` fixes runs with its bound value,
    and the call may not pass it (#285)"""
    entry = entries.get(name)
    served = {e.path for e in entries.values()}
    moved = next(
        (
            p
            for p in app.redirected_paths
            if tool_name(p) == name and app._redirects[p].to in served
        ),
        None,
    )
    if entry is None and moved is not None:
        # An old name answers REDIRECTED, naming the tool to call instead
        return _as_tools(app.call(moved.value, arguments, env=env))
    if entry is None:
        run = _Run(app, io.StringIO(), io.StringIO(), env if env is not None else {})
        return run.arg_error(
            ParseError(
                f"unknown tool {name!r}",
                context={
                    "tool": name,
                    "available": list(entries if available is None else available),
                },
            ),
            code="UNKNOWN_TOOL",
            meta={"_cmd": name},
        )
    bound = bindings.for_command(app.commands[entry.path])
    return app._call_bound(entry.path.value, arguments, bound, env=env)


def _as_tools(envelope: Envelope) -> Envelope:
    """A REDIRECTED envelope with tool names where the command line has command paths"""
    error = envelope.error
    if error is None or error.redirect is None:
        return envelope
    to = tool_name(CommandPath(error.redirect.command))
    was = error.context.get("from")
    redirect = dataclasses.replace(error.redirect, command=to)
    context = {**error.context, "to": to}
    if isinstance(was, str):
        context["from"] = tool_name(CommandPath(was))
    was_tool = context.get("from")
    message = (
        f"Tool {was_tool} is now {to}." if isinstance(was_tool, str) else f"This tool is now {to}."
    )
    detail = dataclasses.replace(
        error,
        message=message,
        context=context,
        suggestion=f"call {to} instead",
        redirect=redirect,
    )
    return dataclasses.replace(envelope, error=detail)


# The SDK-facing part


def build_server(
    app: App,
    *,
    env: Mapping[str, str] | None = None,
    called: Callable[[], None] | None = None,
    provided: Mapping[str, Provided] | None = None,
    instructions: str | None = None,
    served: frozenset[CommandPath] | None = None,
    bindings: Bindings = NO_BINDINGS,
) -> Any:
    """A low-level ``mcp`` Server whose tools are the app's commands, those ``served``
    selects when given (#281), and the ``provided`` tools (#240), called with ``env`` (the
    process's when None), with the fields ``bindings`` fixes (#285); ``called`` runs as
    each tool call is answered"""
    from mcp import types
    from mcp.server.lowlevel.server import Server

    extra = dict(provided or {})
    entries = {e.name: e for e in tool_entries(app, served, bindings)}
    listed = [*entries.values(), *(p.entry() for p in extra.values())]
    # An unknown tool's answer lists what tools/list lists, in its order (#289)
    available = tuple(e.name for e in listed)
    environ = env if env is not None else os.environ
    tools = [
        types.Tool(
            name=e.name,
            description=e.description,
            input_schema=e.input_schema,
            output_schema=e.output_schema,
            annotations=types.ToolAnnotations(
                read_only_hint=e.read_only,
                destructive_hint=e.destructive,
                idempotent_hint=e.idempotent,
                open_world_hint=e.open_world,
            ),
        )
        for e in listed
    ]

    async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        arguments = params.arguments or {}
        tool = extra.get(params.name)
        if tool is not None:
            envelope = await asyncio.to_thread(app._call_provided, tool, arguments, env=environ)
        else:
            envelope = await asyncio.to_thread(
                call_tool,
                app,
                entries,
                params.name,
                arguments,
                env=env,
                bindings=bindings,
                available=available,
            )
        if called is not None:
            called()
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=serialize(envelope))],
            structured_content=without_surrogates(envelope.to_json()),
            is_error=not envelope.ok,
        )

    return Server(
        app.name,
        version=app.version,
        instructions=instructions
        if instructions is not None
        else DEFAULT_INSTRUCTIONS.format(description=app.description or app.name),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def serve(app: App) -> None:
    """Run the server over stdio until the client disconnects"""
    from mcp.server.stdio import stdio_server

    server = build_server(app)
    async with stdio_server() as (read, write):
        # The transport holds the real streams now. For the rest of the process a print()
        # goes to stderr and an input() exits 4, instead of corrupting or stalling the
        # protocol. App.call only wraps the streams it finds, redacting its own threads'
        # writes there, since calls run on several threads
        sys.stdout = sys.stderr
        sys.stdin = cast(TextIO, NoPromptStdin(io.StringIO()))
        await server.run(read, write, server.create_initialization_options())


JOIN_SECONDS = 0.25
"""How often the run's thread wakes while the server runs: a wait without a timeout may
not see a signal's handler run on Windows"""


class _Serving:
    """The server thread's loop and cancel scope, so the run's thread can stop it, and how
    the server ended"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop: Callable[[], None] | None = None
        self._stopped = False
        self.calls = 0
        self.error: BaseException | None = None
        self.read_all = threading.Event()
        """Set once the stdin reader left its last read: no read of the protocol's
        descriptor is pending"""

    def started(self, stop: Callable[[], None]) -> None:
        with self._lock:
            self._stop = stop
            stopped = self._stopped
        if stopped:
            stop()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            stop = self._stop
        if stop is not None:
            stop()

    def called(self) -> None:
        with self._lock:
            self.calls += 1


def serve_wire(
    app: App,
    wire: Wire,
    *,
    env: Mapping[str, str],
    provided: Mapping[str, Provided],
    instructions: str,
    served: frozenset[CommandPath] | None,
    bindings: Bindings,
) -> McpServed:
    """``mcp serve``: the app's commands as tools on ``wire`` until stdin ends, or until a
    signal raises ``Cancelled`` here, on the run's thread, which stops the server. The
    server runs on a thread of its own, so the run's thread only waits, where a signal
    can interrupt it without tearing the event loop."""
    assert wire.stdin is not None, "mcp serve refuses a closed stdin before serving"
    serving = _Serving()
    server = build_server(
        app,
        env=env,
        called=serving.called,
        provided=provided,
        instructions=instructions,
        served=served,
        bindings=bindings,
    )
    with _claimed_stdin(wire.stdin, serving.read_all) as stdin:

        def run() -> None:
            try:
                asyncio.run(_serve_wire(server, wire.out, stdin, serving))
            except BaseException as exc:  # noqa: BLE001 - re-raised on the run's thread below
                serving.error = exc

        thread = threading.Thread(target=run, name="treaty-mcp", daemon=True)
        thread.start()
        try:
            while thread.is_alive():
                thread.join(JOIN_SECONDS)
        except Cancelled as exc:
            serving.stop()
            thread.join(GRACE_SECONDS)
            return McpServed(_signal_name(exc.signal.name), serving.calls)
        except KeyboardInterrupt:
            # No treaty handler on this thread (a run started off the main thread)
            serving.stop()
            thread.join(GRACE_SECONDS)
            return McpServed("SIGINT", serving.calls)
    if serving.error is not None:
        raise serving.error
    return McpServed("eof", serving.calls)


@contextlib.contextmanager
def _claimed_stdin(stdin: IO[str], read_all: threading.Event) -> Iterator[IO[str] | IO[bytes]]:
    """The protocol's stdin, read from a private copy of its descriptor while the
    descriptor itself reads an empty pipe, its write end closed: a handler reading
    ``sys.stdin``, or a child that inherited the descriptor, reads its end at once instead
    of blocking on or taking the client's requests. Not the null device, which Windows
    reports as a terminal, so a read would pass for a prompt. A stream with no descriptor, as in a
    test, is read as it is. The copy stays open while the reader thread may still read it."""
    try:
        fd = stdin.fileno()
    except AttributeError, OSError, ValueError:  # io.UnsupportedOperation: no descriptor
        yield stdin
        return
    private = os.dup(fd)
    source = os.fdopen(private, "rb")
    empty, end = os.pipe()
    os.close(end)
    try:
        os.dup2(empty, fd)  # inheritable, as descriptor 0 is, so a child reads its end too
    finally:
        os.close(empty)
    _rebind_std_handle(fd)
    try:
        yield source
    finally:
        # On Windows a read still pending on the copy (the client left by closing stdout,
        # or a signal stopped the server, with stdin open) holds the C runtime's lock on
        # that descriptor, so restoring from it would block until the client writes or
        # closes stdin: the run is ending, and descriptor 0 keeps reading the empty pipe
        if sys.platform != "win32" or read_all.is_set():
            os.dup2(private, fd)
            _rebind_std_handle(fd)


def _rebind_std_handle(fd: int) -> None:
    """On Windows, point the process's standard handle for ``fd`` at what the descriptor
    holds now: ``os.dup2`` changes only the C runtime's table, and ``subprocess`` hands a
    child the standard handle, so a child would still read the client's pipe"""
    if sys.platform == "win32":
        from mcp.os.win32.utilities import rebind_std_handle_to_fd

        rebind_std_handle_to_fd(fd)


def _signal_name(name: str) -> Literal["SIGINT", "SIGTERM"]:
    if name == "SIGTERM":
        return "SIGTERM"
    if name == "SIGINT":
        return "SIGINT"
    raise ValueError(f"no server stop for {name}")


async def _serve_wire(
    server: Any, out: IO[str], stdin: IO[str] | IO[bytes], serving: _Serving
) -> None:
    import anyio
    from mcp.server.stdio import stdio_server

    loop = asyncio.get_running_loop()
    lines: asyncio.Queue[str | Exception | None] = asyncio.Queue()
    reader = threading.Thread(
        target=_read_lines,
        args=(stdin, loop, lines, serving.read_all),
        name="treaty-mcp-stdin",
        daemon=True,
    )
    with anyio.CancelScope() as scope:
        serving.started(lambda: _call_soon(loop, scope.cancel))
        reader.start()
        # Explicit streams: the SDK claims no descriptor, and the protocol goes to the copy
        # of the original stdout, not descriptor 1, which leads to stderr
        async with stdio_server(
            stdin=cast(Any, _Lines(lines)), stdout=cast(Any, _WireOut(out, serving.stop))
        ) as (read, write):
            await server.run(read, write, server.create_initialization_options())


def _call_soon(loop: asyncio.AbstractEventLoop, fn: Callable[[], object]) -> None:
    try:
        loop.call_soon_threadsafe(fn)
    except RuntimeError:
        pass  # the loop already closed: the server stopped on its own


def _read_lines(
    stdin: IO[str] | IO[bytes],
    loop: asyncio.AbstractEventLoop,
    lines: asyncio.Queue[str | Exception | None],
    read_all: threading.Event,
) -> None:
    """Stdin's lines, read on a daemon thread of their own, so stopping the server never
    waits for a read; UTF-8 whatever the platform's encoding, as the SDK reads them. None
    marks the end, and an error reading is handed to the server, which raises it.
    ``read_all`` is set once no read is pending any more"""
    buffer = getattr(stdin, "buffer", None)
    item: str | Exception | None
    try:
        while True:
            raw = buffer.readline() if buffer is not None else stdin.readline()
            if not raw:
                break
            item = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            loop.call_soon_threadsafe(lines.put_nowait, item)
        item = None
    except (OSError, ValueError) as exc:
        item = exc
    except RuntimeError:
        return  # the loop closed: the server stopped first
    finally:
        read_all.set()
    try:
        loop.call_soon_threadsafe(lines.put_nowait, item)
    except RuntimeError:
        return


class _Lines:
    """What the SDK's transport iterates for stdin's lines"""

    def __init__(self, lines: asyncio.Queue[str | Exception | None]) -> None:
        self._lines = lines

    def __aiter__(self) -> _Lines:
        return self

    async def __anext__(self) -> str:
        item = await self._lines.get()
        if item is None:
            raise StopAsyncIteration
        if isinstance(item, Exception):
            raise item
        return item


class _WireOut:
    """What the SDK's transport writes each message to: UTF-8 bytes on the run's stdout's
    descriptor, whatever the platform's encoding, as the SDK writes them, and unbuffered,
    so nothing is left for the run's last flush; text on a stream without one, as in a
    test. Writes run on a worker thread, so a client that stops reading blocks no
    shutdown. A client that closed stdout left: ``gone`` stops the server, as the end of
    stdin does, and what is left to write is dropped."""

    def __init__(self, out: IO[str], gone: Callable[[], None]) -> None:
        self._out = out
        self._gone = gone
        self._closed = False
        try:
            self._fd: int | None = out.fileno()
        except AttributeError, OSError, ValueError:  # io.UnsupportedOperation: none
            self._fd = None

    async def write(self, text: str) -> None:
        import anyio

        await anyio.to_thread.run_sync(self._write, text, abandon_on_cancel=True)

    async def flush(self) -> None:
        import anyio

        await anyio.to_thread.run_sync(self._flush, abandon_on_cancel=True)

    def _write(self, text: str) -> None:
        if self._closed:
            return
        try:
            if self._fd is None:
                self._out.write(text)
                return
            self._out.flush()
            view = memoryview(text.encode("utf-8"))
            while view:
                view = view[os.write(self._fd, view) :]
        except OSError as exc:
            if not _closed_pipe(exc):
                raise
            self._closed = True
            self._gone()

    def _flush(self) -> None:
        if self._fd is None and not self._closed:
            self._out.flush()


def main(argv: list[str] | None = None) -> int:
    """``treaty-mcp module:app``: serve that app's commands as MCP tools over stdio;
    ``--list-tools`` prints them as JSON and exits, for ``mcp-validate`` (REQ-O-035). An
    app whose ``McpServe`` binds arguments is refused with exit 4, both ways: only its own
    ``mcp serve`` has the startup arguments to bind (#285)"""
    from ._cli import load_app

    args = sys.argv[1:] if argv is None else argv
    listing = args[1:] == ["--list-tools"]
    if not (len(args) == 1 or listing) or args[0].startswith("-"):
        sys.stderr.write("usage: treaty-mcp module:app [--list-tools]\n")
        return 2
    try:
        app = load_app(args[0], Path.cwd())
    except CliExit as exc:
        sys.stderr.write(f"treaty-mcp: {exc.code}: {exc.message}\n")
        return 2
    if app.mcp is not None and app.mcp.bind is not None:
        # treaty-mcp has no startup arguments to bind from: serving would leave the bound
        # fields free for every call (#285)
        sys.stderr.write(
            f"treaty-mcp: {MCP_BIND_NEEDS_SERVE}: {app.name} binds arguments with "
            f"McpServe(bind=), which only its own server applies; run {app.name} mcp serve "
            "instead\n"
        )
        return 4
    if listing:
        sys.stdout.write(json.dumps(tool_list(app), indent=2, sort_keys=True) + "\n")
        return 0
    try:
        import mcp  # noqa: F401 - probe for the optional dependency
    except ModuleNotFoundError:
        sys.stderr.write("treaty-mcp: the mcp package is missing; install treaty[mcp]\n")
        return 2
    asyncio.run(serve(app))
    return 0


if __name__ == "__main__":
    sys.exit(main())
