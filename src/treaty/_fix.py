"""Executable ``fix_command`` values (REQ-C-030).

An agent runs ``error.fix_command`` verbatim, so it must be one command with no
placeholder, no shell syntax, and nothing destructive: the app itself or a companion
program the app declares, such as ``mkdir``. Declared fixes are checked before the first
run; a fix raised at run time is checked when it is raised.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Collection, Mapping

from ._command import Command, DangerLevel
from ._parse import resolve_path
from ._values import CommandPath

# Redirection, expansion, pipes, command lists, and placeholders such as <your-token>
_SHELL = re.compile(r"[<>$`|;&\n]")


def fix_problem(
    text: object,
    *,
    app_name: str,
    companions: Collection[str],
    commands: Mapping[CommandPath, Command] | None,
) -> str | None:
    """Why ``text`` cannot be run verbatim as a fix, or None when it can. With
    ``commands`` None only its shape is checked, for a target not registered yet"""
    if not isinstance(text, str) or not text.strip():
        return "a fix command is non-empty text"
    found = _SHELL.search(text)
    if found is not None:
        return (
            f"{text!r} holds {found.group()!r}: a fix runs verbatim, so it has no placeholder "
            "or shell syntax; state the condition in fix_required instead"
        )
    try:
        words = shlex.split(text)
    except ValueError:
        return f"{text!r} has unbalanced quotes"
    program = words[0]
    if program != app_name and program not in companions:
        return (
            f"{text!r} runs {program!r}, which is neither {app_name!r} nor a companion; "
            f'declare it with App(companions=("{program}",))'
        )
    if program != app_name or commands is None:
        return None
    route = resolve_path(words[1:], commands)
    if route.path is None:
        return f"{text!r} names no command of {app_name}"
    if commands[route.path].danger_level is DangerLevel.DESTRUCTIVE:
        return f"{text!r} runs {route.path}, which is destructive; a fix is safe to run twice"
    return None
