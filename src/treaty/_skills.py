"""Agent skill files rendered from the manifest (REQ-O-034).

``CONTEXT.md`` is the overview: what the tool does, its commands, the shared exit codes,
and how an agent calls it. Each ``SKILL-<command>.md`` has a YAML frontmatter block whose
values are all JSON (``args`` a JSON flow mapping), which is valid YAML and needs no YAML
library, then usage examples, arguments, guardrails, and patterns. Nothing here reads
anything the manifest and ``--schema`` do not already say.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING

from ._command import Command, DangerLevel, OptionPlacement
from ._manifest import payload_schema
from ._values import CommandPath

if TYPE_CHECKING:
    from ._app import App

CONTEXT_FILE = "CONTEXT.md"


def skill_file(path: CommandPath) -> str:
    return f"SKILL-{path.value.replace('.', '-')}.md"


def skill_name(app_name: str, path: CommandPath) -> str:
    """Lowercase words and hyphens, as skill loaders expect"""
    return f"{app_name}-{path.value.replace('.', '-')}"


def _invocation(app_name: str, path: CommandPath) -> str:
    return " ".join((app_name, *path.parts))


def _placeholder(name: str, spec: Mapping[str, object]) -> str:
    enum = spec.get("enum")
    if isinstance(enum, list) and enum:
        return str(enum[0])
    return f"<{name}>"


def examples(
    app_name: str, path: CommandPath, entry: Mapping[str, object], *, passthrough: bool = False
) -> list[str]:
    """The declared examples, a minimal call with the required arguments, the ``--schema``
    call, and ``--help``: always at least three"""
    base = _invocation(app_name, path)
    found = [str(e["command"]) for e in entry.get("examples", ())]  # type: ignore[attr-defined]
    if passthrough:
        # #35: after the path every token is the tool's, --help too; treaty's go before
        schema = " ".join((app_name, "--schema", *path.parts))
        return list(dict.fromkeys((*found, f"{base} <tool arguments>", schema, f"{base} --help")))
    positionals = entry.get("positionals", [])  # absent for a command without any
    assert isinstance(positionals, list)
    words = [_placeholder(p["name"], p) for p in positionals if p.get("required")]
    named = {p["name"] for p in positionals}
    flags = entry.get("flags", {})
    assert isinstance(flags, dict)
    options: list[str] = []
    for flag, spec in flags.items():
        if spec.get("required") and flag not in named:
            options.append(f"--{flag}")
            if spec.get("type") != "boolean":
                options.append(_placeholder(flag, spec))
    # REQ-C-027: a strict command reads options only before its first positional
    strict = entry.get("option_placement") == "strict"
    minimal = " ".join((base, *(options + words if strict else words + options)))
    return list(dict.fromkeys((*found, minimal, f"{base} --schema", f"{base} --help")))


def _frontmatter(fields: Mapping[str, object]) -> str:
    lines = [f"{key}: {json.dumps(value, sort_keys=True)}" for key, value in fields.items()]
    return "---\n" + "\n".join(lines) + "\n---\n"


def _guardrails(app_name: str, command: Command, entry: Mapping[str, object]) -> list[str]:
    rails = [f"Danger level: {command.danger_level.value}"]
    if command.danger_level is DangerLevel.DESTRUCTIVE:
        switch = command.dry_run_field
        flag = "--dry-run" if switch is None else f"--{switch.flag}"
        rails.append(
            f"Destructive: run with {flag} first and read data.would_affect; apply only "
            "with --confirm-destructive, which nothing else implies"
        )
    elif command.danger_level is DangerLevel.MUTATING:
        if (confirming := command.confirm_field) is not None:
            rails.append(
                f"Previews unless --{confirming.flag}: read the would_* effect first, then "
                f"apply with --{confirming.flag}"
            )
        if command.streaming:
            rails.append(
                "Mutating stream: each event's effect says what it did and the _summary "
                "line's effects counts them; it takes no --idempotency-key, so a rerun is a new "
                "run, and a failure after a live effect is not retryable"
            )
        else:
            rails.append("Mutating: pass --idempotency-key so a retry cannot apply it twice")
    if command.endless:
        rails.append(
            "Endless stream: it runs until interrupted, so read its lines as they come and "
            "send SIGINT to stop it; it ends on a CANCELLED line, not a _summary line"
        )
    if command.required_scopes:
        scopes = ", ".join(str(s) for s in command.required_scopes)
        rails.append(f"Needs the credential scopes {scopes}")
    codes = entry.get("exit_codes", {})
    assert isinstance(codes, dict)
    for code, spec in sorted(codes.items(), key=lambda kv: int(kv[0])):
        retry = "retryable" if spec.get("retryable") else "not retryable"
        line = f"Exit {code} {spec['name']} ({retry}): {spec['description']}"
        if errors := spec.get("error_codes"):
            line += f"; error.code is one of {', '.join(errors)}"  # ExitCodeEntry 1.1 (#362)
        rails.append(line)
    rails.append(f"Shared exit codes, 2 among them for bad arguments: {app_name} manifest")
    return rails


def render_skill(app: App, command: Command, entry: Mapping[str, object]) -> str:
    path = command.path
    front = _frontmatter(
        {
            "name": skill_name(app.name, path),
            "description": command.description or _invocation(app.name, path),
            "version": app.version,
            "command": _invocation(app.name, path),
            "args": payload_schema(command, stream_key=False),
        }
    )
    usage = "\n".join(examples(app.name, path, entry, passthrough=command.passthrough))
    flags = entry.get("flags", {})
    assert isinstance(flags, dict)
    rows = [
        f"| `--{name}` | {spec.get('type', 'any')} | {'yes' if spec.get('required') else 'no'} "
        f"| {str(spec.get('description', '')).replace('|', '/')} |"
        for name, spec in flags.items()
    ]
    table = "\n".join(["| Flag | Type | Required | Description |", "|---|---|---|---|", *rows])
    rails = "\n".join(f"- {r}" for r in _guardrails(app.name, command, entry))
    invocation = _invocation(app.name, path)
    if command.passthrough:
        # #35: the tool owns stdout and every token after the path
        before = " ".join((app.name, "--validate-only", *path.parts))
        patterns = (
            "- The tool's output is on stdout; the envelope is the last line on stderr: read "
            "its `ok`, then `meta.exit_code`, the tool's own exit code\n"
            f"- Treaty's flags go before the path: `{before} ...`; `--output PATH` writes the "
            "envelope there too"
        )
        avoid = (
            "- Putting treaty's flags after the path, where the tool gets them\n"
            f"- Guessing the tool's arguments instead of reading `{invocation} --help`"
        )
    else:
        # REQ-C-027: a strict command reads options only before its first positional
        strict = command.option_placement is OptionPlacement.STRICT
        check = (
            f"{invocation} --validate-only ..." if strict else f"{invocation} ... --validate-only"
        )
        patterns = (
            "- Read `ok` first, then `data`; on failure act on `error.code` and "
            "`error.fix_required`\n"
            "- Pass `--format json` when stdout may be a terminal; off a terminal it is the "
            "default\n"
            f"- Check arguments without running: `{check}`"
        )
        avoid = (
            "- Parsing the text of `--format plain` instead of the JSON envelope\n"
            "- Retrying an exit code whose `retryable` is false\n"
            f"- Guessing flags instead of reading `{invocation} --schema`"
        )
    return f"""{front}
