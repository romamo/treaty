"""Output mode resolution: explicit flag, then environment, then tty detection."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from enum import StrEnum

from ._errors import ParseError


class Format(StrEnum):
    """Every ``--format`` value treaty knows; an app offers ``plain``, ``json``, and the
    ones it registers a renderer for with ``app.format()``"""

    PLAIN = "plain"
    JSON = "json"
    CSV = "csv"
    TSV = "tsv"
    YAML = "yaml"
    MARKDOWN = "markdown"


def resolve_mode(
    explicit: str | None,
    env: Mapping[str, str],
    stdout_isatty: bool,
    offered: Collection[Format],
    app_name: str,
) -> Format:
    if explicit is not None:
        context = {
            "flag": "format",
            "value": explicit,
            "allowed": [m.value for m in Format if m in offered],
        }
        try:
            mode = Format(explicit)
        except ValueError:
            raise ParseError(f"unknown --format {explicit!r}", context=context) from None
        if mode not in offered:
            # A real format with no renderer registered, not a typo
            raise ParseError(f"{app_name} does not offer --format {explicit!r}", context=context)
        return mode
    forced = env.get("TREATY_FORMAT")
    if forced is not None:
        return resolve_mode(forced, {}, stdout_isatty, offered, app_name)
    if not stdout_isatty or env.get("CI"):
        return Format.JSON
    return Format.PLAIN
