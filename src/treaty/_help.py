"""Manifest-driven help rendering for human mode."""

from __future__ import annotations

from collections.abc import Mapping

from ._command import Command, DangerLevel
from ._values import CommandPath


def render_root(
    name: str,
    description: str,
    commands: Mapping[CommandPath, Command],
    groups: Mapping[CommandPath, str],
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
    group_rows = [
        (p, d)
        for p, d in sorted(groups.items(), key=lambda kv: kv[0].value)
        if p.parts[:depth] == prefix and len(p.parts) == depth + 1
    ]
    width = max(
        [len(p.parts[-1]) for p, _ in listed] + [len(p.parts[-1]) for p, _ in group_rows] + [8]
    )
    if group_rows:
        lines.append("Command groups")
        for p, d in group_rows:
            lines.append(f"  {p.parts[-1]:<{width}}  {d}")
        lines.append("")
    if listed:
        lines.append("Commands")
        for p, c in listed:
            lines.append(f"  {p.parts[-1]:<{width}}  {c.description}")
        lines.append("")
    lines.append("Global flags")
    lines.append(f"  {'--format':<{width}}  Output mode: human or json (default: json when piped)")
    lines.append(f"  {'--help':<{width}}  Show help for a command")
    lines.append(f"  {'--max-output':<{width}}  Byte cap on JSON output (default: 1 MiB)")
    lines.append(f"  {'--schema':<{width}}  Print parameters and output schema as JSON")
    if not prefix:
        lines.append(f"  {'--version':<{width}}  Print the tool name and version")
    return "\n".join(lines) + "\n"


def _framework_rows(command: Command) -> list[tuple[str, str]]:
    """The flags treaty adds to this command, so a person can find how to apply it"""
    rows: list[tuple[str, str]] = []
    if command.danger_level is DangerLevel.DESTRUCTIVE:
        rows.append(("--confirm-destructive", "Apply; without it the command only previews"))
    if command.danger_level is not DangerLevel.SAFE:
        rows.append(("--idempotency-key KEY", "Repeat calls with KEY replay the first result"))
    if command.has_network_io:
        rows.append(("--timeout SECONDS", "Abort with TIMEOUT after SECONDS; 0 disables it"))
    if command.supports_raw_payload:
        rows.append(("--raw-payload JSON", "All field values as one JSON object"))
    if command.streaming:
        rows.append(("--no-stream", "One envelope with every event instead of JSONL"))
    return rows


def render_command(name: str, command: Command) -> str:
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
    if rows:
        lines.append("Flags")
        width = max(len(label) for label, _ in rows)
        for label, text in rows:
            lines.append(f"  {label:<{width}}  {text}")
        lines.append("")
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
