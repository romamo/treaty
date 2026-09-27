"""The audit log: one JSONL entry per invocation, bounded on disk (REQ-F-026, REQ-F-042).

Every command a run answers appends one line to ``audit.jsonl``: when, which command,
its parameters with secrets redacted, the exit code, the duration, and the trace,
request, and session ids. The file is append-only; when the next line would take it past
``max_bytes`` it becomes ``audit.1.jsonl`` and the older ones shift up to
``audit.<keep>.jsonl``, the oldest dropped, so the log never holds more than
``(keep + 1) * max_bytes``. The first append of a process deletes log files older than
``max_age_days``. ``_audit.py`` is the static linter; this module is the log.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from ._atomic import exclusive
from ._env import AUDIT_LOG, app_var
from ._errors import ParseError, RegistrationError

AUDIT_LOG_UNAVAILABLE = "AUDIT_LOG_UNAVAILABLE"
FILE_NAME = "audit.jsonl"
OFF = "off"
MAX_STRING = 1024
"""Longer strings in an entry are cut, so every line stays short and is written whole"""
_DAY = 86_400


@dataclass(frozen=True, slots=True)
class AuditLog:
    """Where the audit log goes and how much of it is kept. ``path`` None is
    ``$<APP>_AUDIT_LOG``, then ``$XDG_DATA_HOME/<app>/audit.jsonl``, then
    ``~/.local/share/<app>/audit.jsonl``. ``<APP>_AUDIT_LOG=off`` turns it off for a run,
    ``App(audit_log=None)`` for the app."""

    path: Path | str | None = None
    max_bytes: int = 100 * 2**20
    """Size at which the file is rotated (REQ-F-042)"""
    keep: int = 5
    """Rotated files kept besides the live one"""
    max_age_days: int = 30
    """Log files not written for this long are deleted"""

    def __post_init__(self) -> None:
        for name in ("max_bytes", "keep", "max_age_days"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise RegistrationError(f"AuditLog: {name} is a whole number of at least 1")
        if self.path is not None:
            path = Path(self.path)
            if not path.is_absolute():
                raise RegistrationError(f"AuditLog: path {str(path)!r} is not absolute")
            object.__setattr__(self, "path", path)


DEFAULT_AUDIT_LOG = AuditLog()


def log_path(settings: AuditLog, app_name: str, env: Mapping[str, str]) -> Path | None:
    """The file this run appends to; None when the log is off or no home is known.
    ``<APP>_AUDIT_LOG=off`` wins even over ``AuditLog.path``; any other value that is not
    an absolute path is an argument error, as other variables of the app are."""
    variable = app_var(app_name, AUDIT_LOG.key)
    raw = env.get(variable) or None
    if raw is not None and raw.lower() == OFF:
        return None
    if settings.path is not None:
        return Path(settings.path)
    if raw is not None:
        if not Path(raw).is_absolute():
            raise ParseError(
                f"{variable} is an absolute file path, or off",
                context={"variable": variable, "value": raw[:256]},
                suggestion=f"set {variable} to an absolute path such as /var/log/"
                f"{app_name}/audit.jsonl, or to off",
            )
        return Path(raw)
    xdg = env.get("XDG_DATA_HOME")
    if xdg and Path(xdg).is_absolute():  # a relative one is ignored, as the XDG spec says
        return Path(xdg) / app_name / FILE_NAME
    if home := env.get("HOME"):
        return Path(home) / ".local" / "share" / app_name / FILE_NAME
    return None


def rotated(path: Path, index: int) -> Path:
    """``audit.<index>.jsonl`` beside ``path``"""
    return path.with_name(f"{path.stem}.{index}{path.suffix}")


def _numbered(path: Path) -> list[tuple[int, Path]]:
    """The rotated files beside ``path`` with their numbers, newest first"""
    if not path.parent.is_dir():
        return []
    pattern = re.compile(rf"{re.escape(path.stem)}\.(\d+){re.escape(path.suffix)}")
    found = [
        (int(m.group(1)), p)
        for p in path.parent.iterdir()
        if (m := pattern.fullmatch(p.name)) is not None
    ]
    return sorted(found)


def log_files(path: Path) -> list[Path]:
    """The rotated files, oldest first, then the live one: the order entries were written"""
    files = [p for _, p in reversed(_numbered(path))]
    return [*files, path] if path.exists() else files


def shortened(value: object) -> object:
    """``value`` with every string past ``MAX_STRING`` characters cut"""
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING else value[:MAX_STRING] + "...[truncated]"
    if isinstance(value, dict):
        return {k: shortened(v) for k, v in value.items()}
    if isinstance(value, list):
        return [shortened(v) for v in value]
    return value


_pruned: set[Path] = set()
_pruned_lock = threading.Lock()


class Journal:
    """Appends entries to one audit log file; any ``OSError`` reaches the caller"""

    def __init__(self, path: Path, settings: AuditLog) -> None:
        self.path = path
        self.settings = settings

    def append(self, entry: Mapping[str, object]) -> None:
        line = (json.dumps(shortened(dict(entry)), separators=(",", ":")) + "\n").encode()
        parent = self.path.parent
        if not parent.is_dir():
            os.makedirs(parent, mode=0o700, exist_ok=True)
        self._prune_once()
        if self._size() + len(line) > self.settings.max_bytes:
            self._rotate(len(line))
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)  # one write: a concurrent run never splits the line
        finally:
            os.close(fd)

    def _size(self) -> int:
        try:
            return self.path.stat().st_size
        except FileNotFoundError:
            return 0

    def _rotate(self, incoming: int) -> None:
        """Shift ``audit.jsonl`` to ``audit.1.jsonl`` and each older file up by one, the
        one past ``keep`` dropped; under a lock, after a re-stat, since another run may
        have rotated already"""
        with exclusive(self.path.with_name(self.path.name + ".lock")):
            size = self._size()
            if size == 0 or size + incoming <= self.settings.max_bytes:
                return
            keep = self.settings.keep
            for index, stale in _numbered(self.path):
                if index >= keep:
                    stale.unlink(missing_ok=True)
            for index in range(keep - 1, 0, -1):
                older = rotated(self.path, index)
                if older.exists():
                    os.replace(older, rotated(self.path, index + 1))
            os.replace(self.path, rotated(self.path, 1))

    def _prune_once(self) -> None:
        """REQ-F-042: on the process's first append, delete log files not written for
        ``max_age_days``"""
        with _pruned_lock:
            if self.path in _pruned:
                return
            _pruned.add(self.path)
        cutoff = time.time() - self.settings.max_age_days * _DAY
        for old in log_files(self.path):
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)


_DURATION = re.compile(r"(\d+)([smhd])")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": _DAY}


def parse_since(raw: str, now: dt.datetime) -> dt.datetime:
    """``--since``: a duration back from ``now`` (``30m``, ``1h``, ``2d``, ``45s``) or an
    ISO 8601 time, UTC when it names no zone"""
    if (match := _DURATION.fullmatch(raw)) is not None:
        seconds = int(match.group(1)) * _UNIT_SECONDS[match.group(2)]
        if seconds > 100 * 365 * _DAY:
            raise ParseError(
                f"--since {raw!r} reaches back more than a century",
                context={"flag": "since", "value": raw},
            )
        return now - dt.timedelta(seconds=seconds)
    try:
        when = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ParseError(
            f"--since {raw[:64]!r} is neither a duration nor an ISO 8601 time",
            context={"flag": "since", "value": raw[:64]},
            suggestion="pass a duration such as 30m, 1h, or 2d, or a time such as "
            "2026-01-31T12:00:00Z",
        ) from None
    return when if when.tzinfo is not None else when.replace(tzinfo=dt.UTC)


def entry_time(entry: Mapping[str, object]) -> dt.datetime | None:
    stamp = entry.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_entries(path: Path) -> Iterator[dict[str, object] | None]:
    """Every entry, oldest first; None for a line that is not a JSON object, such as one a
    full disk cut short"""
    for file in log_files(path):
        with open(file, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    yield None
                    continue
                yield entry if isinstance(entry, dict) else None
