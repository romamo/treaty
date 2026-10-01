"""``treaty scaffold-from``: a treaty module from a click, typer, or argparse command tree.

The tree is read from the live objects, so the target module is imported and its
top-level code runs. Each command becomes an args dataclass, with the options of the
groups above it on base classes, and a handler that returns its arguments with effect
``noop``; ``danger_level`` and ``exit_codes`` are left for the author as ``mutating`` and
``()``, which ``treaty audit`` reports until they are declared.

click and typer are imported only when the target is one of theirs: treaty does not
depend on either.
"""

from __future__ import annotations

import argparse
import enum
import inspect
import json
import keyword
import re
import sys
import types
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from ._app import App, NoArgs
from ._context import Ctx
from ._errors import Exit, RegistrationError
from ._framework import RESERVED_GLOBAL, RESERVED_OPT_IN, framework_flags
from ._parse import SWITCH_GLOBALS, VALUED_GLOBALS
from ._redact import secret_name
from ._values import CommandPath, InvalidValue

Kind = Literal["typer", "click", "argparse"]

BUILT_IN_COMMANDS = frozenset({"manifest", "version", "exec"})
"""Reserved command names: treaty answers them itself"""
BUILT_IN_FLAGS = frozenset({"verbose", "quiet", "debug", "help", "version"})
"""Options the framework gives every command: the old CLI's are dropped"""
RENAMED_FLAGS: Mapping[str, str] = {
    "format": "output_format",
    "config": "config_file",
    "schema": "schema_file",
    "fields": "field_names",
    "cwd": "work_dir",
    "output": "output_path",
}
"""A clearer name than ``<name>_value`` for an option that collides with treaty's"""
BUILT_IN_REASON = "built in on every treaty command: -v/--verbose, -vv/--debug, -q/--quiet"
CONFIRMATIONS = frozenset({"yes", "force_yes", "assume_yes"})
"""Options that skip a confirmation: a destructive danger level replaces them"""
WIDTH = 88

_TYPER_COMPLETION = frozenset({"install_completion", "show_completion"})
_WORD = re.compile(r"[^0-9a-zA-Z]+")


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    """A Python identifier; the flag is its kebab-case spelling"""
    annotation: str
    description: str
    positional: bool = False
    default: str | None = None
    """Source text of the default; None when the field is required"""
    short: str | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CommandSpec:
    path: tuple[str, ...]
    description: str
    fields: tuple[FieldSpec, ...]
    origin: str | None
    """``module:qualname`` of the function the old CLI ran"""


@dataclass(frozen=True, slots=True)
class GroupSpec:
    path: tuple[str, ...]
    description: str
    fields: tuple[FieldSpec, ...]
    children: tuple[GroupSpec | CommandSpec, ...]


@dataclass(frozen=True, slots=True)
class Skipped:
    path: str
    reason: str


@dataclass(slots=True)
class _Walk:
    """What one walk collects besides the tree"""

    reserved: frozenset[str]
    env: Mapping[str, str]
    """The environment the target was imported under, to spot a default read from it"""
    skipped: list[Skipped] = field(default_factory=list)
    renamed: list[tuple[str, str, str]] = field(default_factory=list)
    """(command, old flag, new flag)"""


@dataclass(frozen=True, slots=True)
class Scaffold:
    name: str
    description: str
    target: str
    kind: Kind
    root: GroupSpec
    skipped: tuple[Skipped, ...]
    renamed: tuple[tuple[str, str, str], ...]

    def commands(self) -> Iterator[CommandSpec]:
        yield from _commands(self.root)


def _commands(group: GroupSpec) -> Iterator[CommandSpec]:
    for child in group.children:
        if isinstance(child, GroupSpec):
            yield from _commands(child)
        else:
            yield child


def reserved_flags() -> frozenset[str]:
    """Every flag name a scaffolded command cannot take: the globals and what treaty adds
    to a mutating command, the danger level every scaffolded command starts at"""
    probe = App("scaffold-probe", version="0.0.0")

    @probe.command("probe", description="Probe", danger_level="mutating", exit_codes=())
    def _probe(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"effect": "noop"}

    command = probe.commands[CommandPath("probe")]
    names = {f.name for f in framework_flags(command)} | set(RESERVED_GLOBAL)
    names |= {n for n, applies in RESERVED_OPT_IN.items() if applies(command)}
    return frozenset(names | VALUED_GLOBALS | SWITCH_GLOBALS | {"help", "schema"})


