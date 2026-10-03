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

Arguments fixed for the run (#285): ``McpServe(bind=bind)`` calls ``bind(args)`` once as
serving starts, and each served command tool with a field it names runs with that value;
the field leaves the tool's input schema, so a call passing it is refused as unknown.

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
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from ._command import Command, DangerLevel, build_command
from ._context import Ctx
from ._errors import ArgsCrashed, CliExit, ParseError, RegistrationError
from ._flags import Flag
from ._framework import CONFIRM_FLAG
from ._redact import REDACTED
from ._resources import dependency_params
from ._types import is_dataclass_type, type_hints
from ._values import CommandPath, ExitCodeName, InvalidValue

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
MCP_COMMAND_UNKNOWN = "MCP_COMMAND_UNKNOWN"
MCP_INSTRUCTIONS_INVALID = "MCP_INSTRUCTIONS_INVALID"
MCP_BIND_UNKNOWN = "MCP_BIND_UNKNOWN"
MCP_BIND_INVALID = "MCP_BIND_INVALID"
MCP_BIND_NEEDS_SERVE = "MCP_BIND_NEEDS_SERVE"
LIST_TOOLS = "list_tools"
CONFIRM_KEY = CONFIRM_FLAG.replace("-", "_")
CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"

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
type CommandSelector = Callable[[Any], Collection[str] | None]
type Instructions = str | Callable[[Any], str]
type Binder = Callable[[Any], Mapping[str, object]]


@dataclass(frozen=True, slots=True)
class McpServe:
    """``App(mcp=McpServe(...))`` adds the ``mcp serve`` built-in. ``args`` is the args
    dataclass of its startup flags, None for none. ``setup(args, ctx, *resources)``, if
    given, runs once before serving, takes resources as a handler does, and raises
    ``treaty.Exit`` to refuse (answered on stderr). ``tools(args, ctx)`` returns the
    ``McpTool`` values served beside the commands, called once as serving starts, after
    ``setup``. ``instructions`` is the server's instructions to the client: text, or a
    function of the startup arguments returning it. ``exit_codes`` names the app exit
    codes ``setup``, ``tools``, and ``bind`` may raise, registered with ``app.exit_code``
    before the first run. ``commands(args)`` returns the command paths served as tools for the
    startup arguments, such as ``{"fleet", "observe.logs"}``: None serves every command,
    an empty collection only the provided tools, and a path the app does not have is
    refused before serving. A command registered ``mcp=False`` is never served, whatever
    it returns (#281). ``bind(args)`` returns the argument values fixed for the run, such
    as ``{"project": "/srv/fleet"}``, keyed as a tool call's arguments are, values as JSON
    gives them: every served command tool with that field runs with the value, checked as
    a passed one is on each call; the field leaves the tool's input schema, and a call
    that passes it anyway is refused as an unknown field. A name no served command has, a
    secret field, or a value its field refuses fails before serving (#285)."""

    args: type | None = None
    setup: Callable[..., None] | None = None
    exit_codes: Sequence[str] = ()
    tools: ToolProvider | None = None
    instructions: Instructions | None = None
    commands: CommandSelector | None = None
    bind: Binder | None = None

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
        if self.commands is not None and not callable(self.commands):
            raise RegistrationError(
                "McpServe(commands=...) is a function (args) returning the command paths "
                "to serve, or None for every command"
            )
        if self.bind is not None and not callable(self.bind):
            raise RegistrationError(
                "McpServe(bind=...) is a function (args) returning the argument values "
                "fixed for the run, such as {'project': '/srv/fleet'}"
            )
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

    @property
    def gated(self) -> bool:
        """A tool advertised destructive runs only with ``confirm_destructive: true``, as a
        destructive command does over MCP (REQ-C-004)"""
        return advertised_destructive(self.tool)

    def secret_locations(self, arguments: Mapping[str, object]) -> list[tuple[str, ...]]:
        """Where ``arguments`` hold a value its input schema marks secret (``is_secret``)"""
        return secret_locations(self.validator, arguments)

    def secret_values(self, arguments: Mapping[str, object]) -> list[object]:
        """The values ``arguments`` holds at the secret locations, every scalar inside"""
        found: list[object] = []
        for path in self.secret_locations(arguments):
            found.extend(_scalars(_at(arguments, path)))
        return found

    def masked(self, arguments: Mapping[str, object]) -> dict[str, object]:
        """``arguments`` with each secret location's value ``[REDACTED]``"""
        secrets = set(self.secret_locations(arguments))
        if () in secrets:
            return {key: REDACTED for key in arguments}
        return {k: _masked(v, secrets, (str(k),)) for k, v in arguments.items()}

    def entry(self) -> ToolEntry:
        from ._tools import ToolEntry, output_schema

        tool = self.tool
        schema = dict(tool.input_schema)
        description = tool.description
        if self.gated:
            properties = schema.get("properties")
            schema["properties"] = {
                **(properties if isinstance(properties, Mapping) else {}),
                CONFIRM_KEY: {
                    "type": "boolean",
                    "default": False,
                    "description": "Required to apply; without it the call is refused with "
                    "CONFIRMATION_REQUIRED",
                },
            }
            description += (
                f" Destructive: without {CONFIRM_KEY}=true the call is refused with "
                "CONFIRMATION_REQUIRED and nothing runs."
            )
        return ToolEntry(
            name=tool.name,
            path=MCP_SERVE_PATH,
            description=description,
            input_schema=schema,
            output_schema=output_schema(self.command),
            read_only=tool.read_only_hint,
            destructive=tool.destructive_hint,
            idempotent=tool.level is DangerLevel.SAFE,
            open_world=False,
        )

    def problems(
        self, arguments: Mapping[str, object], redact: Callable[[str], str]
    ) -> list[dict[str, object]]:
        """Where ``arguments`` break the input schema: each error's path and message. An
        error at or under a secret path names only the rule it broke, never the value, and
        ``redact`` takes every secret value out of the other messages"""
        found = sorted(self.validator.iter_errors(arguments), key=lambda e: list(e.path))
        secrets = self.secret_locations(arguments)
        listed: list[dict[str, object]] = []
        for error in found:
            path = tuple(str(p) for p in error.path)
            secret = any(path[: len(s)] == s for s in secrets)
            message = (
                f"the secret value breaks the schema's {error.validator} rule"
                if secret
                else redact(error.message)
            )
            listed.append({"path": "/".join(path), "message": message})
        return listed


def is_secret(prop: object) -> bool:
    """A property of a provided tool's input schema that holds a secret: ``writeOnly:
    true``, ``format: "password"``, or ``"x-secret": true``"""
    return isinstance(prop, Mapping) and (
        prop.get("writeOnly") is True
        or prop.get("format") == "password"
        or prop.get("x-secret") is True
    )


def secret_locations(validator: Any, instance: object) -> list[tuple[str, ...]]:
    """The paths in ``instance`` whose value a subschema marks secret: one reached through
    ``properties``, ``patternProperties``, ``additionalProperties``, array items, a local
    ``$ref``, or any branch of ``allOf``, ``anyOf``, ``oneOf`` and ``if``/``then``/``else``.
    A branch the instance may not match counts too: redacting more never leaks"""
    from referencing import Registry
    from referencing.jsonschema import specification_with

    specification = specification_with(type(validator).META_SCHEMA["$schema"])
    root = specification.create_resource(validator.schema)
    found: list[tuple[str, ...]] = []

    def applicable(resolver: Any, schema: object) -> list[tuple[Any, Mapping[str, Any]]]:
        """``schema`` and every subschema that applies at the same instance location"""
        listed: list[tuple[Any, Mapping[str, Any]]] = []
        pending: list[tuple[Any, object]] = [(resolver, schema)]
        seen: set[int] = set()
        while pending:
            at, node = pending.pop()
            if not isinstance(node, Mapping) or id(node) in seen:
                continue
            seen.add(id(node))
            at = at.in_subresource(specification.create_resource(node))
            listed.append((at, node))
            for keyword in ("$ref", "$dynamicRef"):
                ref = node.get(keyword)
                if isinstance(ref, str):
                    resolved = at.lookup(ref)
                    pending.append((resolved.resolver, resolved.contents))
            for keyword in ("allOf", "anyOf", "oneOf"):
                branches = node.get(keyword)
                if isinstance(branches, list):
                    pending.extend((at, branch) for branch in branches)
            for keyword in ("if", "then", "else"):
                pending.append((at, node.get(keyword)))
            for keyword in ("dependentSchemas", "dependencies"):
                dependent = node.get(keyword)
                if isinstance(dependent, Mapping):
                    pending.extend((at, sub) for sub in dependent.values())
        return listed

    def children(
        schemas: list[tuple[Any, Mapping[str, Any]]], key: str | int
    ) -> list[tuple[Any, object]]:
        """The subschemas that apply to the ``key`` member or item"""
        reached: list[tuple[Any, object]] = []
        for at, node in schemas:
            if isinstance(key, str):
                matched = False
                properties = node.get("properties")
                if isinstance(properties, Mapping) and key in properties:
                    reached.append((at, properties[key]))
                    matched = True
                patterns = node.get("patternProperties")
                if isinstance(patterns, Mapping):
                    for pattern, sub in patterns.items():
                        if _matches(pattern, key):
                            reached.append((at, sub))
                            matched = True
                if not matched:
                    reached += [(at, node.get("additionalProperties"))]
                    reached += [(at, node.get("unevaluatedProperties"))]
            else:
                prefix = node.get("prefixItems")
                items = node.get("items")
                tuple_form = prefix if isinstance(prefix, list) else items
                if isinstance(tuple_form, list) and key < len(tuple_form):
                    reached.append((at, tuple_form[key]))
                else:
                    reached += [(at, items), (at, node.get("additionalItems"))]
                reached += [(at, node.get("contains")), (at, node.get("unevaluatedItems"))]
        return reached

    def walk(subschemas: list[tuple[Any, object]], value: object, path: tuple[str, ...]) -> None:
        schemas = [pair for at, sub in subschemas for pair in applicable(at, sub)]
        if any(is_secret(node) for _, node in schemas):
            found.append(path)
        elif isinstance(value, Mapping):
            for key, item in value.items():
                walk(children(schemas, str(key)), item, (*path, str(key)))
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                walk(children(schemas, index), item, (*path, str(index)))

    walk([(Registry().resolver_with_root(root), validator.schema)], instance, ())
    return found


def _matches(pattern: str, key: str) -> bool:
    """Whether a ``patternProperties`` pattern matches ``key``; one Python cannot compile
    counts as a match, since redacting more never leaks"""
    try:
        return re.search(pattern, key) is not None
    except re.error:
        return True


def _at(value: object, path: tuple[str, ...]) -> object:
    for key in path:
        if isinstance(value, Mapping):
            value = value.get(key)
        elif isinstance(value, (list, tuple)):
            value = value[int(key)]
    return value


def _scalars(value: object) -> list[object]:
    if isinstance(value, Mapping):
        return [s for v in value.values() for s in _scalars(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _scalars(v)]
    return [] if value is None or isinstance(value, bool) else [value]


def _masked(value: object, secrets: set[tuple[str, ...]], path: tuple[str, ...]) -> object:
    if path in secrets:
        return REDACTED
    if isinstance(value, Mapping):
        return {k: _masked(v, secrets, (*path, str(k))) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_masked(v, secrets, (*path, str(i))) for i, v in enumerate(value)]
    return value


def advertised_destructive(tool: McpTool) -> bool:
    """Whether the tool is advertised destructive: ``danger_level="destructive"`` or
    ``destructive=True``"""
    return tool.level is DangerLevel.DESTRUCTIVE or tool.destructive is True


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
    from ._tools import tool_name

    returned = provide(args, ctx)
    if isinstance(returned, (str, bytes, Mapping)) or not isinstance(returned, Sequence):
        raise TypeError(
            f"McpServe tools returned {type(returned).__name__}; it returns a list of McpTool"
        )
    # Every command's name, served or not: a provided tool never answers for a command
    # left off the server (#281)
    taken = {tool_name(path) for path in app.commands}
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
        properties = tool.input_schema.get("properties")
        if (
            advertised_destructive(tool)
            and isinstance(properties, Mapping)
            and CONFIRM_KEY in properties
        ):
            raise _invalid(
                tool.name,
                f"defines {CONFIRM_KEY}, which treaty adds to a destructive tool's input schema",
            )
        validator_class = validator_for(tool.input_schema, default=Draft202012Validator)
        try:
            validator_class.check_schema(tool.input_schema)
        except SchemaError as exc:
            raise _invalid(tool.name, f"has an invalid input schema: {exc.message}") from None
        unresolved = _unresolved_ref(tool.input_schema, validator_class)
        if unresolved is not None:
            raise _invalid(
                tool.name,
                f"refers to {unresolved!r}, which its input schema does not hold; "
                "nothing is fetched, so a reference resolves within the schema",
            )
        for name in tool.exit_codes:
            if ExitCodeName(name) not in app.exits:
                raise _invalid(tool.name, f"declares exit code {name}, which is not registered")
        try:
            command = _command_for(app, tool)
        except RegistrationError as exc:
            raise _invalid(tool.name, f"cannot be served: {exc}") from None
        found[tool.name] = Provided(tool, command, validator_class(tool.input_schema))
    return found


def _unresolved_ref(schema: Mapping[str, object], validator_class: Any) -> str | None:
    """The first ``$ref`` or ``$dynamicRef`` of ``schema`` that does not resolve within it,
    each against the base URI of the subschema it sits in. The registry retrieves nothing,
    so a remote reference never resolves; unchecked, it failed the first call that reached
    it, outside any envelope"""
    from referencing import Registry
    from referencing.exceptions import Unresolvable
    from referencing.jsonschema import specification_with

    specification = specification_with(validator_class.META_SCHEMA["$schema"])
    root = specification.create_resource(schema)
    pending = [(Registry().resolver_with_root(root), root)]
    while pending:
        resolver, resource = pending.pop()
        contents = resource.contents
        if isinstance(contents, Mapping):
            for keyword in ("$ref", "$dynamicRef"):
                ref = contents.get(keyword)
                if isinstance(ref, str):
                    try:
                        resolver.lookup(ref)
                    except Unresolvable:
                        return ref
        pending.extend((resolver.in_subresource(sub), sub) for sub in resource.subresources())
    return None


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


def served_commands(app: App, spec: McpServe, args: object) -> frozenset[CommandPath] | None:
    """``spec.commands(args)`` as command paths, each one the app has; None when it serves
    every command. Checked before serving, so a typo never quietly drops a tool (#281)"""
    select = spec.commands
    if select is None:
        return None
    returned = select(args)
    if returned is None:
        return None
    if isinstance(returned, (str, bytes, Mapping)) or not isinstance(returned, Collection):
        raise TypeError(
            f"McpServe commands returned {type(returned).__name__}; it returns a collection "
            "of command paths, or None for every command"
        )
    served: set[CommandPath] = set()
    for given in returned:
        if not isinstance(given, str):
            raise TypeError(
                f"McpServe commands returned a {type(given).__name__}; each command path is "
                "a str such as 'deploy.rollback'"
            )
        try:
            path: CommandPath | None = CommandPath(given)
        except InvalidValue:
            path = None
        if path is None or path not in app.commands:
            raise CliExit(
                ExitCodeName("PRECONDITION"),
                f"McpServe commands selected {given!r}, which is not a command of {app.name}",
                code=MCP_COMMAND_UNKNOWN,
                context={"command": given},
                fix_required="return command paths as the manifest names them, dots "
                "between the parts, such as 'deploy.rollback'",
            )
        served.add(path)
    return frozenset(served)


@dataclass(frozen=True, slots=True)
class Bindings:
    """The argument values ``McpServe(bind=)`` fixed for a server run, by payload key, as
    JSON gives them (#285)"""

    values: Mapping[str, object] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", types.MappingProxyType(dict(self.values)))

    def for_command(self, command: Command) -> Mapping[str, object]:
        """The bound values of the fields ``command`` has"""
        keys = {f.key for f in command.fields}
        return types.MappingProxyType({k: v for k, v in self.values.items() if k in keys})


NO_BINDINGS = Bindings()


def _bind_invalid(name: str, problem: str, **context: object) -> CliExit:
    return CliExit(
        ExitCodeName("PRECONDITION"),
        f"McpServe bind fixed {name!r}, which {problem}",
        code=MCP_BIND_INVALID,
        context={"field": name, **context},
        fix_required="correct what McpServe(bind=...) returns for the field",
    )


def bound_values(
    app: App, spec: McpServe, args: object, served: frozenset[CommandPath] | None
) -> Bindings:
    """``spec.bind(args)``, checked before serving (#285): each name a field of a served
    command tool, none of them a secret, and each value one its field accepts, as JSON
    gives it. A secret is never bound: treaty takes a secret only from a variable or a
    file a call names, never as a value, and the variable the server runs with already
    fixes it for every call. Values are copied through JSON, so ``bind`` keeps no handle
    on them"""
    from ._parse import check_bound_value
    from ._tools import tool_entries

    bind = spec.bind
    if bind is None:
        return NO_BINDINGS
    returned = bind(args)
    if not isinstance(returned, Mapping):
        raise TypeError(
            f"McpServe bind returned {type(returned).__name__}; it returns a mapping of "
            "field names to values"
        )
    commands = [app.commands[e.path] for e in tool_entries(app, served)]
    fixed: dict[str, object] = {}
    for name, value in returned.items():
        if not isinstance(name, str):
            raise TypeError(
                f"McpServe bind returned a {type(name).__name__} key; each key is a field "
                "name as a tool call passes it, such as 'project'"
            )
        try:
            copied = json.loads(json.dumps(value, allow_nan=False))
        except TypeError, ValueError:
            raise _bind_invalid(
                name, f"is a {type(value).__name__}, not a JSON value", type=type(value).__name__
            ) from None
        fields = [(c, f) for c in commands for f in c.fields if f.key == name]
        if not fields:
            raise CliExit(
                ExitCodeName("PRECONDITION"),
                f"McpServe bind fixed {name!r}, which no served command tool has",
                code=MCP_BIND_UNKNOWN,
                context={"field": name},
                fix_required="return field names as the tools' input schemas name them, "
                "underscores between the words, such as 'dry_run'",
            )
        for command, field in fields:
            where = {"command": command.path.value}
            if field.secret:
                raise _bind_invalid(
                    name,
                    "is a secret; a secret is read from a variable or a file, never bound",
                    **where,
                )
            try:
                check_bound_value(field, copied)
            except ParseError as exc:
                raise _bind_invalid(name, f"its field refuses: {exc.message}", **where) from None
            except ArgsCrashed as exc:  # an object's __post_init__: a bug in user code
                raise exc.cause from None
        fixed[name] = copied
    return Bindings(fixed)


def instructions_for(app: App, spec: McpServe, args: object) -> str:
    given = spec.instructions
    if given is None:
        return DEFAULT_INSTRUCTIONS.format(description=app.description or app.name)
    text = given if isinstance(given, str) else given(args)
    if not isinstance(text, str) or not text.strip():
        got = "empty text" if isinstance(text, str) else f"a {type(text).__name__}"
        raise CliExit(
            ExitCodeName("PRECONDITION"),
            f"the server's instructions are {got}; McpServe(instructions=...) gives the text "
            "an MCP client reads, so it cannot be empty",
            code=MCP_INSTRUCTIONS_INVALID,
            fix_required="return non-empty text from McpServe(instructions=...)",
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
        served = served_commands(app, spec, args)
        bindings = bound_values(app, spec, args, served)
        provided = provided_tools(app, spec, args, ctx)
        if getattr(args, LIST_TOOLS):
            from ._tools import tool_list

            extra = [p.entry() for p in provided.values()]
            listed = tool_list(app, extra=extra, served=served, bindings=bindings)
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

        return serve_wire(
            app,
            wire,
            env=ctx.env,
            provided=provided,
            instructions=text,
            served=served,
            bindings=bindings,
        )

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
