"""The opt-in audit log: one JSONL entry per invocation, bounded on disk (REQ-O-030).

The log is off until the app passes ``App(audit_log=AuditLog())`` or the operator sets
``<APP>_AUDIT_LOG`` to ``1`` or an absolute path; ``0`` turns it off whatever the app
says. While it is off nothing is created. Every command a run resolves appends one line
to ``audit.jsonl``: when, which command, its arguments with secrets redacted, the exit
code, the duration, the warning codes, and the request, trace, and session ids. A line
never exceeds 16 KiB: the largest ``args`` values become ``[TRUNCATED]`` until it fits.

The file is append-only. When the next line would take it past ``max_bytes``, or its
first entry is older than ``max_age_days``, it becomes ``audit.1.jsonl`` and the older
ones shift up to ``audit.<keep>.jsonl``, the oldest dropped; rotated files not written
for ``max_age_days`` are deleted before each append. The log therefore never holds more
than ``(keep + 1) * max_bytes`` plus one entry. ``_audit.py`` is the static linter; this
module is the log.
"""

from __future__ import annotations

import datetime as dt
import errno
import json
import os
import re
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import IO

from ._atomic import exclusive, retry_sharing_violation
from ._env import AUDIT_LOG, app_var
from ._errors import ParseError, RegistrationError

AUDIT_LOG_UNAVAILABLE = "AUDIT_LOG_UNAVAILABLE"
INVALID_AUDIT_LOG_SETTING = "INVALID_AUDIT_LOG_SETTING"
FILE_NAME = "audit.jsonl"
ON = "1"
OFF = "0"
MAX_ENTRY_BYTES = 16 * 1024
"""The longest line, newline included: larger ``args`` values are ``[TRUNCATED]``"""
TRUNCATED = "[TRUNCATED]"
_DAY = 86_400
_SHARING_VIOLATION = 32
"""Windows ``ERROR_SHARING_VIOLATION``: another run has the file open, so it cannot move"""


@dataclass(frozen=True, slots=True)
class AuditLog:
    """Turns the audit log on for the app, and sets where it goes and how much of it is
    kept. ``path`` None is ``$XDG_STATE_HOME/<app>/audit.jsonl``, else
    ``~/.local/state/<app>/audit.jsonl``. ``<APP>_AUDIT_LOG`` wins: ``0`` turns it off,
    an absolute path moves it. The same bounds apply when the operator turns on the log of
    an app that passed none."""

    path: Path | str | None = None
    max_bytes: int = 10 * 2**20
    """Size at which the file is rotated"""
    keep: int = 5
    """Rotated files kept besides the live one"""
    max_age_days: int = 30
    """Entries older than this are rotated out, and rotated files not written for this
    long are deleted"""

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
"""The bounds of a log the operator turns on for an app that configured none"""


@dataclass(frozen=True, slots=True)
class Setting:
    """Whether this run keeps the log, and where"""

    enabled: bool
    path: Path | None
    """The file; None when the log is off, or on with no home to put it in"""
    bounds: AuditLog


def resolve(settings: AuditLog | None, app_name: str, env: Mapping[str, str]) -> Setting:
    """The run's audit log: ``<APP>_AUDIT_LOG`` over the app's ``settings``. Any value
    but ``1``, ``0``, and an absolute path exits 2 with ``INVALID_AUDIT_LOG_SETTING``."""
    variable = app_var(app_name, AUDIT_LOG.key)
    raw = env.get(variable)
    bounds = settings if settings is not None else DEFAULT_AUDIT_LOG
    if raw is None:
        enabled = settings is not None
    elif raw == OFF:
        return Setting(False, None, bounds)
    elif raw == ON:
        enabled = True
    elif Path(raw).is_absolute():
        return Setting(True, Path(raw), bounds)
    else:
        raise ParseError(
            f"{variable} is 1, 0, or an absolute file path, not {raw[:64]!r}",
            code=INVALID_AUDIT_LOG_SETTING,
            context={
                "variable": variable,
                "value": raw[:256],
                "accepted": ["1", "0", "an absolute path"],
            },
            suggestion=f"set {variable} to 1 to turn the audit log on, 0 to turn it off, or "
            f"an absolute path such as /var/log/{app_name}/audit.jsonl; or unset it",
        )
    if not enabled:
        return Setting(False, None, bounds)
    if settings is not None and settings.path is not None:
        return Setting(True, Path(settings.path), bounds)
    return Setting(True, default_path(app_name, env), bounds)


