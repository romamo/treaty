"""What a run knows about itself for ``meta``: when it started, where, and in which trace.

Read once per process from the run's environment, never ``os.environ``, so ``App.run``
and MCP callers control them; ``exec`` lines share them.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from ._errors import ParseError

TRACE_ENV = "TOOL_TRACE_ID"
"""Literal, not ``<APP>_TRACE_ID``: a trace crosses tools (REQ-F-025)"""
MAX_TRACE_ID = 256


def utc_timestamp() -> str:
    """Now as ISO 8601 in UTC, milliseconds, with ``Z`` (REQ-F-024)"""
    now = dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds")
    return now.removesuffix("+00:00") + "Z"


def read_trace_id(env: Mapping[str, str]) -> str | None:
    """``TOOL_TRACE_ID``, or None when unset or empty; a value no log line can carry
    (too long, or with control characters) is an argument error"""
    value = env.get(TRACE_ENV)
    if not value:
        return None
    if len(value) > MAX_TRACE_ID or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ParseError(
            f"{TRACE_ENV} must be at most {MAX_TRACE_ID} printable characters",
            code="TRACE_ID_INVALID",
            context={"variable": TRACE_ENV, "length": len(value)},
            suggestion=f"set {TRACE_ENV} to a short printable ID, or unset it",
        )
    return value


def logical_cwd(env: Mapping[str, str]) -> Path:
    """The working directory as ``pwd`` prints it: ``PWD`` when it names the same
    directory as ``os.getcwd()``, so a symlinked ``/tmp`` stays ``/tmp`` (REQ-F-027).
    A working directory removed under the process has no physical path: then ``PWD``,
    or the root, stands in, so help and version still answer (REQ-F-068)."""
    pwd = env.get("PWD")
    try:
        physical = Path.cwd()
    except FileNotFoundError:
        return Path(pwd) if pwd and os.path.isabs(pwd) else Path(os.sep)
    if pwd and os.path.isabs(pwd):
        try:
            if os.path.samefile(pwd, physical):
                return Path(pwd)
        except OSError:
            pass  # PWD names a directory that is gone; the physical path stands
    return physical


def find_project_root(start: Path, markers: Sequence[str]) -> Path | None:
    """The first directory from ``start`` up that holds one of ``markers``"""
    for directory in (start, *start.parents):
        if any((directory / m).exists() for m in markers):
            return directory
    return None
