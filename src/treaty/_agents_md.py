"""AGENTS.md rendered from the registry, and the check that keeps it current.

``render_file`` writes the ``cli-version`` comment on line 1 and the generated sections
between ``<!-- treaty:begin -->`` and ``<!-- treaty:end -->``; everything outside the
markers is the author's and is kept (REQ-O-043, REQ-O-044). ``check`` compares an
AGENTS.md, a skill file, or a ``--list-tools`` JSON file with the app: the declared
version, the required sections, and every command, flag, and variable the text names
against the ``--help`` output (REQ-O-045, REQ-O-046).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ._auth import HEADLESS_FLAG
from ._command import INPUT_FILE_FLAG, Command
from ._env import KNOWN, UNPREFIXED, app_var
from ._envnames import declared_text
from ._errors import Exit, RegistrationError
from ._framework import (
    CONFIRM_FLAG,
    LIVE_FLAG,
    NON_INTERACTIVE_FLAG,
    RAW_PAYLOAD_FLAG,
    YES_FLAG,
    framework_flags,
)
from ._help import declared_env_rows, env_readers, global_rows, render_command, render_root
from ._manifest import global_flag_entries
from ._values import CommandPath

if TYPE_CHECKING:
    from ._app import App

AGENTS_FILE = "AGENTS.md"
BEGIN = "<!-- treaty:begin -->"
END = "<!-- treaty:end -->"
_VERSION = re.compile(r"<!-- cli-version: (\S+) -->")
SECTIONS = (
    "Installation",
    "Canonical Invocation",
    "Non-Interactive Flags",
    "Environment Variables",
    "Input Conventions",
    "CI Validation",
)
PROMPT_FLAGS = (CONFIRM_FLAG, LIVE_FLAG, YES_FLAG, NON_INTERACTIVE_FLAG, HEADLESS_FLAG)
"""The framework flags that answer or suppress a prompt (REQ-F-009)"""

SHARED: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("CI", "GITHUB_ACTIONS", "JENKINS_URL"),
        "CI detection: JSON output, no prompts, and no update check",
    ),
    (
        ("NO_COLOR", "TERM", "COLUMNS"),
        "Color, terminal capabilities, and table width of plain output",
    ),
    (("TOOL_TRACE_ID",), "Trace id of the run, inherited by child processes"),
    (
        ("HOME", "USER", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"),
        "Where the user config file, state, and caches live",
    ),
    (("PATH", "SHELL", "PWD"), "Passed to child processes; PATH finds required tools"),
    (
        ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"),
        "Proxy and CA bundle for network calls, lowercase proxy names too",
    ),
)
"""The unprefixed variables of ``_env.UNPREFIXED``, grouped, as AGENTS.md lists them"""

_SPAN = re.compile(r"`([^`\n]+)`")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
_FLAG = re.compile(r"--[a-z0-9][a-z0-9-]*")


@dataclass(frozen=True, slots=True)
class EnvVarDoc:
    name: str
    type: str
    required: bool
    description: str


def env_vars(app: App) -> tuple[EnvVarDoc, ...]:
    """Every variable the app reads, sorted by name: treaty's own, each setting and the
    names it declares, each secret flag's default, and each command's token variables"""
    found: dict[str, EnvVarDoc] = {}

    def add(doc: EnvVarDoc) -> None:
        old = found.get(doc.name)
        if old is not None and old.type != doc.type:
            raise RegistrationError(f"{doc.name} is read as {old.type} and as {doc.type}")
        found.setdefault(doc.name, doc)

    for v in KNOWN:
        add(EnvVarDoc(app_var(app.name, v.key), v.type, False, v.description))
    if app.settings is not None:
        for s in app.settings.fields:
            text = f"Setting {s.name}, over the config files"
            own = app_var(app.name, s.name)
            add(EnvVarDoc(own, s.classified.flag_type.value, False, text))
            for n in s.env:
                text = declared_text(n, f"Setting {s.name}", own, own)
                add(EnvVarDoc(n.name, s.classified.flag_type.value, False, text))
    readers = env_readers(app.commands, app._builtins)
    for path, command in sorted(app.commands.items(), key=lambda kv: kv[0].value):
        for f in command.fields:
            if f.secret:
                var = command.secret_env_vars[f.name]
                text = f"Default of --{f.flag} of {readers[var]}"
                add(EnvVarDoc(var, "string", f.required, text))
        for var, f, text in declared_env_rows(command, readers):
            kind = "string" if f.secret else f.flag_type.value
            add(EnvVarDoc(var, kind, False, text))
        for var in command.token_env_vars:
            text = f"Token of {path.value} when no --token-env-var is given"
            add(EnvVarDoc(var, "string", False, text))
    return tuple(sorted(found.values(), key=lambda d: d.name))


def help_texts(app: App) -> dict[tuple[str, ...], str]:
    """The plain ``--help`` of the root and of every command, keyed by command words"""
    rows = global_rows(global_flag_entries(app.formats, app.name, app._media_types))
    texts: dict[tuple[str, ...], str] = {
        (): render_root(
            app.name, app.description, app.commands, app._groups, rows, app.environment()
        )
    }
    for path, command in app.commands.items():
        texts[path.parts] = render_command(app.name, command, rows)
    return texts


# Rendering


def _sorted(app: App) -> list[tuple[CommandPath, Command]]:
    return sorted(app.commands.items(), key=lambda kv: kv[0].value)


def _spans(invocation: str, paths: list[CommandPath]) -> str:
    return ", ".join(f"`{invocation} {' '.join(p.parts)}`" for p in paths)


def installation(name: str, install: str) -> str:
    """The default ``## Installation``: written once, then the author's to edit"""
    verify = f"{name} --version"
    width = max(len(install), len(verify))
    return f"""## Installation

```bash
{install:<{width}}  # non-interactive, reads no stdin, exits 0 when repeated
{verify:<{width}}  # verify: exits 0 and prints the version (data.version in JSON)
```
"""