# Names


def identifier(text: str) -> str:
    """``dry-run`` as ``dry_run``; a keyword gets a trailing underscore, as treaty reads it"""
    name = _WORD.sub("_", text).strip("_").lower()
    if not name:
        raise ValueError(f"{text!r} has no letters or digits to name a field after")
    if name[0].isdigit():
        name = f"n_{name}"
    return f"{name}_" if keyword.iskeyword(name) else name


def camel(parts: Sequence[str]) -> str:
    return "".join(w.capitalize() for p in parts for w in _WORD.split(p) if w) or "Root"


def flag_of(name: str) -> str:
    stripped = name[:-1] if name.endswith("_") and keyword.iskeyword(name[:-1]) else name
    return stripped.replace("_", "-")


def _description(text: str | None, fallback: str) -> str:
    words = " ".join((text or "").split())
    if not words or words == argparse.SUPPRESS:
        return fallback
    return words.removesuffix(".") if words.count(".") == 1 else words


def _literal(value: object) -> str | None:
    """Python source for ``value``, or None when it has no literal spelling"""
    if isinstance(value, enum.Enum):
        return _literal(value.value)
    if isinstance(value, str):
        return _q(value)
    if value is None or isinstance(value, (bool, int, float)):
        if isinstance(value, float) and value != value:  # NaN
            return None
        return repr(value)
    if isinstance(value, Path):
        return f"Path({_q(str(value))})"
    if isinstance(value, (list, tuple)):
        items = [_literal(v) for v in value]
        if any(i is None for i in items):
            return None
        inner = ", ".join(i for i in items if i is not None)
        return f"({inner},)" if len(items) == 1 else f"({inner})"
    return None


def _choices_annotation(choices: Sequence[object]) -> str | None:
    values = [c.value if isinstance(c, enum.Enum) else c for c in choices]
    if values and all(isinstance(v, (str, int)) and not isinstance(v, bool) for v in values):
        return f"Literal[{', '.join(_q(v) if isinstance(v, str) else repr(v) for v in values)}]"
    return None


@dataclass(frozen=True, slots=True)
class _Param:
    """One option or argument, whatever the framework"""

    name: str
    long: str | None
    """The long option without its dashes; None for a positional or a short-only option"""
    short: str | None
    positional: bool
    base: str
    """The item annotation, such as ``int`` or ``Literal['a', 'b']``"""
    many: bool
    required: bool
    default: object
    has_default: bool
    help: str | None
    switch: bool = False
    counter: bool = False
    hidden: bool = False
    """The old CLI hid what was typed for it, as for a password"""
    notes: tuple[str, ...] = ()


_MIN_ENV_VALUE = 4
"""Shorter environment values, such as ``1`` or ``en``, match too many defaults by chance"""


def _withheld(param: _Param, walk: _Walk) -> str | None:
    """Why the default of ``param`` stays out of the module, or None to write it

    The module is meant to be committed, and a default evaluated at import time, such as
    ``default=os.environ["TOKEN"]``, holds whatever the environment had."""
    values = param.default if isinstance(param.default, (list, tuple)) else (param.default,)
    strings = [v for v in values if isinstance(v, str) and v]
    if not strings:
        return None
    if param.hidden or secret_name(param.long or param.name):
        return "it may be a secret: read it from a treaty secret or App(settings=)"
    for value in strings:
        if len(value) < _MIN_ENV_VALUE:
            continue
        found = next((k for k, v in walk.env.items() if v == value), None)
        if found is not None:
            return f"it was read from ${found} at import: move it to App(settings=)"
    return None


