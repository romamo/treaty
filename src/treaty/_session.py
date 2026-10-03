"""One run's private temp directory, the files it hands the caller, and its children's
pid file (REQ-F-032, REQ-F-043, REQ-F-030).

The root is ``<temp>/<app>-<uid>/``, or ``instances/<id>/`` under it for an
``--instance-id``; every directory is ``0700`` and every file ``0600``, set explicitly so
a umask never widens them, and a root that is a symlink or someone else's is refused.
The session directory is ``<root>/<request id>/``, made on first use by ``ctx.tmp_dir``,
``ctx.temp_file()``, or a child of ``ctx.run``, and removed when the run ends. Files from
``ctx.output_file()`` outlive the run under ``<root>/out/<expiry>-<request id>-<n>/``; no
daemon removes them: each run prunes what expired, and session directories a killed run
left behind for a day. A run holds a lock on its session directory's ``.live`` file until
it ends, so a long one is never pruned from under it; the kernel drops the lock of a run
that was killed.
"""

from __future__ import annotations

import math
import os
import shlex
import shutil
import stat
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from ._atomic import retry_sharing_violation, try_lock
from ._errors import CliExit
from ._values import ExitCodeName, InstanceId

OUT_DIR = "out"
INSTANCES_DIR = "instances"
BACKGROUND_DIR = "background"
"""``ctx.spawn``'s pid files when the app has no state directory; never pruned"""
PID_FILE = "children.pids"
LIVE_FILE = ".live"
"""Locked by the run for as long as it lasts"""
STALE_SESSION_SECONDS = 86_400
"""A session directory this old belongs to a run that was killed; the next run removes it"""
DEFAULT_KEEP_SECONDS = 300
TEMP_DIR_UNSAFE = "TEMP_DIR_UNSAFE"


@dataclass(frozen=True, slots=True)
class SessionRoot:
    """Where an app's sessions live: ``<temp>/<app>-<uid>/``, and under it
    ``instances/<id>/`` for an ``--instance-id`` (REQ-O-036)"""

    user: Path
    instance: InstanceId | None = None

    @classmethod
    def of(cls, app_name: str, env: Mapping[str, str], instance: InstanceId | None) -> SessionRoot:
        """The run's temp directory (``TMPDIR``, ``TEMP``, ``TMP``, else the system's),
        one directory per user"""
        base = env.get("TMPDIR") or env.get("TEMP") or env.get("TMP") or tempfile.gettempdir()
        user = f"-{os.getuid()}" if hasattr(os, "getuid") else ""
        return cls(Path(base).absolute() / f"{app_name}{user}", instance)

    @property
    def path(self) -> Path:
        if self.instance is None:
            return self.user
        return self.user / INSTANCES_DIR / self.instance.value

    def owned(self) -> bool:
        """Whether every directory of the root exists and passes ``private_dir``'s check,
        without making any: pruning must never follow a planted root"""
        parts = [self.user]
        if self.instance is not None:
            parts += [self.user / INSTANCES_DIR, self.path]
        return all(_owned_dir(p) for p in parts)

    def make(self) -> Path:
        """The root, each directory of it private"""
        path = private_dir(self.user)
        if self.instance is not None:
            path = private_dir(private_dir(path / INSTANCES_DIR) / self.instance.value)
        return path


def _owned_dir(path: Path) -> bool:
    """A directory, not a symlink, owned by the user running this"""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    owner = getattr(os, "getuid", None)
    return stat.S_ISDIR(info.st_mode) and (owner is None or info.st_uid == owner())


def private_dir(path: Path) -> Path:
    """``path`` as a directory only its owner reads: made ``0700``, and refused when it is
    a symlink, not a directory, or owned by another user (a planted directory)"""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    if not _owned_dir(path):
        raise CliExit(
            ExitCodeName("PRECONDITION"),
            f"The temp directory {path} is a symlink, not a directory, or not the user's",
            code=TEMP_DIR_UNSAFE,
            context={"path": str(path)},
            fix_required=f"remove {path}, or point TMPDIR at a directory only you can write",
        )
    os.chmod(path, 0o700)
    return path