def default_path(app_name: str, env: Mapping[str, str]) -> Path | None:
    """``$XDG_STATE_HOME/<app>/audit.jsonl``, else ``~/.local/state/<app>/audit.jsonl``;
    None without either"""
    xdg = env.get("XDG_STATE_HOME")
    if xdg and Path(xdg).is_absolute():  # a relative one is ignored, as the XDG spec says
        return Path(xdg) / app_name / FILE_NAME
    if home := env.get("HOME"):
        return Path(home) / ".local" / "state" / app_name / FILE_NAME
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


def encoded(entry: Mapping[str, object]) -> bytes:
    """``entry`` as one JSON line of at most ``MAX_ENTRY_BYTES``: while it is longer, the
    largest value of ``args`` becomes ``[TRUNCATED]`` and ``truncated`` is set. An entry
    still too long raises ``OSError``, as a write that failed would"""
    record = dict(entry)
    line = _line(record)
    if len(line) <= MAX_ENTRY_BYTES:
        return line
    raw_args = record.get("args")
    args = dict(raw_args) if isinstance(raw_args, dict) else {}
    sizes = {k: len(json.dumps(v, separators=(",", ":"))) for k, v in args.items()}
    record["truncated"] = True
    for key in sorted(sizes, key=lambda k: sizes[k], reverse=True):
        if args[key] == TRUNCATED:
            continue
        args[key] = TRUNCATED
        record["args"] = args
        line = _line(record)
        if len(line) <= MAX_ENTRY_BYTES:
            return line
    # Only the ids and warning codes are left, so the entry cannot be written
    raise OSError(
        errno.EFBIG,
        f"the entry is {len(line)} bytes with every argument truncated, over {MAX_ENTRY_BYTES}",
    )


def _line(record: Mapping[str, object]) -> bytes:
    return (json.dumps(record, separators=(",", ":")) + "\n").encode()


def _make_dirs(folder: Path) -> None:
    """Create ``folder`` and its missing parents with mode 0700 whatever the umask; an
    existing directory keeps its mode"""
    missing: list[Path] = []
    while not folder.exists() and folder.parent != folder:
        missing.append(folder)
        folder = folder.parent
    for made in reversed(missing):
        try:
            made.mkdir(mode=0o700)
        except FileExistsError:
            continue  # another run made it first: its mode is not ours to change
        os.chmod(made, 0o700)


