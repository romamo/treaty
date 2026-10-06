"""Manifest-driven help rendering for plain mode."""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence

from ._command import Command, DangerLevel, OptionPlacement
from ._envelope import visible
from ._envnames import declared_text
from ._flags import FieldInfo, object_shape
from ._framework import framework_flags
from ._mode import FormatName
from ._types import FlagType
from ._values import CommandPath

Row = tuple[str, str]


def global_rows(entries: Mapping[str, object]) -> list[Row]:
    """The manifest's root ``flags`` as help rows: every command takes them. The
    ``--format`` row names what each format an app registered writes (#179)"""
    rows: list[Row] = []
    for flag, entry in entries.items():
        assert isinstance(entry, dict)
        label = f"--{flag}" + (f", -{entry['short']}" if "short" in entry else "")
        if entry["type"] != "boolean":
            label += " " + ("|".join(entry["enum_values"]) if "enum_values" in entry else "VALUE")
        written = entry.get("media_types", {})
        text = str(entry["description"]) + "".join(
            f"; {name} writes {kind}"
            for name, kind in written.items()
            if FormatName(name).builtin is None
        )
        rows.append((label, text))
    return rows


def _section(title: str, rows: Sequence[Row]) -> list[str]:
    if not rows:
        return []
    # Aligned on the labels as shown: an enum value's control takes its escape's width
    shown = [(visible(label), text) for label, text in rows]
    width = max(len(label) for label, _ in shown)
    return [title, *(f"  {label:<{width}}  {text}" for label, text in shown), ""]


def render_root(
    name: str,
    description: str,
    commands: Mapping[CommandPath, Command],
    groups: Mapping[CommandPath, str],
    globals_: Sequence[Row],
    environment: Sequence[Row],
    prefix: tuple[str, ...] = (),
) -> str:
    lines = [f"{name}: {description}" if description else name, ""]
    usage = " ".join([name, *prefix, "<command>", "[flags]"])
    lines.append(f"Usage: {usage}")
    lines.append("")
    depth = len(prefix)
    listed = [
        (p, c)
        for p, c in sorted(commands.items(), key=lambda kv: kv[0].value)
        if p.parts[:depth] == prefix and len(p.parts) == depth + 1
    ]
    # A group is any path segment with commands under it, declared with app.group() or
    # implied by a dotted registration such as db.migrate.up
    declared = {p.parts: d for p, d in groups.items()}
    implied = sorted(
        {
            p.parts[: depth + 1]
            for p in commands
            if p.parts[:depth] == prefix and len(p.parts) > depth + 1
        }
        | {
            parts
            for parts in declared
            # A declared group with no commands yet cannot be routed to, so it is not listed
            if parts[:depth] == prefix and len(parts) == depth + 1 and _under(commands, parts)
        }
    )
    group_rows = [
        (parts[-1], declared.get(parts, f"{len(_under(commands, parts))} commands"))
        for parts in implied
    ]
    labels = [p.parts[-1] for p, _ in listed] + [label for label, _ in group_rows]
    width = max(map(len, labels), default=0)
    if group_rows:
        lines.append("Command groups")
        for label, d in group_rows:
            lines.append(f"  {label:<{width}}  {d}")
        lines.append("")
    if listed:
        lines.append("Commands")
        for p, c in listed:
            lines.append(f"  {p.parts[-1]:<{width}}  {c.description}")
        lines.append("")
    rows = list(globals_)
    if not prefix:
        rows.append(("--version", "Print the tool name and version"))
    lines += _section("Global flags", rows)
    # REQ-F-073, REQ-O-042: every variable read, by its exact prefixed name
    lines += _section("Environment", environment)
    return _shown("\n".join(lines).rstrip("\n") + "\n")


def _shown(text: str) -> str:
    """Help as a terminal gets it: the app's text (descriptions, examples, enum values)
    shows each control character and bidi override as its escape, as plain output does,
    and keeps its newlines and tabs (#203)"""
    return visible(text)