def _field(param: _Param, where: str, walk: _Walk) -> FieldSpec | None:
    """The dataclass field for ``param``, or None for an option treaty provides itself"""
    raw = identifier(param.long or param.name)
    if raw in BUILT_IN_FLAGS and not param.positional:
        if raw not in ("help", "version"):
            walk.skipped.append(Skipped(f"{where} --{flag_of(raw)}", BUILT_IN_REASON))
        return None
    if raw in CONFIRMATIONS and param.switch:
        walk.skipped.append(
            Skipped(
                f"{where} --{flag_of(raw)}",
                "a confirmation switch: declare danger_level='destructive', which adds "
                "--confirm-destructive",
            )
        )
        return None
    if param.switch and param.default is True and raw.startswith("no_") and param.long:
        # store_false on --no-wait: treaty's --no-<name> of a True boolean does that
        raw = raw.removeprefix("no_")
    name, notes = raw, list(param.notes)
    if raw != identifier(param.name):
        notes.append(f"the old code read it as {param.name}")
    if not param.positional and _collides(name, param.switch, walk.reserved):
        renamed = RENAMED_FLAGS.get(name, f"{name.rstrip('_')}_value")
        walk.renamed.append((where, flag_of(name), flag_of(renamed)))
        notes.append(f"--{flag_of(name)} is treaty's; renamed --{flag_of(renamed)}")
        name = renamed
    withheld = None if param.switch or param.counter else _withheld(param, walk)
    if withheld is not None:
        notes.append(f"the default was left out: {withheld}")
        param = replace(param, default=None, has_default=False)
    base = param.base
    annotation, default = base, None
    if param.counter:
        annotation, default = "int", _literal(param.default) or "0"
    elif param.switch:
        annotation = "bool"
        default = "True" if param.default is True else "False"
    elif param.many:
        annotation = f"tuple[{base}, ...]"
        if not param.required:
            values = param.default if isinstance(param.default, (list, tuple)) else ()
            default = _literal(tuple(values)) or "()"
    elif not param.required:
        literal = _literal(param.default) if param.has_default else "None"
        if literal is None:
            notes.append(f"the default was {param.default!r}, which has no literal spelling")
            literal = "None"
        if literal == "None":
            annotation = f"{base} | None"
        default = literal
    short = param.short if param.short not in (None, "h") else None
    description = _description(param.help, f"The {flag_of(name).replace('-', ' ')}")
    return FieldSpec(
        name=name,
        annotation=annotation,
        description=description,
        positional=param.positional,
        default=default,
        short=None if param.positional else short,
        notes=tuple(notes),
    )


def _collides(name: str, switch: bool, reserved: frozenset[str]) -> bool:
    flag = flag_of(name)
    return flag in reserved or (switch and f"no-{flag}" in reserved)


def _fields(params: Sequence[_Param], where: str, walk: _Walk) -> tuple[FieldSpec, ...]:
    out = [f for p in params if (f := _field(p, where, walk)) is not None]
    # Required positionals before optional ones, as treaty reads them
    positionals = [f for f in out if f.positional]
    ordered = [f for f in positionals if f.default is None]
    ordered += [f for f in positionals if f.default is not None]
    return (*[f for f in out if not f.positional], *ordered)


def _command_word(name: str, parent: tuple[str, ...]) -> str:
    try:
        CommandPath(".".join((*parent, name)))
    except InvalidValue as exc:
        raise Exit.PRECONDITION(
            f"Command {' '.join((*parent, name))!r} has no treaty spelling: {exc}",
            context={"command": " ".join((*parent, name))},
            fix_required="rename the command in the old CLI to lowercase words joined by "
            "hyphens, then scaffold again",
        ) from None
    return name


# click and typer
#
# Read by shape, not by class: typer 0.20 and later run on a copy of click
# (typer._click), whose classes are not click's.


def _is_command(obj: object) -> bool:
    return isinstance(getattr(obj, "params", None), list) and hasattr(obj, "callback")


def _subcommands(obj: object) -> Mapping[str, Any] | None:
    commands = getattr(obj, "commands", None)
    return commands if isinstance(commands, Mapping) else None


def _click_tree(root: object, target: str, walk: _Walk) -> tuple[str | None, GroupSpec]:
    if not _is_command(root):
        raise Exit.PRECONDITION(
            f"Target {target} is {type(root).__name__}, not a click command or group",
            context={"target": target},
            fix_required="point at the click.Group (or click.Command) the CLI runs",
        )
    found: Any = getattr(root, "name", None)
    name = found if isinstance(found, str) and found else None
    if _subcommands(root) is None:
        word = _command_word(name or "run", ())
        return name, GroupSpec((), "", (), (_click_command(root, (word,), walk),))
    return name, _click_group(root, (), walk)


def _click_group(group: Any, path: tuple[str, ...], walk: _Walk) -> GroupSpec:
    where = " ".join(path) or "(root)"
    params = [p for p in (_click_param(p) for p in group.params) if p is not None]
    children: list[GroupSpec | CommandSpec] = []
    for name, sub in (_subcommands(group) or {}).items():
        if not path and name in BUILT_IN_COMMANDS:
            walk.skipped.append(Skipped(name, f"treaty's built-in {name} replaces it"))
            continue
        child = (*path, _command_word(name, path))
        if _subcommands(sub) is not None:
            children.append(_click_group(sub, child, walk))
        else:
            children.append(_click_command(sub, child, walk))
    return GroupSpec(
        path=path,
        description=_description(_help(group), f"The {' '.join(path)} commands"),
        fields=_fields(params, where, walk),
        children=tuple(children),
    )


