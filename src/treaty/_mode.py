"""Output mode resolution: explicit flag, then environment, then tty detection."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from ._errors import ParseError


class OutputMode(StrEnum):
    HUMAN = "human"
    JSON = "json"


def resolve_mode(explicit: str | None, env: Mapping[str, str], stdout_isatty: bool) -> OutputMode:
    if explicit is not None:
        try:
            return OutputMode(explicit)
        except ValueError:
            raise ParseError(
                f"unknown --format {explicit!r}",
                context={
                    "flag": "format",
                    "value": explicit,
                    "allowed": [m.value for m in OutputMode],
                },
            ) from None
    forced = env.get("TREATY_FORMAT")
    if forced is not None:
        return resolve_mode(forced, {}, stdout_isatty)
    if not stdout_isatty or env.get("CI"):
        return OutputMode.JSON
    return OutputMode.HUMAN
