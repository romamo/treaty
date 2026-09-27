"""One run's private temp directory, the files it hands the caller, and its children's
pid file (REQ-F-032, REQ-F-043, REQ-F-030).

The root is ``<temp>/<app>-<uid>/``, or ``instances/<id>/`` under it for an
``--instance-id``; every directory is ``0700`` and every file ``0600``, set explicitly so
a umask never widens them, and a root that is a symlink or someone else's is refused.
The session directory is ``<root>/<request id>/``, made on first use by ``ctx.tmp_dir``,
``ctx.temp_file()``, or a child of ``ctx.run``, and removed when the run ends. Files from
``ctx.output_file()`` outlive the run under ``<root>/out/<expiry>-<request id>-<n>/``; no
daemon removes them: each run prunes what expired, and session directories a killed run
left behind for a day.
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
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path

from ._errors import CliExit
from ._values import ExitCodeName, InstanceId

OUT_DIR = "out"
INSTANCES_DIR = "instances"
PID_FILE = "children.pids"
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
    for entry in os.scandir(root.path):
        if entry.name in (OUT_DIR, INSTANCES_DIR) or not entry.is_dir(follow_symlinks=False):
            continue
        if now - entry.stat(follow_symlinks=False).st_mtime > STALE_SESSION_SECONDS:
            due.append(entry.path)
    out = root.path / OUT_DIR
    if _owned_dir(out):
        for entry in os.scandir(out):
            expiry, dash, _ = entry.name.partition("-")
            if dash and expiry.isdigit() and int(expiry) <= now:
                due.append(entry.path)
    for path in due:
        try:
            _removed(Path(path))
        except OSError:
            continue


def outputs(root: Path) -> list[Path]:
    """Every directory of output files under ``root``, for the ``cleanup`` built-in"""
    out = root / OUT_DIR
    return sorted(Path(e.path) for e in os.scandir(out)) if out.is_dir() else []


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
                self._dir = private_dir(self.root.make() / self.request_id)
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

    def track(self, pids: Collection[int]) -> None:
        """Rewrite the pid file of the children running now (REQ-F-030); once the run
        ended, a late child of an abandoned handler is not written anywhere"""
        if self._removed:
            return
        path = self.directory() / PID_FILE
        text = "".join(f"{pid}\n" for pid in sorted(pids))
        partial = path.with_name(f".{PID_FILE}.{uuid.uuid4().hex}")
        private_file(partial).write_text(text, encoding="ascii")
        os.replace(partial, path)

    @property
    def pid_file(self) -> Path | None:
        return None if self._dir is None else self._dir / PID_FILE

    def remove(self) -> None:
        """The run ended: its session directory goes, output files stay until expiry"""
        with self._lock:
            self._removed = True
            made = self._dir
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
