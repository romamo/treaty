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

Tools from runtime data (#240): ``McpServe(tools=provide)`` calls ``provide(args, ctx)``
once as serving starts, and lists each ``McpTool`` it returns beside the command tools. A
provided tool runs as a command does, through ``App.call``'s path: its arguments are
checked against its input schema first, and its handler's result is enveloped and checked
against the output schema its return annotation gives.

Nothing here imports the ``mcp`` package; the server itself is ``_mcp.serve_wire``.
``jsonschema``, which the ``mcp`` extra brings, is imported only once tools are provided.
"""

from __future__ import annotations

import contextvars
import dataclasses
import importlib.util
import inspect
import json
import re
import types
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from ._command import Command, DangerLevel, build_command
from ._context import Ctx
from ._errors import CliExit, RegistrationError
from ._flags import Flag
from ._resources import dependency_params
from ._types import is_dataclass_type, type_hints
from ._values import CommandPath, ExitCodeName

if TYPE_CHECKING:
    from ._app import App
    from ._tools import ToolEntry

MCP_GROUP = CommandPath("mcp")
MCP_SERVE_PATH = MCP_GROUP.child("serve")
NEEDS_STDIO = "NEEDS_STDIO"
MCP_SDK_MISSING = "MCP_SDK_MISSING"
STDIN_CLOSED = "STDIN_CLOSED"
MCP_TOOL_INVALID = "MCP_TOOL_INVALID"
MCP_TOOL_NAME_TAKEN = "MCP_TOOL_NAME_TAKEN"
LIST_TOOLS = "list_tools"

DESCRIPTION = (
    "Serve the app's commands as MCP tools over stdio until stdin ends or SIGINT or "
    "SIGTERM arrives, then exit 0. Stdout carries the MCP protocol (JSON-RPC lines), never "
    "an envelope: a failure before serving, and the end of the run, answer with the "
    "envelope as one JSON line on stderr. --list-tools prints the tool list as JSON instead"
)
"""The manifest marks the command's stdout as a protocol in its description: the spec has
no key for it yet (cli-agent-spec/cli-agent-spec#51)"""

_TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
"""MCP's tool name characters and length (SEP-986)"""

DEFAULT_INSTRUCTIONS = (
    "{description}. Every result is a CLI Agent Spec response envelope: read ok, then data, "
    "else error.code and error.fix_required. Field names use underscores. Call the manifest "
    "tool for the full contract."
)


@dataclass(frozen=True, slots=True)
class McpTool:
    """A tool ``McpServe(tools=...)`` provides from runtime data (#240). ``input_schema``
    is the JSON Schema of its arguments, an object schema; a call's arguments are
    checked against it before ``handler(arguments, ctx)`` runs. The handler's return
    annotation gives the output schema its result is checked against, as a command's
    does. ``danger_level`` sets the tool's hints (``safe`` read-only and idempotent,
    ``destructive`` destructive); ``read_only`` and ``destructive`` override them, so a
    tool that only previews may still be flagged destructive. ``exit_codes`` names the
    app exit codes the handler may raise."""

    name: str
    description: str
    input_schema: Mapping[str, object]
    handler: Callable[[Mapping[str, object], Ctx], object]
    danger_level: DangerLevel | str = DangerLevel.SAFE
    read_only: bool | None = None
    destructive: bool | None = None
    exit_codes: Sequence[str] = ()

    def __post_init__(self) -> None:
        where = f"McpTool {self.name!r}"
        if not isinstance(self.name, str) or not _TOOL_NAME.fullmatch(self.name):
            raise ValueError(f"{where}: a tool name is 1 to 128 letters, digits, '_', '-', or '.'")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError(f"{where}: description is required")
        schema = self.input_schema
        if not isinstance(schema, Mapping) or schema.get("type") != "object":
            raise ValueError(f'{where}: input_schema is a JSON Schema with "type": "object"')
        if not callable(self.handler):
            raise ValueError(f"{where}: handler is a function (arguments, ctx)")
        try:
            level = DangerLevel(self.danger_level)
        except ValueError:
            raise ValueError(
                f"{where}: danger_level is one of {', '.join(d.value for d in DangerLevel)}"
            ) from None
        for hint in (self.read_only, self.destructive):
            if hint is not None and not isinstance(hint, bool):
                raise ValueError(f"{where}: read_only and destructive are True, False, or None")
        if isinstance(self.exit_codes, str) or not all(isinstance(c, str) for c in self.exit_codes):
            raise ValueError(f"{where}: exit_codes is a sequence of exit code names")
        object.__setattr__(self, "input_schema", dict(schema))
        object.__setattr__(self, "danger_level", level)
        object.__setattr__(self, "exit_codes", tuple(self.exit_codes))

    @property
    def level(self) -> DangerLevel:
        assert isinstance(self.danger_level, DangerLevel)  # converted in __post_init__
        return self.danger_level

    @property
    def read_only_hint(self) -> bool:
        return self.level is DangerLevel.SAFE if self.read_only is None else self.read_only

    @property
    def destructive_hint(self) -> bool:
        if self.destructive is not None:
            return self.destructive
        return self.level is DangerLevel.DESTRUCTIVE


type ToolProvider = Callable[[Any, Ctx], Sequence[McpTool]]
type Instructions = str | Callable[[Any], str]


@dataclass(frozen=True, slots=True)
class McpServe:
    """``App(mcp=McpServe(...))`` adds the ``mcp serve`` built-in. ``args`` is the args
    dataclass of its startup flags, None for none. ``setup(args, ctx, *resources)``, if
    given, runs once before serving, takes resources as a handler does, and raises
    ``treaty.Exit`` to refuse (answered on stderr). ``tools(args, ctx)`` returns the
    ``McpTool`` values served beside the commands, called once as serving starts, after
    ``setup``. ``instructions`` is the server's instructions to the client: text, or a
    function of the startup arguments returning it. ``exit_codes`` names the app exit
    codes ``setup`` and ``tools`` may raise, registered with ``app.exit_code`` before the
    first run."""

    args: type | None = None
    setup: Callable[..., None] | None = None
    exit_codes: Sequence[str] = ()
    tools: ToolProvider | None = None
    instructions: Instructions | None = None

    def __post_init__(self) -> None:
        if self.args is not None and not is_dataclass_type(self.args):
            raise RegistrationError(
                "McpServe(args=...) is the args dataclass of mcp serve's startup flags, or None"
            )
        if self.args is not None and any(
            f.name == LIST_TOOLS for f in dataclasses.fields(self.args)
        ):
            raise RegistrationError(
                f"McpServe(args=...): {self.args.__qualname__}.{LIST_TOOLS} is mcp serve's "
                "own --list-tools; rename the field"
            )
        if self.setup is not None and not callable(self.setup):
            raise RegistrationError("McpServe(setup=...) is a function (args, ctx, *resources)")
        if self.tools is not None and not callable(self.tools):
            raise RegistrationError("McpServe(tools=...) is a function (args, ctx)")
        instructions = self.instructions
        if instructions is not None and not (
            callable(instructions) or (isinstance(instructions, str) and instructions.strip())
        ):
            raise RegistrationError(
                "McpServe(instructions=...) is text, or a function of the startup arguments"
            )
        if isinstance(self.exit_codes, str) or not all(isinstance(c, str) for c in self.exit_codes):
            raise RegistrationError(
                "McpServe(exit_codes=...) is a sequence of exit code names, such as "
                "('PRECONDITION',)"
            )
        object.__setattr__(self, "exit_codes", tuple(self.exit_codes))


@dataclass(frozen=True, slots=True)
class McpServed:
    """How a server run ended: the client left, a signal stopped it, or ``--list-tools``
    printed the tools without serving"""

    stopped_by: Literal["eof", "SIGINT", "SIGTERM", "list-tools"]
    """``eof`` when the client left: stdin ended, or stdout was closed"""
    tool_calls: int
    """Tool calls answered during the run"""


@dataclass(frozen=True, slots=True)
class Provided:
    """A provided tool ready to list and call: its synthetic command runs the handler"""

    tool: McpTool
    command: Command
    validator: Any
    """The ``jsonschema`` validator of its input schema"""

    def entry(self) -> ToolEntry:
        from ._tools import ToolEntry, output_schema

        tool = self.tool
        return ToolEntry(
            name=tool.name,
            path=MCP_SERVE_PATH,
            description=tool.description,
            input_schema=dict(tool.input_schema),
            output_schema=output_schema(self.command),
            read_only=tool.read_only_hint,
            destructive=tool.destructive_hint,
            idempotent=tool.level is DangerLevel.SAFE,
            open_world=False,
        )

    def problems(self, arguments: Mapping[str, object]) -> list[dict[str, object]]:
        """Where ``arguments`` break the input schema: each error's path and message"""
        found = sorted(self.validator.iter_errors(arguments), key=lambda e: list(e.path))
        return [
            {"path": "/".join(str(p) for p in error.path), "message": error.message}
            for error in found
        ]


_ARGUMENTS: contextvars.ContextVar[Mapping[str, object]] = contextvars.ContextVar(
    "treaty_mcp_arguments"
)
"""The arguments of the provided tool call that runs now, for its handler; the handler runs
in a copy of the calling context"""


def bound(arguments: Mapping[str, object]) -> contextvars.Token[Mapping[str, object]]:
    return _ARGUMENTS.set(types.MappingProxyType(dict(arguments)))


def unbound(token: contextvars.Token[Mapping[str, object]]) -> None:
    _ARGUMENTS.reset(token)


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


def _invalid(name: str, problem: str) -> CliExit:
    return CliExit(
        ExitCodeName("PRECONDITION"),
        f"the provided MCP tool {name!r} {problem}",
        code=MCP_TOOL_INVALID,
        context={"tool": name},
        fix_required="correct the tool where McpServe(tools=...) builds it",
    )


def provided_tools(app: App, spec: McpServe, args: object, ctx: Ctx) -> dict[str, Provided]:
    """``spec.tools(args, ctx)``, each checked: an ``McpTool``, a valid input schema, a
    name no command tool or other provided tool has, and an output type treaty can check.
    Raised before serving, so a bad catalog never reaches a client"""
    provide = spec.tools
    if provide is None:
        return {}
    try:
        from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]
        from jsonschema.validators import (  # type: ignore[import-untyped]
            Draft202012Validator,
            validator_for,
        )
    except ModuleNotFoundError:
        raise CliExit(
            ExitCodeName("PRECONDITION"),
            "provided MCP tools need the jsonschema package, which is not installed",
            code=MCP_SDK_MISSING,
            fix_required="install the mcp extra: treaty[mcp]",
        ) from None
    from ._tools import tool_entries

    returned = provide(args, ctx)
    if isinstance(returned, (str, bytes, Mapping)) or not isinstance(returned, Sequence):
        raise TypeError(
            f"McpServe tools returned {type(returned).__name__}; it returns a list of McpTool"
        )
    taken = {e.name for e in tool_entries(app)}
    found: dict[str, Provided] = {}
    for tool in returned:
        if not isinstance(tool, McpTool):
            raise TypeError(
                f"McpServe tools returned a {type(tool).__name__}; each tool is a treaty.McpTool"
            )
        if tool.name in taken or tool.name in found:
            what = "a command's tool" if tool.name in taken else "another provided tool"
            raise CliExit(
                ExitCodeName("PRECONDITION"),
                f"the provided MCP tool {tool.name!r} has the name of {what}",
                code=MCP_TOOL_NAME_TAKEN,
                context={"tool": tool.name},
                fix_required="rename the provided tool; tool names are unique",
            )
        validator_class = validator_for(tool.input_schema, default=Draft202012Validator)
        try:
            validator_class.check_schema(tool.input_schema)
        except SchemaError as exc:
            raise _invalid(tool.name, f"has an invalid input schema: {exc.message}") from None
        for name in tool.exit_codes:
            if ExitCodeName(name) not in app.exits:
                raise _invalid(tool.name, f"declares exit code {name}, which is not registered")
        try:
            command = _command_for(app, tool)
        except RegistrationError as exc:
            raise _invalid(tool.name, f"cannot be served: {exc}") from None
        found[tool.name] = Provided(tool, command, validator_class(tool.input_schema))
    return found


def _command_for(app: App, tool: McpTool) -> Command:
    """The command a provided tool runs as: no flags, the handler's return type as its
    output, safe whatever its hints say, since treaty cannot preview or deduplicate the
    tool's own work; its arguments come from the call (``bound``)"""
    from ._app import NoArgs

    hints = type_hints(tool.handler)
    if "return" not in hints:
        raise RegistrationError("its handler needs a return annotation for the output schema")
    handler = tool.handler

    def run(args: NoArgs, ctx: Ctx) -> object:
        return handler(_ARGUMENTS.get(), ctx)

    run.__annotations__ = {"args": NoArgs, "ctx": Ctx, "return": hints["return"]}
    return build_command(
        run,
        app_name=app.name,
        path=MCP_SERVE_PATH,
        description=tool.description,
        danger_level=DangerLevel.SAFE,
        required_scopes=(),
        exit_codes=tuple(ExitCodeName(c) for c in tool.exit_codes),
        examples=(),
        has_network_io=False,
        timeout=None,
        supports_raw_payload=False,
        cleanup=None,
        renderers={},
        media_types={},
        scalars=app.scalars,
        args_adapters=app.args_adapters,
    )


def instructions_for(app: App, spec: McpServe, args: object) -> str:
    given = spec.instructions
    if given is None:
        return DEFAULT_INSTRUCTIONS.format(description=app.description or app.name)
    text = given if isinstance(given, str) else given(args)
    if not isinstance(text, str) or not text.strip():
        raise TypeError(
            f"McpServe instructions returned {type(text).__name__}; it returns the text"
        )
    return text


def register_mcp_serve(app: App, spec: McpServe) -> CommandPath:
    """The ``mcp serve`` built-in: its handler takes ``setup``'s resources, so they are
    acquired and released as any handler's are. Its args extend the app's with
    ``--list-tools``"""
    from ._app import NoArgs  # loaded: the App being built calls this

    app.group(MCP_GROUP.value, description="Serve the app to MCP clients")
    setup = spec.setup
    resources = () if setup is None else dependency_params(setup, "McpServe setup")
    base = NoArgs if spec.args is None else spec.args
    args_type = dataclasses.make_dataclass(
        base.__name__,
        [
            (
                LIST_TOOLS,
                bool,
                Flag(
                    default=False,
                    description="Print the tool list as JSON on stdout, as treaty-mcp "
                    "--list-tools does, and exit without serving; the tools given these "
                    "startup flags provide are listed too",
                ),
            )
        ],
        bases=(base,),
        frozen=True,
        slots=True,
        module=base.__module__,
    )

    def serve(args: object, ctx: Ctx, *acquired: object) -> McpServed:
        wire = ctx._wire
        if wire is None:
            raise needs_stdio(app.name)
        if setup is not None:
            returned = setup(args, ctx, *acquired)
            if returned is not None:
                raise TypeError(
                    f"McpServe setup returned {type(returned).__name__}; it returns None, "
                    "and raises treaty.Exit to refuse"
                )
        provided = provided_tools(app, spec, args, ctx)
        if getattr(args, LIST_TOOLS):
            from ._tools import tool_list

            listed = tool_list(app, extra=[p.entry() for p in provided.values()])
            wire.out.write(json.dumps(listed, indent=2, sort_keys=True) + "\n")
            wire.out.flush()
            return McpServed("list-tools", 0)
        if importlib.util.find_spec("mcp") is None:
            raise CliExit(
                ExitCodeName("PRECONDITION"),
                "mcp serve needs the mcp package, which is not installed",
                code=MCP_SDK_MISSING,
                fix_required="install the mcp extra: treaty[mcp]",
            )
        if wire.stdin is None:
            raise CliExit(
                ExitCodeName("PRECONDITION"),
                "stdin is closed, so no MCP client can send requests",
                code=STDIN_CLOSED,
                fix_required="start mcp serve from an MCP client, with stdin and stdout piped",
            )
        text = instructions_for(app, spec, args)
        from ._mcp import serve_wire  # imports the App module, which imports this one

        return serve_wire(app, wire, env=ctx.env, provided=provided, instructions=text)

    # The handler's signature is setup's, so the resources setup asks for are resolved
    kind = inspect.Parameter.POSITIONAL_OR_KEYWORD
    names = [f"resource_{n}" for n in range(len(resources))]
    signature = inspect.Signature(
        [inspect.Parameter(n, kind) for n in ("args", "ctx", *names)],
        return_annotation=McpServed,
    )
    setattr(serve, "__signature__", signature)  # noqa: B010 - functions do not declare it
    serve.__annotations__ = {
        "args": args_type,
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
        examples=[
            ("Serve the tools to an MCP client over stdio", f"{app.name} mcp serve"),
            ("Save the tool list for mcp-validate", f"{app.name} mcp serve --list-tools"),
        ],
    )(serve)
    return MCP_SERVE_PATH