def render_block(app: App, target: str, invocation: str) -> str:
    """The generated sections, markers included"""
    commands = _sorted(app)
    listed = "\n".join(
        f"- `{invocation} {' '.join(p.parts)}`: {c.description}" for p, c in commands
    )
    prompt = ["- `--format json`: the JSON envelope on stdout, the default off a terminal"]
    for name in PROMPT_FLAGS:
        using = [(p, c) for p, c in commands if any(f.name == name for f in framework_flags(c))]
        if using:
            flag = next(f for f in framework_flags(using[0][1]) if f.name == name)
            where = _spans(invocation, [p for p, _ in using])
            prompt.append(f"- `--{name}` ({where}): {flag.describe(using[0][1])}")
    prompt.append(
        "- Off a terminal nothing prompts: an answer a command needs fails with an exit code"
    )
    env = "\n".join(
        f"- `{d.name}` ({d.type}, {'required' if d.required else 'optional'}): {d.description}"
        for d in env_vars(app)
    )
    shared = "\n".join(
        f"- {', '.join(f'`{n}`' for n in names)} (string, optional): {text}"
        for names, text in SHARED
    )
    raw = [p for p, c in commands if c.supports_raw_payload]
    stdin = [p for p, c in commands if any(f.name == INPUT_FILE_FLAG for f in framework_flags(c))]
    inputs = [
        "- Arguments are positionals and `--flag value` pairs; "
        f"`{invocation} <command> --schema` prints a command's input and output schema, and "
        "`--validate-only` checks the arguments without running anything",
    ]
    if raw:
        inputs.append(
            f"- `--{RAW_PAYLOAD_FLAG}` takes every field as one JSON object on "
            + _spans(invocation, raw)
        )
    if stdin:
        inputs.append(
            f"- The input is read from stdin, or from `--{INPUT_FILE_FLAG}` (`-` is stdin), "
            f"on {_spans(invocation, stdin)}"
        )
    delegating = [p for p, c in commands if c.passthrough]
    if delegating:
        # #35: the one grammar where treaty's flags precede the path
        one = len(delegating) == 1
        inputs.append(
            f"- {_spans(invocation, delegating)} {'hands' if one else 'hand'} every token "
            f"after the command path to the tool {'it delegates' if one else 'they delegate'} "
            f"to, verbatim; {'its' if one else 'their'} own flags, such as `--output`, go "
            "before the path, the tool's output is on stdout, and the envelope is the last "
            'line on stderr. Exec lines pass the tokens as `"argv": [...]`'
        )
    inputs.append(
        f"- `{invocation} exec` reads JSONL DispatchRequest lines from stdin, one command "
        "each, and answers one envelope line per request"
    )
    newline = "\n"
    return f"""{BEGIN}
## Canonical Invocation

`{invocation} <command> [arguments] [flags]`, where the command is one of these;
`{invocation} manifest` returns every command, flag, and exit code as JSON:

{listed}

## Non-Interactive Flags

{newline.join(prompt)}

## Environment Variables

{env}

Shared conventions, read without the prefix:

{shared}

## Input Conventions

{newline.join(inputs)}

## CI Validation

`uv run treaty check-docs {target} {AGENTS_FILE}` compares this file with the binary:
the `cli-version` comment, these sections, and every command, flag, and variable named
here. Drift exits 81 with one line per mismatch. `uv run treaty agents-md {target}`
rewrites the text between the treaty markers and keeps the rest.
{END}
"""


