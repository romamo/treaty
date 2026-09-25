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


def check_path(raw: str, flag: str) -> Path:
    """Return ``raw`` as a ``Path`` or raise a validation-phase ``ParseError``

    Rejected, with ``rejected_pattern`` in the error context: null bytes,
    percent-encoded sequences, and any ``..`` segment.
    """
    if "\x00" in raw:
        raise ParseError(
            f"{flag!r} contains a null byte",
            context={"flag": flag, "value": raw, "rejected_pattern": "null_byte"},
        )
    if _PERCENT_RE.search(raw):
        raise ParseError(
            f"{flag!r} contains a percent-encoded sequence",
            context={"flag": flag, "value": raw, "rejected_pattern": "percent_encoded"},
            suggestion=f"pass the decoded path: --{flag} {unquote(raw)}",
        )
    path = Path(raw)
    if ".." in path.parts:
        raise ParseError(
            f"{flag!r} escapes its base directory with '..'",
            context={"flag": flag, "value": raw, "rejected_pattern": "path_traversal"},
            suggestion=f"pass the absolute path if intended: --{flag} {path.resolve()}",
        )
    return path