def private_file(path: Path) -> Path:
    """A new, empty ``0600`` file; an existing one is never reused"""
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    return path


def _removed(path: Path) -> None:
    """Remove a tree another run may be removing too"""

    def gone(function: object, name: str, exc: BaseException) -> None:
        if not isinstance(exc, FileNotFoundError):
            raise exc

    shutil.rmtree(path, onexc=gone)


def prune(root: SessionRoot, now: float) -> None:
    """Output files past their expiry, and session directories a killed run left; no
    daemon does this, so each run does it once (REQ-F-043). Only under a root that is the
    user's own, and best effort: what cannot be removed now waits for a later run."""
    if not root.owned():
        return
    due: list[str] = []
    for entry in listed(root.path):
        kept = (OUT_DIR, INSTANCES_DIR, BACKGROUND_DIR)
        if entry.name in kept or not entry.is_dir(follow_symlinks=False):
            continue
        mtime = modified(entry)
        stale = mtime is not None and now - mtime > STALE_SESSION_SECONDS
        if stale and not _live(Path(entry.path)):
            due.append(entry.path)
    out = root.path / OUT_DIR
    if _owned_dir(out):
        for entry in listed(out):
            expiry, dash, _ = entry.name.partition("-")
            if dash and expiry.isdigit() and int(expiry) <= now:
                due.append(entry.path)
    for path in due:
        try:
            _removed(Path(path))
        except OSError:
            continue


def listed(directory: Path) -> list[os.DirEntry[str]]:
    """The entries of ``directory``; none once another run has removed it"""
    try:
        with os.scandir(directory) as it:
            return list(it)
    except FileNotFoundError:
        return []


def modified(entry: os.DirEntry[str]) -> float | None:
    """The modification time of a listed entry, symlinks not followed; None once another
    run has removed it since the listing"""
    try:
        return entry.stat(follow_symlinks=False).st_mtime
    except FileNotFoundError:
        return None