def render_file(
    app: App, existing: str | None, target: str, invocation: str, install: str | None = None
) -> str:
    """A new AGENTS.md, or ``existing`` with its version comment and generated block
    replaced; without markers the block is appended, after a default Installation
    section when the file has none. ``install`` defaults to ``uv tool install <name>``"""
    block = render_block(app, target, invocation)
    setup = installation(app.name, install or f"uv tool install {app.name}")
    head = f"<!-- cli-version: {app.version} -->\n"
    if existing is None:
        return f"{head}# AGENTS.md: {app.name}\n\n{setup}\n{block}"
    lines = existing.splitlines(keepends=True)
    if lines and _VERSION.fullmatch(lines[0].strip()):
        lines = lines[1:]
    text = "".join(lines)
    start = text.find(BEGIN)
    if start != -1:
        # The end marker after the begin one: prose may quote the marker earlier
        end = text.find(END, start)
        if end == -1:
            raise Exit.PRECONDITION(
                f"The file has {BEGIN} but no {END} after it",
                context={"begin_marker": BEGIN, "end_marker": END},
                fix_required=f"add {END} where the generated sections end, or remove {BEGIN}",
            )
        return head + text[:start] + block + text[end + len(END) :].removeprefix("\n")
    text = text.rstrip("\n") + "\n\n"
    if not re.search(r"^## Installation\s*$", text, re.MULTILINE):
        text += setup + "\n"
    return head + text + block


# Checking


@dataclass(frozen=True, slots=True)
class Mismatch:
    file: Path
    line: int
    kind: str
    """version, section, command, flag, or env"""
    name: str
    problem: str


def declared_version(text: str) -> tuple[int, str] | None:
    """The line and value of the ``cli-version`` comment on line 1, or of the skill
    frontmatter's ``version:``"""
    lines = text.splitlines()
    if lines and lines[0] == "---":
        for n, line in enumerate(lines[1:], 2):
            if line == "---":
                break
            if line.startswith("version: "):
                return (n, line.removeprefix("version: ").strip().strip('"'))
        return None
    found = _VERSION.fullmatch(lines[0].strip()) if lines else None
    return None if found is None else (1, found.group(1))


def _sections(text: str) -> dict[str, tuple[int, str]]:
    """``## `` heading to its line and body, marker comments left out"""
    found: dict[str, tuple[int, str]] = {}
    current: str | None = None
    for n, line in enumerate(text.splitlines(), 1):
        if line.startswith("## "):
            current = line[3:].strip()
            found[current] = (n, "")
        elif current is not None and line.strip() not in (BEGIN, END):
            at, body = found[current]
            found[current] = (at, body + line + "\n")
    return found


def _mentions(text: str) -> Iterator[tuple[int, str, str | None]]:
    """Each backticked span and each line of a fenced block, with its line and section"""
    section: str | None = None
    fenced = False
    for n, line in enumerate(text.splitlines(), 1):
        if line.startswith("## "):
            section = line[3:].strip()
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            yield n, line.split(" #")[0].strip(), section
        else:
            for span in _SPAN.findall(line):
                yield n, span, section


def _command(app: App, words: list[str]) -> tuple[tuple[str, ...], str | None]:
    """The longest command or group the words name, and the word that names neither"""
    parts: tuple[str, ...] = ()
    for word in words:
        if word.startswith(("-", "<", "[", ".")):
            break
        candidate = (*parts, word)
        if not any(p.parts[: len(candidate)] == candidate for p in app.commands):
            # After a command the word is an argument; after a group it names nothing
            named = any(p.parts == parts for p in app.commands)
            return parts, None if named else word
        parts = candidate
    return parts, None


_INVOCATION = re.compile(r"^`(.+?) <command> \[arguments\] \[flags\]`", re.MULTILINE)