def _help(command: Any) -> str | None:
    text = getattr(command, "help", None) or getattr(command, "short_help", None)
    return text if isinstance(text, str) else None


def _click_command(command: Any, path: tuple[str, ...], walk: _Walk) -> CommandSpec:
    params = [p for p in (_click_param(p) for p in command.params) if p is not None]
    return CommandSpec(
        path=path,
        description=_description(_help(command), f"Run {' '.join(path)}"),
        fields=_fields(params, " ".join(path), walk),
        origin=_origin(command.callback),
    )


def _origin(callback: object) -> str | None:
    if not callable(callback):
        return None
    fn = inspect.unwrap(callback)
    module, qualname = getattr(fn, "__module__", None), getattr(fn, "__qualname__", None)
    if not module or not qualname:
        return None
    return f"{module}:{qualname}"


def _click_param(param: Any) -> _Param | None:
    if not param.expose_value or param.name is None or param.name in _TYPER_COMPLETION:
        return None
    notes: list[str] = []
    base = _click_type(param.type, notes)
    envvar = getattr(param, "envvar", None)
    if envvar:
        names = envvar if isinstance(envvar, str) else ", ".join(envvar)
        notes.append(f"was also read from {names}: move it to App(settings=)")
    if getattr(param, "prompt", None):
        notes.append("the old CLI prompted for it: pass it as a flag, or ctx.prompt")
    many = bool(getattr(param, "multiple", False)) or param.nargs == -1
    if param.nargs > 1:
        notes.append(f"took {param.nargs} values")
        many = True
    unset = type(param.default).__name__ == "Sentinel"  # click 8.2's UNSET
    default = None if callable(param.default) or unset else param.default
    if callable(param.default):
        notes.append("the default was computed by a function")
    if param.param_type_name == "argument":
        return _Param(
            name=param.name,
            long=None,
            short=None,
            positional=True,
            base=base,
            many=many,
            required=bool(param.required),
            default=default,
            has_default=default is not None,
            help=getattr(param, "help", None),
            notes=tuple(notes),
        )
    long = next((o[2:] for o in param.opts if o.startswith("--")), None)
    short = next((o[1:] for o in param.opts if len(o) == 2 and o[0] == "-"), None)
    return _Param(
        name=param.name,
        long=long,
        short=short,
        positional=False,
        base=base,
        many=many,
        required=bool(param.required),
        default=default,
        has_default=default is not None,
        help=getattr(param, "help", None),
        switch=bool(getattr(param, "is_flag", False) and getattr(param, "is_bool_flag", False)),
        counter=bool(getattr(param, "count", False)),
        hidden=bool(getattr(param, "hide_input", False)),
        notes=tuple(notes),
    )


_CLICK_TYPES: Mapping[str, str] = {
    "text": "str",
    "str": "str",
    "int": "int",
    "int range": "int",
    "integer": "int",
    "integer range": "int",
    "float": "float",
    "float range": "float",
    "boolean": "bool",
    "path": "Path",
    "file": "Path",
    "directory": "Path",
}
_PYTHON_TYPES: Mapping[type, str] = {str: "str", int: "int", float: "float", bool: "bool"}


def _click_type(tp: Any, notes: list[str]) -> str:
    name = getattr(tp, "name", None)
    if name == "choice":
        literal = _choices_annotation(list(tp.choices))
        if literal is not None:
            return literal
    if name == "filename":
        notes.append("was a click.File: open the path in the handler")
        return "Path"
    if isinstance(name, str) and name in _CLICK_TYPES:
        return _CLICK_TYPES[name]
    func = getattr(tp, "func", None)  # type=<a Python callable>
    if isinstance(func, type) and func in _PYTHON_TYPES:
        return _PYTHON_TYPES[func]
    if isinstance(func, type) and issubclass(func, Path):
        return "Path"
    notes.append(f"was {name or type(tp).__name__}: annotate the type it parses to")
    return "str"


def _typer_root(obj: object, target: str) -> object:
    import typer

    if not isinstance(obj, typer.Typer):
        raise Exit.PRECONDITION(
            f"Target {target} is {type(obj).__name__}, not a typer.Typer",
            context={"target": target},
            fix_required="point at the typer.Typer() the CLI runs",
        )
    return typer.main.get_command(obj)