def _live(session: Path) -> bool:
    """Whether a run still holds the session directory's ``.live`` lock"""
    try:
        fd = os.open(session / LIVE_FILE, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return False  # a directory from before 1.0.0: its age decides
    with os.fdopen(fd, "w") as handle:
        return not try_lock(handle)  # closing the handle releases a lock it took


def outputs(root: SessionRoot) -> list[Path]:
    """Every directory of output files under ``root``, for the ``cleanup`` built-in; like
    ``prune``, nothing under a root or ``out`` that is a symlink or someone else's"""
    out = root.path / OUT_DIR
    if not (root.owned() and _owned_dir(out)):
        return []
    return sorted(Path(e.path) for e in listed(out) if e.is_dir(follow_symlinks=False))


def cleanup_command(paths: Collection[Path]) -> str:
    """The shell command that deletes ``paths``, quoted for the platform's shell"""
    if sys.platform == "win32":
        return "del /f /q " + " ".join(f'"{p}"' for p in paths)
    return shlex.join(["rm", "-f", *(str(p) for p in paths)])


class Session:
    """One run's temp directory, its output files, and its children's pid file"""

    def __init__(self, root: SessionRoot, request_id: str) -> None:
        self.root = root
        self.request_id = request_id
        self._dir: Path | None = None
        self._removed = False
        self._outputs: list[tuple[Path, int]] = []
        self._out_id = uuid.uuid4().hex[:8]
        """Tells apart the output files of ``exec`` lines, which share a request id"""
        self._lock = threading.Lock()
        self._pids_lock = threading.Lock()
        """Serialises the rewrites of the pid file: snapshot, write, rename"""
        self._live: IO[str] | None = None

    @property
    def made(self) -> Path | None:
        """The session directory, once something used it, even after the run removed
        it; ``meta.session_tmp_dir``"""
        return self._dir

    def directory(self) -> Path:
        with self._lock:
            if self._removed:
                raise RuntimeError("the run ended, and its temp directory with it")
            if self._dir is None:
                made = private_dir(self.root.make() / self.request_id)
                live = os.fdopen(os.open(made / LIVE_FILE, os.O_WRONLY | os.O_CREAT, 0o600), "w")
                if not try_lock(live):
                    live.close()
                    raise RuntimeError(f"another run holds {made / LIVE_FILE}")
                self._dir, self._live = made, live
            return self._dir

    def temp_file(self, suffix: str = "") -> Path:
        _plain_name(f"tmp{suffix}", "suffix")
        return private_file(self.directory() / f"tmp-{uuid.uuid4().hex}{suffix}")

    def output_file(self, name: str, keep_seconds: int) -> Path:
        _plain_name(name, "name")
        if isinstance(keep_seconds, bool) or not isinstance(keep_seconds, int) or keep_seconds < 0:
            raise ValueError("keep_seconds is a whole number of seconds")
        expiry = math.ceil(time.time() + keep_seconds)  # never removed before it was due
        with self._lock:
            out = private_dir(self.root.make() / OUT_DIR)
            where = private_dir(out / f"{expiry}-{self.request_id}-{self._out_id}")
            path = private_file(where / name)
            self._outputs.append((path, keep_seconds))
        return path

    def cleanup(self) -> dict[str, object] | None:
        """``data.cleanup`` for the files ``output_file`` handed out (REQ-F-043)"""
        with self._lock:
            outputs = list(self._outputs)
        if not outputs:
            return None
        return {
            "command": cleanup_command([p for p, _ in outputs]),
            "auto_cleanup_after_seconds": max(keep for _, keep in outputs),
        }

    def child_env(self) -> dict[str, str]:
        """``TMPDIR``, ``TEMP``, and ``TMP`` for a child: its temp files are the run's"""
        where = str(self.directory())
        return {"TMPDIR": where, "TEMP": where, "TMP": where}

    def track(
        self,
        pids: Callable[[], Collection[int]],
        *,
        replace: Callable[[Callable[[], None]], None] = retry_sharing_violation,
    ) -> None:
        """Rewrite the pid file of the children running now (REQ-F-030); once the run
        ended, a late child of an abandoned handler is not written anywhere.

        ``pids()`` is read under the pid file's lock, so of two threads tracking at
        once the later write holds the later set, and their renames onto the one file
        never overlap: on Windows the second fails while the first is in flight. A
        reader of the file, another process's, still blocks the rename there for a
        moment, so ``replace`` retries it (``retry_sharing_violation``)."""
        if self._removed:
            return
        path = self.directory() / PID_FILE
        with self._pids_lock:
            text = "".join(f"{pid}\n" for pid in sorted(pids()))
            partial = path.with_name(f".{PID_FILE}.{uuid.uuid4().hex}")
            private_file(partial).write_text(text, encoding="ascii")
            try:
                replace(lambda: os.replace(partial, path))
            finally:
                partial.unlink(missing_ok=True)

    @property
    def pid_file(self) -> Path | None:
        return None if self._dir is None else self._dir / PID_FILE

    def remove(self) -> None:
        """The run ended: its session directory goes, output files stay until expiry"""
        with self._lock:
            self._removed = True
            made, live, self._live = self._dir, self._live, None
        if live is not None:
            live.close()  # before the removal: Windows keeps an open file
        if made is not None:
            _removed(made)


def _plain_name(name: str, what: str) -> None:
    if (
        not isinstance(name, str)
        or not name
        or name in (".", "..")
        or any(c in name for c in "/\\\0")
    ):
        raise ValueError(f"{what} is a plain file name, without a directory: {name!r}")