class Journal:
    """Appends entries to one audit log file; any ``OSError`` reaches the caller"""

    def __init__(self, path: Path, settings: AuditLog) -> None:
        self.path = path
        self.settings = settings
        self.lock_path = path.with_name(path.name + ".lock")
        """Held while files are rotated or pruned, so concurrent runs take turns"""

    def append(self, entry: Mapping[str, object]) -> None:
        line = encoded(entry)
        _make_dirs(self.path.parent)
        now = time.time()
        if self._first_entry_expired(now):
            self._rotate(len(line), now)
        self._prune(now)
        if self._size() + len(line) > self.settings.max_bytes:
            self._rotate(len(line), now)
        fd = self._open()
        try:
            os.write(fd, line)  # one write: a concurrent run never splits the line
        finally:
            os.close(fd)

    def _open(self) -> int:
        """The live file for appending; created 0600 whatever the umask, while an existing
        file keeps its mode"""
        flags = os.O_WRONLY | os.O_APPEND
        while True:
            try:
                fd = retry_sharing_violation(
                    lambda: os.open(self.path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                )
            except FileExistsError:
                pass
            else:
                os.fchmod(fd, 0o600)
                return fd
            try:
                return retry_sharing_violation(lambda: os.open(self.path, flags))
            except FileNotFoundError:
                continue  # another run rotated it away between the two opens

    def _size(self) -> int:
        try:
            return self.path.stat().st_size
        except FileNotFoundError:
            return 0

    def _first_entry_expired(self, now: float) -> bool:
        """Whether the live file's first entry is older than ``max_age_days``; a first line
        with no readable timestamp counts by the file's modification time"""
        try:
            handle: IO[bytes] = retry_sharing_violation(lambda: self.path.open("rb"))
        except FileNotFoundError:
            return False
        with handle:
            first = handle.readline(MAX_ENTRY_BYTES)
            written = os.fstat(handle.fileno()).st_mtime
        if not first.strip():
            return False
        cutoff = now - self.settings.max_age_days * _DAY
        try:
            parsed = json.loads(first)
        except ValueError:
            return written < cutoff
        when = entry_time(parsed) if isinstance(parsed, dict) else None
        stamp = written if when is None else when.timestamp()
        return stamp < cutoff

    def _rotate(self, incoming: int, now: float) -> None:
        """Shift ``audit.jsonl`` to ``audit.1.jsonl`` and each older file up by one, the
        one past ``keep`` dropped; under a lock, after a re-check, since another run may
        have rotated already. On Windows a file another run has open cannot move: the
        rotation then waits for the next append, and the shifts done so far stand, oldest
        first, so no entry is lost."""
        with exclusive(self.lock_path):
            size = self._size()
            full = size + incoming > self.settings.max_bytes
            if size == 0 or not (full or self._first_entry_expired(now)):
                return
            keep = self.settings.keep
            try:
                for index, stale in _numbered(self.path):
                    if index >= keep:
                        stale.unlink(missing_ok=True)
                for index in range(keep - 1, 0, -1):
                    older = rotated(self.path, index)
                    if older.exists():
                        os.replace(older, rotated(self.path, index + 1))
                os.replace(self.path, rotated(self.path, 1))
            except PermissionError as exc:
                if getattr(exc, "winerror", None) != _SHARING_VIOLATION:
                    raise

    def _prune(self, now: float) -> None:
        """Delete the rotated files not written for ``max_age_days``; under the rotation
        lock, since another run's rotation may rename a newer file into a slot between
        its stat and its unlink"""
        if not _numbered(self.path):
            return  # nothing rotated yet: no lock to take
        cutoff = now - self.settings.max_age_days * _DAY
        with exclusive(self.lock_path):
            for _, old in _numbered(self.path):
                try:
                    stale = old.stat().st_mtime < cutoff
                except FileNotFoundError:
                    continue  # pruned by a run that held the lock before this one
                if stale:
                    old.unlink(missing_ok=True)


_DURATION = re.compile(r"([1-9]\d*)([smhd])")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": _DAY}


def parse_since(raw: str, now: dt.datetime) -> dt.datetime:
    """``--since``: a positive duration back from ``now`` (``30s``, ``15m``, ``1h``,
    ``7d``) or an ISO 8601 time with a UTC offset"""
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
        when = None
    if when is None or when.tzinfo is None:
        raise ParseError(
            f"--since {raw[:64]!r} is neither a duration nor an ISO 8601 time with a UTC offset",
            context={"flag": "since", "value": raw[:64]},
            suggestion="pass a duration such as 30s, 15m, 1h, or 7d, or a time such as "
            "2026-01-31T12:00:00Z",
        )
    return when


def command_matches(entry_command: object, wanted: tuple[str, ...]) -> bool:
    """``--command``: the entry's command path is ``wanted`` or starts with it at a word
    boundary, so ``config`` matches ``config set`` but not ``configure``"""
    if not isinstance(entry_command, str):
        return False
    parts = tuple(re.split(r"[ .]", entry_command))
    return parts[: len(wanted)] == wanted


def command_words(raw: str) -> tuple[str, ...]:
    """A ``--command`` value as path words, space- or dot-separated"""
    return tuple(w for w in re.split(r"[ .]", raw) if w)


def entry_time(entry: Mapping[str, object]) -> dt.datetime | None:
    stamp = entry.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        when = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=dt.UTC)


def read_entries(path: Path) -> Iterator[dict[str, object] | None]:
    """Every entry, oldest first; None for a line that is not a JSON object, such as one a
    full disk cut short"""
    for file in log_files(path):
        try:
            handle: IO[str] = retry_sharing_violation(
                partial(open, file, encoding="utf-8", errors="replace")
            )
        except FileNotFoundError:
            continue  # another run rotated it away since the listing
        with handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    yield None
                    continue
                yield entry if isinstance(entry, dict) else None