def _under(commands: Mapping[CommandPath, Command], parts: tuple[str, ...]) -> list[CommandPath]:
    return [p for p in commands if p.parts[: len(parts)] == parts]


def _framework_rows(command: Command) -> list[tuple[str, str]]:
    """The flags treaty adds to this command, so a person can find how to apply it"""
    rows = [f.help_row(command) for f in framework_flags(command)]
    rows.extend(
        (f"${v}", "Token read when no --token-env-var is given") for v in command.token_env_vars
    )
    return rows


def env_readers(
    commands: Mapping[CommandPath, Command], builtins: frozenset[CommandPath]
) -> dict[tuple[str, str], str]:
    """Each variable a command's flag reads, a secret's or a ``Flag(env=)`` flag's, keyed
    with the flag, and the commands whose flag of that name reads it as ``--help`` names
    them: "every command" when all the app's own commands do (treaty's ``builtins``
    aside), else their paths in order. Commands sharing a flag share its variables, and
    a command reading the variable through a flag of another name is not one (#295)"""
    readers: dict[tuple[str, str], list[str]] = {}
    for path, command in sorted(commands.items(), key=lambda kv: kv[0].value):
        for f in command.fields:
            own = command.own_env_var(f.name)
            for var in (*(() if own is None else (own,)), *(n.name for n in f.spec.env)):
                readers.setdefault((var, f.flag), []).append(path.value)
    mine = {p.value for p in commands if p not in builtins}
    return {
        key: "every command" if len(mine) > 1 and set(paths) == mine else ", ".join(paths)
        for key, paths in readers.items()
    }


def declared_env_rows(
    command: Command, readers: Mapping[tuple[str, str], str]
) -> list[tuple[str, FieldInfo, str]]:
    """Each variable of the command's flags that declare ``Flag(env=)``, the field it sets,
    and what it is: a plain flag's ``<APP>_<NAME>``, then the declared names, each the
    default of the flag of the ``readers`` of it (``env_readers``)"""
    rows: list[tuple[str, FieldInfo, str]] = []
    for f in command.fields:
        own = command.own_env_var(f.name)
        if not f.secret and own is not None:
            rows.append((own, f, f"Default of --{f.flag} of {readers[own, f.flag]}"))
        default = own or f"--{f.flag}"
        rows += [
            (
                n.name,
                f,
                declared_text(
                    n, f"Default of --{f.flag} of {readers[n.name, f.flag]}", own, default
                ),
            )
            for n in f.spec.env
        ]
    return rows


def _declared_rows(f: FieldInfo, when: str, default: str) -> list[tuple[str, str]]:
    """One row per ``Flag(env=)`` name, read in order ``when``; a deprecated one names
    its replacement, ``default`` unless declared"""
    what = f"{f.spec.description}: read {when}"
    return [(f"${n.name}", declared_text(n, what, None, default)) for n in f.spec.env]


def field_rows(command: Command, f: FieldInfo) -> list[Row]:
    """A field's rows in ``--help``: a positional's under Arguments; a flag's under
    Flags with the variables it reads; a secret's as its two sources and its variable"""
    if f.positional:
        return [(f.flag, f.spec.description)]
    if f.secret:
        var = command.secret_env_vars[f.name]
        need = " (required)" if f.required else ""
        return [
            (f"--{f.env_flag} VAR", f"{f.spec.description}: read from $VAR{need}"),
            (f"--{f.file_flag} PATH", f"{f.spec.description}: read from PATH"),
            (f"${var}", f"{f.spec.description}: default when neither is given"),
            *_declared_rows(f, f"when ${var} is not set", var),
        ]
    label = f"--{f.flag}" + (f", -{f.spec.short}" if f.spec.short else "")
    text = f.spec.description + (" (required)" if f.required else "")
    if (shape := f.object_type) is not None:
        label += " JSON"
        repeated = f.flag_type is FlagType.ARRAY
        each = "repeat it, one JSON object each" if repeated else "a JSON object"
        text += f"; {each}: {object_shape(shape)}"
    rows = [(label, text)]
    own = command.own_env_var(f.name)
    if own is None:
        return rows
    rows.append((f"${own}", f"{f.spec.description}: read when --{f.flag} is not given"))
    rows += _declared_rows(f, f"when --{f.flag} is not given and ${own} is not set", own)
    return rows