# argparse


def _argparse_tree(
    parser: argparse.ArgumentParser, walk: _Walk, path: tuple[str, ...], help_text: str | None
) -> GroupSpec | CommandSpec:
    actions = _actions(parser)
    params: list[_Param] = []
    subcommands: list[tuple[str, argparse.ArgumentParser, str, tuple[str, ...]]] = []
    for action in actions:
        found = _subparsers(action)
        if found is not None:
            subcommands.extend(found)
            continue
        param = _argparse_param(action)
        if param is not None:
            params.append(param)
    where = " ".join(path) or "(root)"
    description = parser.description or help_text
    if not subcommands:
        if not path:
            raise Exit.PRECONDITION(
                "The parser has no subcommands; scaffold-from needs add_subparsers()",
                context={"prog": parser.prog},
                fix_required="scaffold a single-command CLI by hand: one @app.command",
            )
        return CommandSpec(
            path=path,
            description=_description(description, f"Run {' '.join(path)}"),
            fields=_fields(params, where, walk),
            origin=_argparse_origin(parser),
        )
    children: list[GroupSpec | CommandSpec] = []
    for name, sub, sub_help, aliases in subcommands:
        if not path and name in BUILT_IN_COMMANDS:
            walk.skipped.append(Skipped(name, f"treaty's built-in {name} replaces it"))
            continue
        for alias in aliases:
            old, new = ".".join((*path, alias)), ".".join((*path, name))
            walk.skipped.append(
                Skipped(
                    " ".join((*path, alias)),
                    f"an alias of {' '.join((*path, name))}: app.redirect({_q(old)}, "
                    f"to={_q(new)}) answers it with the new name",
                )
            )
        children.append(_argparse_tree(sub, walk, (*path, _command_word(name, path)), sub_help))
    return GroupSpec(
        path=path,
        description=_description(description, f"The {' '.join(path)} commands"),
        fields=_fields(params, where, walk),
        children=tuple(children),
    )


