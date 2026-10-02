"""``mcp serve``: an app's own MCP stdio server, declared with ``App(mcp=McpServe(...))``
(#239).

Its startup arguments are an args dataclass like any command's: parsed, validated, listed
in ``--schema``, and handed to ``setup``, which takes resources as a handler does and
raises ``treaty.Exit`` to refuse. The command runs from argv only. Its stdout and stdin
carry the protocol from the first byte, so its envelope (on a failure before serving, and
when the server stops) is a JSON line on stderr, as a passthrough command's is. While it
serves, ``sys.stdout`` and descriptor 1 still lead to stderr: a stray ``print()`` never
reaches the protocol, which is written to a copy of the original stdout.

The spec has no manifest key for a command whose stdout is a protocol rather than an
envelope (cli-agent-spec/cli-agent-spec#51), so the command's description says so.

Nothing here imports the ``mcp`` package; the server itself is ``_mcp.serve_wire``.
"""

from __future__ import annotations

import importlib.util
import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ._context import Ctx
from ._errors import CliExit, RegistrationError
from ._resources import dependency_params
from ._types import is_dataclass_type
from ._values import CommandPath, ExitCodeName

if TYPE_CHECKING:
    from ._app import App

MCP_GROUP = CommandPath("mcp")
MCP_SERVE_PATH = MCP_GROUP.child("serve")
NEEDS_STDIO = "NEEDS_STDIO"
MCP_SDK_MISSING = "MCP_SDK_MISSING"
STDIN_CLOSED = "STDIN_CLOSED"

DESCRIPTION = (
    "Serve the app's commands as MCP tools over stdio until stdin ends or SIGINT or "
    "SIGTERM arrives, then exit 0. Stdout carries the MCP protocol (JSON-RPC lines), never "
    "an envelope: a failure before serving, and the end of the run, answer with the "
    "envelope as one JSON line on stderr"
)
"""The manifest marks the command's stdout as a protocol in its description: the spec has
no key for it yet (cli-agent-spec/cli-agent-spec#51)"""


@dataclass(frozen=True, slots=True)
class McpServe:
    """``App(mcp=McpServe(...))`` adds the ``mcp serve`` built-in. ``args`` is the args
    dataclass of its startup flags, None for none. ``setup(args, ctx, *resources)``, if
    given, runs once before serving, takes resources as a handler does, and raises
    ``treaty.Exit`` to refuse (answered on stderr); ``exit_codes`` names the app exit
    codes ``setup`` may raise, registered with ``app.exit_code`` before the first run."""

    args: type | None = None
    setup: Callable[..., None] | None = None
    exit_codes: Sequence[str] = ()

    def __post_init__(self) -> None:
        if self.args is not None and not is_dataclass_type(self.args):
            raise RegistrationError(
                "McpServe(args=...) is the args dataclass of mcp serve's startup flags, or None"
            )
        if self.setup is not None and not callable(self.setup):
            raise RegistrationError("McpServe(setup=...) is a function (args, ctx, *resources)")
        if isinstance(self.exit_codes, str) or not all(isinstance(c, str) for c in self.exit_codes):
            raise RegistrationError(
                "McpServe(exit_codes=...) is a sequence of exit code names, such as "
                "('PRECONDITION',)"
            )
        object.__setattr__(self, "exit_codes", tuple(self.exit_codes))


@dataclass(frozen=True, slots=True)
class McpServed:
    """How a server run ended: the client left, or a signal stopped it"""

    stopped_by: Literal["eof", "SIGINT", "SIGTERM"]
    """``eof`` when the client left: stdin ended, or stdout was closed"""
    tool_calls: int
    """Tool calls answered during the run"""


def protocol_command(app: App, path: CommandPath) -> bool:
    """Whether the command at ``path`` is ``mcp serve``, whose stdout is the protocol's"""
    return app.mcp is not None and path == MCP_SERVE_PATH and path in app.builtins


def needs_stdio(app_name: str) -> CliExit:
    """``mcp serve`` from an ``exec`` line or ``App.call``, which have no stdout to give it"""
    return CliExit(
        ExitCodeName("PRECONDITION"),
        "mcp serve runs only from the command line, where it owns stdout and stdin",
        code=NEEDS_STDIO,
        context={"command": MCP_SERVE_PATH.value},
        fix_command=f"{app_name} mcp serve",
    )


def register_mcp_serve(app: App, spec: McpServe) -> CommandPath:
    """The ``mcp serve`` built-in: its handler takes ``setup``'s resources, so they are
    acquired and released as any handler's are"""
    from ._app import NoArgs  # loaded: the App being built calls this

    app.group(MCP_GROUP.value, description="Serve the app to MCP clients")
    setup = spec.setup
    resources = () if setup is None else dependency_params(setup, "McpServe setup")

    def serve(args: object, ctx: Ctx, *acquired: object) -> McpServed:
        wire = ctx._wire
        if wire is None:
            raise needs_stdio(app.name)
        if importlib.util.find_spec("mcp") is None:
            raise CliExit(
                ExitCodeName("PRECONDITION"),
                "mcp serve needs the mcp package, which is not installed",
                code=MCP_SDK_MISSING,
                fix_required="install the mcp extra: treaty[mcp]",
            )
        if setup is not None:
            returned = setup(args, ctx, *acquired)
            if returned is not None:
                raise TypeError(
                    f"McpServe setup returned {type(returned).__name__}; it returns None, "
                    "and raises treaty.Exit to refuse"
                )
        if wire.stdin is None:
            raise CliExit(
                ExitCodeName("PRECONDITION"),
                "stdin is closed, so no MCP client can send requests",
                code=STDIN_CLOSED,
                fix_required="start mcp serve from an MCP client, with stdin and stdout piped",
            )
        from ._mcp import serve_wire  # imports the App module, which imports this one

        return serve_wire(app, wire, env=ctx.env)

    # The handler's signature is setup's, so the resources setup asks for are resolved
    kind = inspect.Parameter.POSITIONAL_OR_KEYWORD
    names = [f"resource_{n}" for n in range(len(resources))]
    signature = inspect.Signature(
        [inspect.Parameter(n, kind) for n in ("args", "ctx", *names)],
        return_annotation=McpServed,
    )
    setattr(serve, "__signature__", signature)  # noqa: B010 - functions do not declare it
    serve.__annotations__ = {
        "args": NoArgs if spec.args is None else spec.args,
        "ctx": Ctx,
        **dict(zip(names, resources, strict=True)),
        "return": McpServed,
    }
    app.command(
        MCP_SERVE_PATH.value,
        description=DESCRIPTION,
        danger_level="safe",
        exit_codes=(),  # the app's codes join once registered: App._declare_serve_exits
        timeout=None,  # a server runs until its client leaves
        examples=[("Serve the tools to an MCP client over stdio", f"{app.name} mcp serve")],
    )(serve)
    return MCP_SERVE_PATH
