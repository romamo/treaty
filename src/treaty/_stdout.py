"""What third-party code writes to file descriptor 1, kept off stdout (REQ-F-060).

``App.main()`` points descriptor 1 at a pipe and writes envelopes to a copy of the
original. A reader thread passes everything that reaches the pipe on to stderr and keeps
the first 4 KiB and a byte count; before an envelope is written, a marker sent through
the pipe makes sure nothing written earlier is still on its way. A C extension, a child
that inherited the descriptor, or an import-time ``print`` is caught the same way, the
last once ``intercept_stdout()`` runs before the app is imported.
"""

from __future__ import annotations

import atexit
import json
import os
import sys
import threading
import uuid

TEXT_CAP = 4096
"""Bytes of captured text a ``THIRD_PARTY_STDOUT`` warning carries"""
SYNC_SECONDS = 2.0
"""The longest an envelope waits for text still in the pipe"""


class Interceptor:
    """Descriptor 1 as a pipe to stderr, read by a daemon thread; ``saved`` is the
    original stdout, where envelopes go"""

    def __init__(self) -> None:
        if sys.stdout is not None:
            sys.stdout.flush()
        self.saved = os.dup(1)
        self._marker = f"\0treaty-sync-{uuid.uuid4().hex}\0".encode()
        read, write = os.pipe()
        os.dup2(write, 1)
        os.close(write)
        self._read = read
        self._cond = threading.Condition()
        self._text = bytearray()
        self._bytes = 0
        self._synced = 0
        self._closed = False
        self._thread = threading.Thread(target=self._pump, name="treaty-stdout", daemon=True)
        self._thread.start()
        atexit.register(self.close)

    def take(self) -> tuple[str, int]:
        """The text (cut to ``TEXT_CAP`` bytes) and the byte count that reached descriptor
        1 since the last call, once everything written before this call arrived"""
        with self._cond:
            if self._closed:
                return "", 0
            target = self._synced + 1
        os.write(1, self._marker)  # below PIPE_BUF, so no other write splits it
        with self._cond:
            self._cond.wait_for(lambda: self._synced >= target, timeout=SYNC_SECONDS)
            text, count = bytes(self._text), self._bytes
            self._text.clear()
            self._bytes = 0
        return text.decode("utf-8", "replace"), count

    def close(self) -> None:
        """Descriptor 1 is stdout again; the reader passes on what is left and stops"""
        with self._cond:
            if self._closed:
                return
            self._closed = True
        if sys.__stdout__ is not None:
            sys.__stdout__.flush()  # an import-time print still in Python's buffer
        os.dup2(self.saved, 1)  # the pipe's last write end: the reader sees its end
        self._thread.join(SYNC_SECONDS)
        os.close(self.saved)

    def _pump(self) -> None:
        pending = b""
        marker = self._marker
        while chunk := os.read(self._read, 65536):
            pending += chunk
            while (at := pending.find(marker)) >= 0:
                self._pass_on(pending[:at])
                pending = pending[at + len(marker) :]
                with self._cond:
                    self._synced += 1
                    self._cond.notify_all()
            # Hold back what may be the start of a marker still arriving
            keep = next(
                (
                    n
                    for n in range(min(len(marker) - 1, len(pending)), 0, -1)
                    if marker.startswith(pending[-n:])
                ),
                0,
            )
            self._pass_on(pending[: len(pending) - keep])
            pending = pending[len(pending) - keep :]
        self._pass_on(pending)
        os.close(self._read)

    def _pass_on(self, data: bytes) -> None:
        if not data:
            return
        view = memoryview(data)
        while view:
            view = view[os.write(2, view) :]
        with self._cond:
            self._bytes += len(data)
            self._text += data[: max(TEXT_CAP - len(self._text), 0)]


_active: Interceptor | None = None
_active_lock = threading.Lock()


def intercept_stdout() -> Interceptor:
    """Catch writes to descriptor 1 from now on, below Python: call it in the entry
    module before importing the app, so an import-time ``print`` cannot reach stdout
    (09-D3). ``App.main()`` adopts it, or installs it when this was not called."""
    global _active
    with _active_lock:
        if _active is None or _active._closed:
            _active = Interceptor()
        return _active


def active() -> Interceptor | None:
    """The installed interceptor, while it is"""
    found = _active
    return None if found is None or found._closed else found


def prose(text: str) -> str:
    """``text`` without its lines that are a JSON object or array: output meant for
    stdout, reported nowhere so it is never seen twice (REQ-F-060)"""
    return "".join(line for line in text.splitlines(keepends=True) if not _json_line(line))


def _json_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped[0] not in "{[":
        return False
    try:
        json.loads(stripped)
    except json.JSONDecodeError:
        return False
    return True