def missing_lines(
    name: str, command: Command, missing: Sequence[FieldInfo]
) -> tuple[list[str], list[str]]:
    """What a person reads about a missing required argument (#358): each missing
    field's ``--help`` rows, then a usage line with only the command's required
    arguments and where the rest are. Names and descriptions only, never a value"""
    shown = [(visible(label), text) for f in missing for label, text in field_rows(command, f)]
    width = max(len(label) for label, _ in shown)
    rows = [_shown(f"  {label:<{width}}  {text}") for label, text in shown]
    path = [name, *command.path.parts]
    positionals = [f.shown for f in command.fields if f.required and f.positional]
    options = [
        f"--{f.env_flag} <var>" if f.secret else f"--{f.flag} <{f.flag}>"
        for f in command.fields
        if f.required and not f.positional
    ]
    options.append("[options]")
    # REQ-C-027: a strict command reads options only before its first positional
    strict = command.option_placement is OptionPlacement.STRICT
    usage = [*path, *(options + positionals if strict else positionals + options)]
    tail = [
        f"usage: {' '.join(usage)}",
        f"Run '{' '.join([*path, '--help'])}' for all options.",
    ]
    return rows, [_shown(line) for line in tail]


def render_command(name: str, command: Command, globals_: Sequence[Row]) -> str:
    positionals = [f for f in command.fields if f.positional]
    flags = [f for f in command.fields if not f.positional]
    usage = [name, *command.path.parts]
    usage.extend(f.shown if f.required else f"[{f.flag}]" for f in positionals)
    if command.passthrough:
        # #35: treaty's flags go before the path; every token after it is the tool's
        usage[1:1] = ["[flags]"]
        usage.append("[tool arguments...]")
    elif flags or _framework_rows(command):
        # REQ-C-027: a strict command reads options only before its first positional
        strict = command.option_placement is OptionPlacement.STRICT
        usage.insert(1 + len(command.path.parts) if strict else len(usage), "[flags]")
    lines = [f"{' '.join(usage)}", "", command.description, ""]
    if command.passthrough:
        lines.append(
            "Every argument after the command path goes to the delegated tool verbatim, "
            "--help and -- included; its output is on stdout, and the final envelope is "
            "the last line on stderr"
        )
        if command.help_command is not None:
            shown = shlex.join([name, *command.path.parts, *command.help_command])
            lines.append(f"A lone --help or -h after the path runs {shown}")
        lines.append("")
    if positionals:
        lines.append("Arguments")
        width = max(len(f.flag) for f in positionals)
        for f in positionals:
            lines.append(f"  {f.flag:<{width}}  {f.spec.description}")
        lines.append("")
    rows = [row for f in flags for row in field_rows(command, f)]
    rows.extend(_framework_rows(command))
    lines += _section("Flags", rows)
    if command.requires:  # REQ-C-026
        lines += ["Rules", *(f"  {r.describe()}" for r in command.requires), ""]
    lines += _section("Global flags", globals_)
    lines.append(f"Danger level: {command.danger_level.value}")
    if command.streaming:
        lines.append("Streams one JSONL envelope per event; --no-stream returns a single envelope")
        if command.danger_level is not DangerLevel.SAFE:
            lines.append(
                "Each event reports its own effect; the last line counts them in meta.effects"
            )
    if command.examples:
        lines.append("")
        lines.append("Examples")
        for e in command.examples:
            lines.append(f"  # {e.description}")
            lines.append(f"  {e.command}")
    return _shown("\n".join(lines) + "\n")
