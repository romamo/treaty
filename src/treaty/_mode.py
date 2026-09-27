"""Output mode resolution: explicit flag, then environment, then tty detection."""

from __future__ import annotations

from collections.abc import Collection, Mapping, MutableMapping
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
            raise ParseError(f"--format {explicit!r} is not offered by {app_name}", context=context)
        return mode
    forced = env.get("TREATY_FORMAT")
    if forced is not None:
        return resolve_mode(forced, {}, stdout_isatty, offered, app_name)
    if not stdout_isatty or env.get("CI"):
        return Format.JSON
    return Format.PLAIN


# REQ-F-008: any of these, even empty for NO_COLOR, turns color off; CI systems set the rest
_COLOR_OFF = ("CI", "GITHUB_ACTIONS", "JENKINS_URL")


def color_allowed(env: Mapping[str, str], stdout_isatty: bool) -> bool:
    """Whether text written for a person may carry color (REQ-F-008)"""
    if not stdout_isatty or "NO_COLOR" in env or env.get("TERM") == "dumb":
        return False
    return not any(env.get(name) for name in _COLOR_OFF)


def quiet_children(env: MutableMapping[str, str], stdout_isatty: bool) -> None:
    """Settings every child process inherits: never a pager (REQ-F-010), and no color
    once the tool itself has none (REQ-F-008)"""
    env["PAGER"] = "cat"
    env["GIT_PAGER"] = "cat"
    if not color_allowed(env, stdout_isatty):
        env["NO_COLOR"] = "1"