# {_invocation(app.name, path)}

{command.description}

## Usage

```bash
{usage}
```

## Arguments

{table}

## Guardrails

{rails}

## Patterns

{patterns}

## Anti-patterns

{avoid}
"""


def render_context(app: App, manifest: Mapping[str, object], paths: list[CommandPath]) -> str:
    commands = manifest["commands"]
    assert isinstance(commands, dict)
    listed = "\n".join(
        f"- `{_invocation(app.name, p)}` ({commands[p.value]['danger_level']}): "
        f"{commands[p.value]['description']}, see {skill_file(p)}"
        for p in paths
    )
    shared = manifest["exit_codes"]
    assert isinstance(shared, dict)
    codes = "\n".join(
        f"| {code} | {spec['name']} | {'yes' if spec.get('retryable') else 'no'} "
        f"| {spec['description']} |"
        for code, spec in sorted(shared.items(), key=lambda kv: int(kv[0]))
    )
    return f"""<!-- cli-version: {app.version} -->
# {app.name} {app.version}

{app.description or app.name}

## Commands

{listed}

Built-ins every command set has: `{app.name} manifest` (the whole contract in one call),
`{app.name} doctor`, `{app.name} status`, and `{app.name} <command> --schema`.

## Exit codes

| Code | Name | Retryable | Meaning |
|---|---|---|---|
{codes}

## Calling it as an agent

- Every response is one JSON envelope: `ok`, `data`, `error`, `warnings`, `meta`
- Nothing prompts: a destructive command without `--confirm-destructive` previews and exits 2
- Exit 2 is always an argument error; `error.errors` lists every problem at once
- Retry only when `error.retryable` is true, after `error.retry_after_ms`
"""


def render(app: App) -> dict[str, str]:
    """File name to text: ``CONTEXT.md`` and one ``SKILL-<command>.md`` per app command"""
    manifest = app.manifest()
    commands = manifest["commands"]
    assert isinstance(commands, dict)
    paths = sorted((p for p in app.commands if p not in app.builtins), key=lambda p: p.value)
    files = {CONTEXT_FILE: render_context(app, manifest, paths)}
    for path in paths:
        files[skill_file(path)] = render_skill(app, app.commands[path], commands[path.value])
    return files