def _stale_sections(app: App, label: Path, text: str, target: str) -> list[Mismatch]:
    """Each generated section that differs from what ``treaty agents-md`` writes now: a
    command or variable added since is missing from it, though every name it has exists"""
    start = text.find(BEGIN)
    end = text.find(END, start)
    if start == -1 or end == -1:
        return []
    written = text[start : end + len(END)]
    named = _INVOCATION.search(written)
    fresh = _sections(render_block(app, target, app.name if named is None else named.group(1)))
    old, lines = _sections(written), _sections(text)
    return [
        Mismatch(
            label,
            lines.get(title, (1, ""))[0],
            "section",
            title,
            f"differs from what treaty agents-md {target} writes; run it",
        )
        for title, (_, body) in fresh.items()
        if title in old and old[title][1].strip() != body.strip()
    ]


def check(
    app: App, label: Path, text: str, *, agents_md: bool, target: str | None = None
) -> list[Mismatch]:
    """Every way ``text`` disagrees with the app; ``agents_md`` adds the section check,
    and ``target``, the app's import path, the check of the generated sections"""
    found: list[Mismatch] = []
    version = declared_version(text)
    if version is None:
        found.append(
            Mismatch(label, 1, "version", "", f"no cli-version; {app.name} is {app.version}")
        )
    elif version[1] != app.version:
        found.append(
            Mismatch(
                label,
                version[0],
                "version",
                version[1],
                f"declares {version[1]}, {app.name} --version is {app.version}",
            )
        )
    if agents_md:
        sections = _sections(text)
        for title in SECTIONS:
            at, body = sections.get(title, (0, ""))
            if not at:
                found.append(Mismatch(label, 1, "section", title, "missing"))
            elif not body.strip():
                found.append(Mismatch(label, at, "section", title, "empty"))
    texts = help_texts(app)
    every = "\n".join(texts.values())
    env_names = set(_ENV_NAME.findall(every)) | UNPREFIXED
    prefix = app_var(app.name, "x").removesuffix("X")
    for line, span, section in _mentions(text):
        words = _invoked(span.split())
        if words and words[0] == app.name:
            parts, unknown = _command(app, words[1:])
            if unknown is not None:
                found.append(
                    Mismatch(label, line, "command", unknown, f"not a command of {app.name}")
                )
                continue
            where = texts.get(parts, texts[()])
            for word in words[1:]:
                flag = _FLAG.match(word)
                if flag and not _has(where, flag.group()):
                    shown = " ".join((app.name, *parts, "--help"))
                    found.append(Mismatch(label, line, "flag", flag.group(), f"not in {shown}"))
        elif section == "Non-Interactive Flags" and _FLAG.fullmatch(span.split()[0]):
            flag_name = span.split()[0]
            if not _has(every, flag_name):
                found.append(Mismatch(label, line, "flag", flag_name, f"not a flag of {app.name}"))
        elif _ENV_NAME.fullmatch(span) and (
            section == "Environment Variables" or span.startswith(prefix)
        ):
            if span not in env_names:
                found.append(Mismatch(label, line, "env", span, f"not a variable {app.name} reads"))
    if agents_md and target is not None:
        # A section already reported for a name it should not have is not reported again
        named = {_section_at(text, m.line) for m in found}
        found += [m for m in _stale_sections(app, label, text, target) if m.name not in named]
    return found


def _section_at(text: str, line: int) -> str | None:
    """The ``## `` section ``line`` is in"""
    section: str | None = None
    for value in text.splitlines()[:line]:
        if value.startswith("## "):
            section = value[3:].strip()
    return section


_LAUNCHERS = (("uv", "run"), ("uvx",), ("pipx", "run"))
"""What runs a tool in a doc's command line before the tool's own name"""


def _invoked(words: list[str]) -> list[str]:
    """A command line from the tool's name on: a shell prompt's ``$`` and a launcher such
    as ``uv run`` come off, so ``uv run tool nosuch`` is checked like ``tool nosuch``"""
    if words[:1] == ["$"]:
        words = words[1:]
    for launcher in _LAUNCHERS:
        if tuple(words[: len(launcher)]) == launcher:
            return words[len(launcher) :]
    return words


def _has(text: str, flag: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", text) is not None


def check_tools(app: App, label: Path, text: str) -> list[Mismatch]:
    """A ``treaty-mcp --list-tools`` file: its ``cli_version`` against the app's"""
    loaded = json.loads(text)
    value = loaded.get("cli_version") if isinstance(loaded, dict) else None
    if value == app.version:
        return []
    problem = (
        "no cli_version"
        if value is None
        else f"declares {value}, {app.name} --version is {app.version}"
    )
    return [Mismatch(label, 1, "version", str(value or ""), problem)]
