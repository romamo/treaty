"""Manifest-driven help rendering for plain mode."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ._command import Command
from ._framework import framework_flags
from ._values import CommandPath

Row = tuple[str, str]


def global_rows(entries: Mapping[str, object]) -> list[Row]:
    """The manifest's root ``flags`` as help rows: every command takes them"""
    rows: list[Row] = []
    for flag, entry in entries.items():
        assert isinstance(entry, dict)
        label = f"--{flag}" + (f", -{entry['short']}" if "short" in entry else "")
        if entry["type"] != "boolean":
            label += " " + ("|".join(entry["enum_values"]) if "enum_values" in entry else "VALUE")
        rows.append((label, str(entry["description"])))
    return rows


def _section(title: str, rows: Sequence[Row]) -> list[str]:
    if not rows:
        return []
    width = max(len(label) for label, _ in rows)
    return [title, *(f"  {label:<{width}}  {text}" for label, text in rows), ""]


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
    return "\n".join(lines).rstrip("\n") + "\n"


def _under(commands: Mapping[CommandPath, Command], parts: tuple[str, ...]) -> list[CommandPath]:
    return [p for p in commands if p.parts[: len(parts)] == parts]


def _framework_rows(command: Command) -> list[tuple[str, str]]:
    """The flags treaty adds to this command, so a person can find how to apply it"""
    rows = [f.help_row(command) for f in framework_flags(command)]
    rows.extend(
        (f"${v}", "Token read when no --token-env-var is given") for v in command.token_env_vars
    )
    return rows


def render_command(name: str, command: Command, globals_: Sequence[Row]) -> str:
    positionals = [f for f in command.fields if f.positional]
    flags = [f for f in command.fields if not f.positional]
    usage = [name, *command.path.parts]
    usage.extend(f"<{f.flag}>" if f.required else f"[{f.flag}]" for f in positionals)
    if flags or _framework_rows(command):
        usage.append("[flags]")
    lines = [f"{' '.join(usage)}", "", command.description, ""]
    if positionals:
        lines.append("Arguments")
        width = max(len(f.flag) for f in positionals)
        for f in positionals:
            lines.append(f"  {f.flag:<{width}}  {f.spec.description}")
        lines.append("")
    rows: list[tuple[str, str]] = []
    for f in flags:
        if f.secret:
            var = command.secret_env_vars[f.name]
            need = " (required)" if f.required else ""
            rows.append((f"--{f.env_flag} VAR", f"{f.spec.description}: read from $VAR{need}"))
            rows.append((f"--{f.file_flag} PATH", f"{f.spec.description}: read from PATH"))
            rows.append((f"${var}", f"{f.spec.description}: default when neither is given"))
            continue
        label = f"--{f.flag}" + (f", -{f.spec.short}" if f.spec.short else "")
        rows.append((label, f.spec.description + (" (required)" if f.required else "")))
    rows.extend(_framework_rows(command))
    lines += _section("Flags", rows)
    lines += _section("Global flags", globals_)
    lines.append(f"Danger level: {command.danger_level.value}")
    if command.streaming:
        lines.append("Streams one JSONL envelope per event; --no-stream returns a single envelope")
    if command.examples:
        lines.append("")
        lines.append("Examples")
        for e in command.examples:
            lines.append(f"  # {e.description}")
            lines.append(f"  {e.command}")
    return "\n".join(lines) + "\n"
