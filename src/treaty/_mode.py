"""Output mode resolution: explicit flag, then ``<APP>_FORMAT``, then tty detection."""

from __future__ import annotations

import functools
import sys
from collections.abc import Callable, Collection, Mapping, MutableMapping
from enum import StrEnum

from ._env import FORMAT, app_var
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
    ID = "id"
    """The bare primary identifier per line, for piping (REQ-O-005)"""


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
    # REQ-O-042: the tool's own variable, failing as the same --format value would
    var = app_var(app_name, FORMAT.key)
    forced = env.get(var)
    if forced:
        try:
            return resolve_mode(forced, {}, stdout_isatty, offered, app_name)
        except ParseError as exc:
            exc.context["source"] = var
            raise
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


UPDATE_NOTIFIERS: Mapping[str, str] = {
    "CI": "1",
    "NO_UPDATE_NOTIFIER": "1",
    "NPM_CONFIG_UPDATE_NOTIFIER": "false",
    "HOMEBREW_NO_AUTO_UPDATE": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
}
"""REQ-F-050: off a terminal or under CI, no child or library prints an update notice"""
C_LOCALE: Mapping[str, str] = {"LANG": "C", "LC_MESSAGES": "C", "LC_NUMERIC": "C"}
"""REQ-F-066: children answer in English with dot decimals, unless ``preserve_locale``"""
WINDOWS_C_LOCALE: Mapping[str, str] = {"LC_ALL": "C", "LC_NUMERIC": "C"}
"""Windows children take the locale from the user profile; ported tools that read the
variables get the C locale, as before"""
LOCALE_CATEGORIES = (
    "LC_ALL",
    "LC_ADDRESS",
    "LC_COLLATE",
    "LC_CTYPE",
    "LC_IDENTIFICATION",
    "LC_MEASUREMENT",
    "LC_MESSAGES",
    "LC_MONETARY",
    "LC_NAME",
    "LC_NUMERIC",
    "LC_PAPER",
    "LC_TELEPHONE",
    "LC_TIME",
)
"""The POSIX and glibc locale variables; ``LC_TERMINAL`` and the like are not locales"""
UTF8_CTYPE = "C.UTF-8"


@functools.cache
def locale_available(name: str, library: str | None = None) -> bool:
    """Whether the C library can load ``name`` for LC_CTYPE. ``newlocale`` builds a
    separate locale object, freed at once, so the process's own locale never changes
    and no other thread sees a switch, as ``locale.setlocale`` would cause.

    ``library`` is the C library to open, the running process's by default. A Python
    without ``ctypes``, or whose C library cannot be opened, such as a static build,
    gets ``False``, so children get ``C``: the probe runs for every command, and must
    not fail one that starts no child"""
    if sys.platform == "win32":
        return False
    try:
        import ctypes
    except ImportError:
        return False
    import locale

    try:
        libc = ctypes.CDLL(library)
    except OSError:
        return False
    if not (hasattr(libc, "newlocale") and hasattr(libc, "freelocale")):
        return False
    libc.newlocale.restype = ctypes.c_void_p
    libc.newlocale.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p)
    libc.freelocale.argtypes = (ctypes.c_void_p,)
    # glibc and musl number the masks by category; macOS and the BSDs, where LC_ALL is
    # category 0, leave that bit out
    offset = 1 if locale.LC_ALL == 0 else 0
    handle = libc.newlocale(1 << (locale.LC_CTYPE - offset), name.encode("ascii"), None)
    if not handle:
        return False
    libc.freelocale(handle)
    return True


def child_ctype(available: Callable[[str], bool] = locale_available) -> str:
    """``C.UTF-8`` where the platform has it (glibc 2.35+, musl, macOS), else ``C``"""
    return UTF8_CTYPE if available(UTF8_CTYPE) else "C"


def normalize_locale(env: MutableMapping[str, str], *, ctype: str) -> None:
    """REQ-F-066: the C locale for a child, with ``ctype`` as its character set.

    ``LC_ALL`` would override ``LC_CTYPE``, so it goes, with every other locale
    variable the user set: ``LANG=C`` then covers collation, time, and money, and
    ``LC_MESSAGES`` and ``LC_NUMERIC`` are named as the requirement asks. A UTF-8
    ``ctype`` lets tools that refuse an ASCII locale, like Ansible, start"""
    if sys.platform == "win32":
        env.update(WINDOWS_C_LOCALE)
        return
    for name in LOCALE_CATEGORIES:
        env.pop(name, None)
    env.update(C_LOCALE)
    env["LC_CTYPE"] = ctype


def suppress_updates(env: Mapping[str, str], *, interactive: bool) -> bool:
    """Whether update notices are silenced: no terminal on stdin and stdout, or CI"""
    return not interactive or bool(env.get("CI"))


def child_settings(*, color: bool, interactive: bool, ci: bool = False) -> dict[str, str]:
    """What every child process gets: never a pager, no color once the tool has none
    (REQ-F-008), no editor unless stdin and stdout are a terminal, and no update notice
    off a terminal or under CI (REQ-F-050)"""
    settings = dict(PAGERS)
    if not color:
        settings["NO_COLOR"] = "1"
    if not interactive:
        settings.update(NO_EDITOR)
    if not interactive or ci:
        settings.update(UPDATE_NOTIFIERS)
    return settings


def quiet_children(
    env: MutableMapping[str, str], *, stdout_isatty: bool, stdin_isatty: bool
) -> None:
    """Write ``child_settings`` into ``env``, which every child inherits"""
    env.update(
        child_settings(
            color=color_allowed(env, stdout_isatty),
            interactive=stdin_isatty and stdout_isatty,
            ci=bool(env.get("CI")),
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