def _actions(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    """``parser._actions``: argparse has no public list of a parser's arguments"""
    actions = getattr(parser, "_actions", None)
    if not isinstance(actions, list) or not all(isinstance(a, argparse.Action) for a in actions):
        raise Exit.PRECONDITION(
            "This Python's argparse keeps no ArgumentParser._actions list, which "
            "scaffold-from argparse reads",
            context={"prog": parser.prog},
            fix_required="run treaty scaffold-from under a Python whose argparse has "
            "ArgumentParser._actions, such as 3.14",
        )
    return actions


def _subparsers(
    action: argparse.Action,
) -> list[tuple[str, argparse.ArgumentParser, str, tuple[str, ...]]] | None:
    """The (name, parser, help, aliases) of each subcommand an ``add_subparsers()``
    action holds; None for any other action"""
    choices = action.choices
    if not isinstance(choices, dict) or not choices:
        return None
    if not all(isinstance(p, argparse.ArgumentParser) for p in choices.values()):
        return None
    helps: dict[str, str] = {}
    for pseudo in getattr(action, "_choices_actions", []):
        if isinstance(pseudo, argparse.Action) and isinstance(pseudo.help, str):
            helps[pseudo.dest] = pseudo.help
    names: dict[int, list[str]] = {}
    parsers: dict[int, argparse.ArgumentParser] = {}
    for name, parser in choices.items():
        names.setdefault(id(parser), []).append(name)  # the name, then its aliases
        parsers[id(parser)] = parser
    return [
        (spelled[0], parsers[key], helps.get(spelled[0], ""), tuple(spelled[1:]))
        for key, spelled in names.items()
    ]


_SKIPPED_ACTIONS = frozenset({"_HelpAction", "_VersionAction"})


def _argparse_param(action: argparse.Action) -> _Param | None:
    kind = type(action).__name__
    if kind in _SKIPPED_ACTIONS or action.dest == argparse.SUPPRESS:
        return None
    notes: list[str] = []
    positional = not action.option_strings
    long = next((o[2:] for o in action.option_strings if o.startswith("--")), None)
    short = next(
        (o[1:] for o in action.option_strings if len(o) == 2 and o[0] == "-" and o[1] != "-"),
        None,
    )
    default = None if action.default == argparse.SUPPRESS else action.default
    common = {
        "name": action.dest,
        "long": long,
        "short": short,
        "positional": positional,
        "help": action.help,
    }
    if action.nargs == 0:
        if kind == "_CountAction":
            return _Param(
                **common,  # type: ignore[arg-type]
                base="int",
                many=False,
                required=False,
                default=default or 0,
                has_default=True,
                counter=True,
            )
        if kind not in ("_StoreTrueAction", "_StoreFalseAction", "BooleanOptionalAction"):
            notes.append(f"was {kind} with const={action.const!r}: read it as a switch")
        return _Param(
            **common,  # type: ignore[arg-type]
            base="bool",
            many=False,
            required=False,
            default=bool(default),
            has_default=True,
            switch=True,
            notes=tuple(notes),
        )
    if kind == "BooleanOptionalAction":
        return _Param(
            **common,  # type: ignore[arg-type]
            base="bool",
            many=False,
            required=False,
            default=bool(default),
            has_default=True,
            switch=True,
        )
    base = _argparse_type(action, notes)
    many = kind in ("_AppendAction", "_ExtendAction") or action.nargs in ("*", "+")
    if isinstance(action.nargs, int) and action.nargs > 1:
        notes.append(f"took {action.nargs} values")
        many = True
    if kind not in ("_StoreAction", "_AppendAction", "_ExtendAction"):
        notes.append(f"was a {kind}: check how it reads its values")
    required = action.required if not positional else action.nargs not in ("?", "*")
    return _Param(
        **common,  # type: ignore[arg-type]
        base=base,
        many=many,
        required=required,
        default=default,
        has_default=default is not None,
        notes=tuple(notes),
    )


def _argparse_type(action: argparse.Action, notes: list[str]) -> str:
    if action.choices is not None:
        literal = _choices_annotation(list(action.choices))
        if literal is not None:
            return literal
    tp = action.type
    if tp is None or tp is str:
        return "str"
    if tp is int:
        return "int"
    if tp is float:
        return "float"
    if isinstance(tp, type) and issubclass(tp, Path):
        return "Path"
    if isinstance(tp, argparse.FileType):
        notes.append("was an argparse.FileType: open the path in the handler")
        return "Path"
    name = getattr(tp, "__qualname__", type(tp).__name__)
    notes.append(f"was type={name}: register its type with app.scalar, then annotate it")
    return "str"


def _argparse_origin(parser: argparse.ArgumentParser) -> str | None:
    func = parser.get_default("func")
    return _origin(func) if callable(func) else None


# Loading


def scaffold(
    kind: Kind, obj: object, target: str, name: str | None, *, env: Mapping[str, str]
) -> Scaffold:
    """The command tree of ``obj``, the object ``target`` names, imported under ``env``"""
    walk = _Walk(reserved=reserved_flags(), env=env)
    module = target.partition(":")[0].rpartition(".")[2]
    if kind == "argparse":
        parser = _parser(obj, target)
        root = _argparse_tree(parser, walk, (), None)
        # argparse names a parser built without prog= after the running program
        found: str | None = None if parser.prog == Path(sys.argv[0]).name else parser.prog
        description = parser.description or ""
    else:
        if kind == "typer":
            obj = _typer_root(obj, target)
        found, root = _click_tree(obj, target, walk)
        description = getattr(obj, "help", None) or ""
    assert isinstance(root, GroupSpec)
    app_name = name or found or module
    return Scaffold(
        name=app_name,
        description=_description(description, f"The {app_name} command line"),
        target=target,
        kind=kind,
        root=root,
        skipped=tuple(walk.skipped),
        renamed=tuple(walk.renamed),
    )


def _parser(obj: object, target: str) -> argparse.ArgumentParser:
    """The parser, or what a function with no required arguments returns"""
    if isinstance(obj, argparse.ArgumentParser):
        return obj
    if callable(obj):
        signature = inspect.signature(obj)
        required = [
            p
            for p in signature.parameters.values()
            if p.default is inspect.Parameter.empty
            and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
        ]
        if not required:
            try:
                built = obj()
            except SystemExit:
                raise Exit.PRECONDITION(
                    f"{target}() exited: it parses the command line rather than only "
                    "building the parser",
                    context={"target": target},
                    fix_required="move the parser's construction into a function that "
                    "returns it, such as build_parser(), and point at that",
                ) from None
            if isinstance(built, argparse.ArgumentParser):
                return built
            raise Exit.PRECONDITION(
                f"{target}() returned {type(built).__name__}, not an argparse.ArgumentParser",
                context={"target": target},
                fix_required="point at the parser, or at a function that only builds and "
                "returns it",
            )
    raise Exit.PRECONDITION(
        f"Target {target} is {type(obj).__name__}, not an argparse.ArgumentParser",
        context={"target": target},
        fix_required="point at the parser, or at a function that only builds and returns it",
    )


# Rendering


def render_module(scaffold: Scaffold) -> str:
    """The module's source: it registers, and passes ruff and mypy --strict"""
    return _Writer(scaffold).render()


class _Writer:
    def __init__(self, scaffold: Scaffold) -> None:
        self.scaffold = scaffold
        self.treaty: set[str] = {"App", "Ctx"}
        self.uses_path = False
        self.uses_literal = False
        self.uses_asdict = False
        self.classes: list[str] = []
        self.body: list[str] = []
        self.names: set[str] = set()

    def render(self) -> str:
        s = self.scaffold
        self._group(s.root, "app", None)
        doc = [
            f"Scaffolded by treaty scaffold-from from the {s.kind} CLI at {s.target}.",
            "",
            "Every command starts as danger_level='mutating' with exit_codes=(): declare "
            "each one's real values, which treaty audit asks for until you do. Each handler "
            "returns its arguments with effect 'noop': replace its body with a call to the "
            "function that does the work, split out of the command its docstring names.",
        ]
        if s.skipped:
            doc += ["", "Left out:", ""]
            doc += [f"- {item.path}: {item.reason}" for item in s.skipped]
        imports = ["from dataclasses import asdict, dataclass"]
        if not self.uses_asdict:
            imports = ["from dataclasses import dataclass"]
        if self.uses_path:
            imports.append("from pathlib import Path")
        if self.uses_literal:
            imports.append("from typing import Literal")
        app_args = [_q(s.name), 'version="0.1.0"', f"description={_q(s.description)}"]
        lines = [
            '"""',
            *_wrap_text(doc),
            '"""',
            "",
            *imports,
            "",
            f"from treaty import {', '.join(sorted(self.treaty))}",
            "",
            *_call("app = App(", app_args, ")", 0),
            *self.classes,
            *self.body,
            "",
            "",
            'if __name__ == "__main__":',
            "    app.main()",
        ]
        return "\n".join(lines) + "\n"

    def _name(self, wanted: str) -> str:
        name, n = wanted, 2
        while name in self.names or keyword.iskeyword(name):
            name, n = f"{wanted}_{n}", n + 1
        self.names.add(name)
        return name

    def _group(self, group: GroupSpec, var: str, base: str | None) -> None:
        options = base
        if group.fields:
            options = self._name(f"{camel(group.path)}Options")
            self._dataclass(options, base, group.fields)
        for child in group.children:
            if isinstance(child, GroupSpec):
                sub_var = self._name(f"{identifier('_'.join(child.path))}_group")
                args = [_q(child.path[-1]), f"description={_q(child.description)}"]
                self.body += ["", "", *_call(f"{sub_var} = {var}.group(", args, ")", 0)]
                self._group(child, sub_var, options)
            else:
                self._command(child, var, options)

    def _dataclass(self, name: str, base: str | None, fields: Sequence[FieldSpec]) -> None:
        self.classes += [
            "",
            "",
            "@dataclass(frozen=True, slots=True, kw_only=True)",
            f"class {name}{f'({base})' if base else ''}:",
        ]
        if not fields:
            self.classes.append("    pass")
        for spec in fields:
            for note in spec.notes:
                self.classes += _words(note, 4, WIDTH, prefix="# ")
            maker = "Arg" if spec.positional else "Flag"
            self.treaty.add(maker)
            args: list[str] = []
            if spec.default is not None:
                self.uses_path |= "Path(" in spec.default
                args.append(f"default={spec.default}")
            args.append(f"description={_q(spec.description)}")
            if spec.short is not None:
                args.append(f"short={_q(spec.short)}")
            self.uses_path |= "Path" in spec.annotation
            self.uses_literal |= "Literal[" in spec.annotation
            self.classes += _call(f"{spec.name}: {spec.annotation} = {maker}(", args, ")", 4)

    def _command(self, command: CommandSpec, var: str, base: str | None) -> None:
        stem = identifier("_".join(command.path))
        if command.fields or base is not None:
            args_class = self._name(f"{camel(command.path)}Args")
            self._dataclass(args_class, base, command.fields)
            result = '{"effect": "noop", "args": asdict(args)}'
            self.uses_asdict = True
        else:
            args_class = "NoArgs"
            self.treaty.add("NoArgs")
            result = '{"effect": "noop", "args": {}}'
        func = self._name(f"{stem}_command")
        decorator = [
            _q(command.path[-1]),
            f"description={_q(command.description)}",
            'danger_level="mutating"',
            "exit_codes=()",
        ]
        self.body += [
            "",
            "",
            *_call(f"@{var}.command(", decorator, ")", 0, always_split=True),
            f"def {func}(args: {args_class}, ctx: Ctx) -> dict[str, object]:",
            *_wrap_text([f"Was {command.origin or 'an unknown function'}"], 4, doc=True),
            f"    return {result}",
        ]


def _call(
    head: str, args: Sequence[str], tail: str, indent: int, *, always_split: bool = False
) -> list[str]:
    """``head`` + the arguments + ``tail`` on one line when it fits, else one argument a
    line, a long string argument split into implicitly concatenated pieces"""
    pad = " " * indent
    one = f"{pad}{head}{', '.join(args)}{tail}"
    if len(one) <= WIDTH and not always_split:
        return [one]
    lines = [f"{pad}{head}"]
    for arg in args:
        lines += _argument(arg, indent + 4)
    lines.append(f"{pad}{tail}")
    return lines


def _argument(arg: str, indent: int) -> list[str]:
    pad = " " * indent
    line = f"{pad}{arg},"
    if len(line) <= WIDTH:
        return [line]
    name, eq, value = arg.partition("=")
    if not eq or not value.startswith('"') or not value.endswith('"'):
        return [line]  # nothing to split; the author sees the long line
    text = json.loads(value)
    pieces = _pieces(text, WIDTH - indent - 4 - 2)
    return [f"{pad}{name}=(", *(f"{pad}    {_q(p)}" for p in pieces), f"{pad}),"]


def _pieces(text: str, room: int) -> list[str]:
    """``text`` cut after spaces into pieces whose literals fit ``room``"""
    pieces: list[str] = []
    current = ""
    for word in re.split(r"(?<= )", text):
        if current and len(_q(current + word)) - 2 > room:
            pieces.append(current)
            current = word
        else:
            current += word
    if current:
        pieces.append(current)
    return pieces


def _q(text: str) -> str:
    """A double-quoted Python string literal: JSON's escapes are Python's too"""
    return json.dumps(text, ensure_ascii=False)


def _wrap_text(paragraphs: Sequence[str], indent: int = 0, *, doc: bool = False) -> list[str]:
    """Lines of prose wrapped at the width; ``doc`` makes them one docstring"""
    pad = " " * indent
    if doc:
        text = " ".join(paragraphs).replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
        line = f'{pad}"""{text}"""'
        if len(line) <= WIDTH:
            return [line]
        return [f'{pad}"""', *_words(text, indent, WIDTH), f'{pad}"""']
    lines: list[str] = []
    for paragraph in paragraphs:
        text = paragraph.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
        lines += _words(text, indent, WIDTH) if text else [""]
    return lines


def _words(text: str, indent: int, width: int, *, prefix: str = "") -> list[str]:
    pad = " " * indent + prefix
    # A list item's later lines line up with its text
    hanging = pad + "  " if text.startswith("- ") else pad
    lines: list[str] = []
    current = ""
    for word in text.split():
        if current and len(lines and hanging or pad) + len(current) + 1 + len(word) > width:
            lines.append((hanging if lines else pad) + current)
            current = word
        else:
            current = f"{current} {word}" if current else word
    if current:
        lines.append((hanging if lines else pad) + current)
    return lines


def check_module(source: str, target: str) -> None:
    """Run the generated module, which imports only treaty and the standard library, so
    a name the walk missed fails here rather than in the author's project"""
    # dataclasses and type hints look the module up by name while it runs
    name = f"_treaty_scaffold_check_{id(source)}"
    module = types.ModuleType(name)
    sys.modules[name] = module
    try:
        exec(compile(source, "<scaffold>", "exec"), module.__dict__)
    except RegistrationError as exc:
        raise Exit.PRECONDITION(
            f"The module scaffolded from {target} does not register: {exc}",
            code="SCAFFOLD_INVALID",
            context={"target": target, "message": str(exc)},
            fix_required="rename the option or command the message names in the old CLI, "
            "and report the message to treaty",
        ) from None
    finally:
        del sys.modules[name]
