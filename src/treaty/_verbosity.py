"""How much a run writes on stderr (REQ-O-008, REQ-F-038).

A person at a terminal sees info and progress lines; an agent, off a terminal or under
``CI``, sees only errors and warnings, so logs never cost it tokens. ``--quiet``,
``--verbose``, and ``--debug`` override either default; ``-v`` is short for ``--verbose``
and ``-vv`` for ``--debug`` on every command without a ``-v`` of its own.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from enum import Enum, IntEnum

from ._errors import ParseError

QUIET_FLAG = "quiet"
VERBOSE_FLAG = "verbose"
DEBUG_FLAG = "debug"
WARNINGS_AS_ERRORS_FLAG = "warnings-as-errors"
VERBOSE_SHORT = "v"
"""``-v`` is ``--verbose``, ``-vv`` or ``-v -v`` is ``--debug``; a command that declares
``short="v"`` keeps ``-v`` for its own flag"""
_SHORT_VERBOSE = re.compile(rf"-{VERBOSE_SHORT}+")

TRACE = logging.getLogger("treaty")
"""The framework's own debug trace; ``--debug`` routes it, and every other logger's
records, through the run's redacting stderr writer"""
TRACE_FIELDS = "treaty_fields"
"""The ``LogRecord`` attribute ``trace`` puts its fields under"""


class Verbosity(IntEnum):
    QUIET = 0
    """``--quiet``: nothing on stderr, not even errors; the envelope carries them"""
    AUTO = 1
    """Off a terminal or under CI: errors and warnings only"""
    NORMAL = 2
    """A terminal: info and progress too"""
    VERBOSE = 3
    """``--verbose``: info and progress anywhere"""
    DEBUG = 4
    """``--debug``: debug lines and the framework's own trace too"""


class Level(Enum):
    """A stderr line's level, and the verbosity from which it is written"""

    ERROR = "error"
    WARN = "warn"
    INFO = "info"
    PROGRESS = "progress"
    DEBUG = "debug"

    @property
    def shown_from(self) -> Verbosity:
        return _SHOWN_FROM[self]


_SHOWN_FROM: Mapping[Level, Verbosity] = {
    Level.ERROR: Verbosity.AUTO,
    Level.WARN: Verbosity.AUTO,
    Level.INFO: Verbosity.NORMAL,
    Level.PROGRESS: Verbosity.NORMAL,
    Level.DEBUG: Verbosity.DEBUG,
}

_FLAGS: Mapping[str, Verbosity] = {
    QUIET_FLAG: Verbosity.QUIET,
    VERBOSE_FLAG: Verbosity.VERBOSE,
    DEBUG_FLAG: Verbosity.DEBUG,
}


def resolve_verbosity(flags: frozenset[str], env: Mapping[str, str], tty: bool) -> Verbosity:
    """An explicit flag wins, even over CI; else AUTO off a terminal or under CI.
    The three flags are exclusive: two of them are an argument error."""
    given = sorted(name for name in flags if name in _FLAGS)
    if len(given) > 1:
        raise ParseError(
            f"{' and '.join(f'--{n}' for n in given)} are exclusive; pass one",
            context={"flags": given},
            suggestion="pass one of --quiet, --verbose, --debug",
        )
    if given:
        return _FLAGS[given[0]]
    return Verbosity.AUTO if not tty or env.get("CI") else Verbosity.NORMAL


def short_verbosity(token: str) -> int:
    """How many ``v`` a ``-v``, ``-vv``, ... token counts; 0 for any other token"""
    return len(token) - 1 if _SHORT_VERBOSE.fullmatch(token) else 0


def trace(event: str, **fields: object) -> None:
    """One step of the framework's work, written on stderr under ``--debug`` (REQ-O-008)"""
    if TRACE.isEnabledFor(logging.DEBUG):
        TRACE.debug(event, extra={TRACE_FIELDS: fields})
