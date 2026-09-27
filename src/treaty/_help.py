"""Manifest-driven help rendering for plain mode."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ._command import Command, DangerLevel
from ._mode import Format
from ._values import CommandPath


def render_root(
    name: str,
    description: str,
    commands: Mapping[CommandPath, Command],
    groups: Mapping[CommandPath, str],
    formats: Sequence[Format],
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
    global_labels = ["--format", "--help", "--max-output", "--schema", "--version"]
    width = max(
        [len(p.parts[-1]) for p, _ in listed]
        + [len(label) for label, _ in group_rows]
        + [len(label) for label in global_labels]
    )
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
    lines.append("Global flags")
    modes = ", ".join(m.value for m in formats)
    lines.append(f"  {'--format':<{width}}  Output mode: {modes} (default: json when piped)")
    lines.append(f"  {'--help':<{width}}  Show help for a command")
    lines.append(f"  {'--max-output':<{width}}  Byte cap on JSON output (default: 1 MiB)")
    lines.append(f"  {'--schema':<{width}}  Print parameters and output schema as JSON")
    if not prefix:
        lines.append(f"  {'--version':<{width}}  Print the tool name and version")
    return "\n".join(lines) + "\n"


def _under(commands: Mapping[CommandPath, Command], parts: tuple[str, ...]) -> list[CommandPath]:
    return [p for p in commands if p.parts[: len(parts)] == parts]


def _framework_rows(command: Command) -> list[tuple[str, str]]:
    """The flags treaty adds to this command, so a person can find how to apply it"""
    rows: list[tuple[str, str]] = []
    if command.safe_default:
        rows.append(("--live", "Apply; without it the command runs as a dry run"))
        rows.append(("--confirm-destructive", "Required with --live"))
    elif command.danger_level is DangerLevel.DESTRUCTIVE:
        rows.append(("--confirm-destructive", "Apply; without it the command only previews"))
    if command.danger_level is not DangerLevel.SAFE:
        rows.append(("--idempotency-key KEY", "Repeat calls with KEY replay the first result"))
    if command.accepts_timeout:
        rows.append(("--timeout SECONDS", "Abort with TIMEOUT after SECONDS; 0 disables it"))
    if command.supports_raw_payload:
        rows.append(("--raw-payload JSON", "All field values as one JSON object"))
    if command.streaming:
        rows.append(("--no-stream", "One envelope with every event instead of JSONL"))
    if command.paginated:
        default = command.default_limit.count or 0
        rows.append(("--limit N", f"Most items to return (default: {default}); 0 returns all"))
        rows.append(("--cursor TOKEN", "Next page: meta.pagination.next_cursor of the last one"))
    if command.output_file:
        rows.append(("--output PATH", "Write the result to PATH; stdout gets the envelope"))
    if command.stdin_input:
        rows.append(("--input-file PATH", "Read the input from PATH instead of stdin"))
    if command.heartbeat:
        rows.append(("--heartbeat-ms MS", "Heartbeat lines while running (default: 10000)"))
    if command.interactive:
        rows.append(("--yes", "Answer yes to every confirmation"))
        rows.append(("--non-interactive", "Never prompt; a needed answer exits 4"))
    if command.config_write_scope is not None:
        rows.append(("--global", "Write the user config file instead of the project's"))
    if command.auth is not None:
        rows.append(("--headless", "Never open a browser; log in with a token variable"))
        rows.append(("--token-env-var NAME", "Read the token from $NAME"))
        rows.extend(
            (f"${v}", "Token read when no --token-env-var is given") for v in command.token_env_vars
        )
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
