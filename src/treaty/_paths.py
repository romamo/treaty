"""Hardening for ``Path`` fields: the agent hallucination patterns of REQ-F-045.

Every ``pathlib.Path`` argument passes through :func:`check_path` on both the
argv and the JSON (``exec``, ``--raw-payload``) routes, before any handler runs.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

from ._errors import ParseError

PATTERN_TYPE = "filepath"
_PERCENT_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_CONTROL = {
    "\x00": ("null byte", "null_byte"),
    "\n": ("newline", "newline"),
    "\r": ("carriage return", "carriage_return"),
}
_PATTERNS_WITHOUT_FIX = frozenset(pattern for _, pattern in _CONTROL.values())


def _rejected(raw: str) -> tuple[str, str] | None:
    """Why ``raw`` is refused, as the message's end and the ``rejected_pattern``, or None"""
    for char, (what, pattern) in _CONTROL.items():
        if char in raw:
            return f"contains a {what}", pattern
    if _PERCENT_RE.search(raw):
        return "contains a percent-encoded sequence", "percent_encoded"
    if ".." in Path(raw).parts:
        return "escapes its base directory with '..'", "path_traversal"
    return None


def _passing_form(raw: str) -> str | None:
    """The decoded path, made absolute when it climbs with ``..``, if the checks accept it:
    ``%2e%2e`` decodes to a climb, and ``%00`` or a double encoding has no form that passes"""
    candidate = unquote(raw)
    problem = _rejected(candidate)
    if problem is not None and problem[1] == "path_traversal":
        candidate = str(Path(candidate).resolve())
        problem = _rejected(candidate)
    return None if problem is not None else candidate


def check_path(raw: str, flag: str) -> Path:
    """Return ``raw`` as a ``Path`` or raise a validation-phase ``ParseError``

    Rejected, with ``rejected_pattern`` in the error context: null bytes, line breaks,
    percent-encoded sequences, and any ``..`` segment. A suggestion is offered only when
    it passes these same checks.
    """
    problem = _rejected(raw)
    if problem is None:
        return Path(raw)
    what, pattern = problem
    passing = None if pattern in _PATTERNS_WITHOUT_FIX else _passing_form(raw)
    suggestion = None
    if passing is not None:
        lead = (
            "pass the decoded path"
            if pattern == "percent_encoded"
            else "pass the absolute path if intended"
        )
        suggestion = f"{lead}: --{flag} {passing}"
    raise ParseError(
        f"{flag!r} {what}",
        context={"flag": flag, "value": raw, "rejected_pattern": pattern},
        suggestion=suggestion,
    )
