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
    JSONL = "jsonl"
    """One compact envelope per line: what ``json`` writes, named for readers that ask"""
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


PAGERS: Mapping[str, str] = {
    "PAGER": "cat",
    "GIT_PAGER": "cat",
    "MANPAGER": "cat",
    "LESS": "-F -X -R",
    "MORE": "",
}
"""REQ-F-010, REQ-F-046: no child can open an interactive pager"""
NO_EDITOR: Mapping[str, str] = {"EDITOR": "true", "VISUAL": "true", "GIT_EDITOR": "true"}
"""REQ-F-055: off a terminal, a child that opens an editor gets a no-op that exits at once"""


def child_settings(*, color: bool, interactive: bool) -> dict[str, str]:
    """What every child process gets: never a pager, no color once the tool has none
    (REQ-F-008), and no editor unless stdin and stdout are a terminal"""
    settings = dict(PAGERS)
    if not color:
        settings["NO_COLOR"] = "1"
    if not interactive:
        settings.update(NO_EDITOR)
    return settings


def quiet_children(
    env: MutableMapping[str, str], *, stdout_isatty: bool, stdin_isatty: bool
) -> None:
    """Write ``child_settings`` into ``env``, which every child inherits"""
    env.update(
        child_settings(
            color=color_allowed(env, stdout_isatty), interactive=stdin_isatty and stdout_isatty
        )
    )


def is_headless(env: Mapping[str, str], *, interactive: bool, platform: str) -> bool:
    """No person or display to show a window to (REQ-F-057): no terminal on stdin and
    stdout, a CI system, or no X11 or Wayland display where one is needed (Linux and
    the BSDs, or any system reached over SSH)"""
    if not interactive or env.get("CI"):
        return True
    needs_display = platform not in ("darwin", "win32") or "SSH_TTY" in env
    return needs_display and not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY")
