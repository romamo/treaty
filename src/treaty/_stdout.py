"""What third-party code writes to file descriptor 1, kept off stdout (REQ-F-060).

``App.main()`` points descriptor 1 at a pipe and writes envelopes to a copy of the
original. A reader thread passes everything that reaches the pipe on to stderr and keeps
the first 4 KiB and a byte count; before an envelope is written, a marker sent through
the pipe makes sure nothing written earlier is still on its way. A C extension, a child
that inherited the descriptor, or an import-time ``print`` is caught the same way, the
last once ``intercept_stdout()`` runs before the app is imported.

What reaches stderr is cleaned as printed text is (#105, #117): colors (SGR) stay where
the run may color, every other escape goes, and other controls but tab, newline, and
carriage return are shown as escapes. Bytes that are not UTF-8 pass through unchanged.

It is redacted of every attached run's secrets as printed text is (#254), a line at a
time: a secret split across two writes, or two reads of the pipe, is whole by the end of
its line. What a line has without its end waits for the end, a carriage return too, for
the next envelope, or for the run's end; past ``LINE_CAP`` it is passed on redacted but for
its last ``LINE_TAIL`` characters.

As the process exits with a handler thread abandoned at its timeout still alive,
descriptor 1 stays off stdout, for that thread may still write a secret (#263). The reader
is a daemon thread and stops for good as the interpreter finalizes, so treaty's exit hook
turns descriptor 1 to a spool file and drains it itself (#271): as the last such thread
ends, when descriptor 1 is stdout again, or at the interpreter's last flush.
"""

from __future__ import annotations

import atexit
import codecs
import contextlib
import json
import os
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable

from ._envelope import open_escape, strip_escapes, terminal_text
from ._mode import color_allowed

TEXT_CAP = 4096
"""Bytes of captured text a ``THIRD_PARTY_STDOUT`` warning carries"""
SYNC_SECONDS = 2.0
"""The longest an envelope waits for text still in the pipe"""
LINE_CAP = 65536
"""Characters of a line without its end held for redaction; past them it is passed on"""
LINE_TAIL = 4096
"""Characters a line past ``LINE_CAP`` keeps back: a secret up to one more that the cut
splits is still whole for the next read"""
PIPE_BYTES = 1 << 20
"""The pipe's buffer where it can be set, Windows and Linux. As the interpreter finalizes,
the reader, a daemon thread, stops for good while a held thread keeps descriptor 1 a pipe
(#263); ``sys.stdout``'s last flush, up to ``io.DEFAULT_BUFFER_SIZE``, then fits in the
pipe rather than blocking the exit forever on a full one: Windows' default is 4 KiB (#268)"""


def _pipe() -> tuple[int, int]:
    """A pipe, its read and write descriptors, holding ``PIPE_BYTES`` where it can"""
    if sys.platform == "win32":
        import _winapi
        import msvcrt

        read, write = _winapi.CreatePipe(None, PIPE_BYTES)  # handles not inherited
        return (
            msvcrt.open_osfhandle(read, os.O_RDONLY | os.O_NOINHERIT),
            msvcrt.open_osfhandle(write, os.O_WRONLY | os.O_NOINHERIT),
        )
    read, write = os.pipe()
    if sys.platform == "linux":
        import fcntl

        # Unprivileged up to /proc/sys/fs/pipe-max-size, 1 MiB unless lowered; the default
        # 64 KiB pipe and the reader's last read still hold a 128 KiB flush when it was
        with contextlib.suppress(PermissionError):
            fcntl.fcntl(write, fcntl.F_SETPIPE_SZ, PIPE_BYTES)
    return read, write


def _spool() -> tuple[int, int]:
    """A file that stands in for the pipe once the reader stops at exit (#271): its write
    descriptor, appending, and a read descriptor of its own, so reading it never moves
    where a write lands. Unlike a pipe, a write to it never blocks for want of a reader.
    It is gone from the disk once both are closed, at the latest as the process exits"""
    made, path = tempfile.mkstemp(prefix="treaty-stdout-")
    os.close(made)
    # Windows: deleted as the last handle closes, each open sharing the delete
    gone = getattr(os, "O_TEMPORARY", 0)
    binary = getattr(os, "O_BINARY", 0)
    write = os.open(path, os.O_WRONLY | os.O_APPEND | gone | binary)
    read = os.open(path, os.O_RDONLY | gone | binary)
    if not gone:
        os.unlink(path)
    return write, read


def _unchanged(text: str) -> str:
    return text


_redact: Callable[[str], str] = _unchanged
"""The secrets of every attached run out of a line, as ``redact_with`` set it"""


def redact_with(redact: Callable[[str], str]) -> None:
    """What reaches stderr from descriptor 1 is redacted with ``redact`` from now on: the
    app module sets it to its runs' redaction, which this module cannot import"""
    global _redact
    _redact = redact


def _never() -> bool:
    return False


_held: Callable[[], bool] = _never
"""Whether a handler thread whose run detached still lives, as ``hold_open_while`` set it"""


def hold_open_while(held: Callable[[], bool]) -> None:
    """``close`` leaves descriptor 1 a pipe to stderr while ``held()``: a handler thread
    abandoned at its timeout may still write to it as the process exits, and only the
    reader redacts what it writes (#263). The app module sets it, as ``redact_with``"""
    global _held
    _held = held


class Interceptor:
    """Descriptor 1 as a pipe to stderr, read by a daemon thread; ``saved`` is the
    original stdout, where envelopes go"""

    def __init__(self) -> None:
        if sys.stdout is not None:
            sys.stdout.flush()
        self.saved = os.dup(1)
        self._marker = f"\0treaty-sync-{uuid.uuid4().hex}\0".encode()
        self.color = color_allowed(os.environ, os.isatty(self.saved))
        """Colors (SGR) stay on what is passed on to stderr, as on a ``ctx.log`` line;
        decided as the run decides, from the environment and whether stdout is a
        terminal"""
        self._decoder = codecs.getincrementaldecoder("utf-8")("surrogateescape")
        self._held = ""
        """The unfinished escape the last read ended with, until the next completes it"""
        self._lines = LineBuffer()
        """What was read since the last line end, held to be redacted whole"""
        read, write = _pipe()
        os.dup2(write, 1)
        os.close(write)
        self._read = read
        self._cond = threading.Condition()
        self._text = bytearray()
        self._bytes = 0
        self._synced = 0
        self._closed = False
        self._pipe: int | None = None
        """The pipe's write end while ``pause`` gave descriptor 1 back to stdout"""
        self._lingering: tuple[tuple[int, Callable[[str], str]], ...] = ()
        """The redaction of a ``sync`` that gave up, with its marker's number: applied
        until the reader passes that marker"""
        self.deferred = False
        """``close`` was called while a held handler thread lived; ``settle`` finishes it"""
        self._switch = threading.Lock()
        """Held while descriptor 1 changes, and while ``take`` writes its marker to it: a
        marker sent as ``pause`` or ``resume`` switches would reach stdout. The reader
        never takes it, so a marker blocked on a full pipe cannot deadlock"""
        self._order = threading.Lock()
        """Held while what was read is passed on, by the reader a read at a time and by a
        ``drain`` of the spool: one line buffer, one decoder"""
        self._spool: int | None = None
        """The read descriptor of the file descriptor 1 writes to once ``retire`` stopped
        the reader at exit, a held handler thread still alive (#271)"""
        self._thread = threading.Thread(target=self._pump, name="treaty-stdout", daemon=True)
        self._thread.start()

    def take(self, redact: Callable[[str], str] | None = None) -> tuple[str, int]:
        """The text (cut to ``TEXT_CAP`` bytes, Windows line endings as ``\\n``) and the byte
        count that reached descriptor 1 since the last call, once everything written before
        this call arrived; ``redact`` as ``sync`` has it"""
        if not self.sync(redact):
            return "", 0
        with self._cond:
            text, count = bytes(self._text), self._bytes
            self._text.clear()
            self._bytes = 0
        # Cut at TEXT_CAP bytes, the last character may be split: dropped, not replaced
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        decoded = decoder.decode(text, final=count <= len(text))
        return decoded.replace("\r\n", "\n"), count

    def sync(self, redact: Callable[[str], str] | None = None) -> bool:
        """Wait until everything written to descriptor 1 before this call reached stderr,
        redacted, its unfinished line too: called as an envelope is written and before a
        run detaches, while its secrets are still known. Past ``SYNC_SECONDS``, a reader
        held up on a full stderr, what is still in the pipe from before is redacted with
        ``redact`` too, the secrets the caller is about to forget. False when paused or
        closed"""
        with self._switch:
            with self._cond:
                if self._closed or self._pipe is not None:
                    return False  # paused: a marker would reach stdout
                spooled = self._spool is not None
                if spooled and redact is not None:
                    # Retired at exit: kept until the process is gone, for what is still
                    # on its way, in a stuck reader's pipe or in a write to the spool
                    self._lingering = (*self._lingering, (sys.maxsize, redact))
                target = self._synced + 1
            if not spooled:
                os.write(1, self._marker)  # below PIPE_BUF, so no other write splits it
        if spooled:
            self._drain()
            return True
        with self._cond:
            synced = self._cond.wait_for(lambda: self._synced >= target, timeout=SYNC_SECONDS)
            if not synced and redact is not None:
                self._lingering = (*self._lingering, (target, redact))
        return True

    def pause(self) -> None:
        """Descriptor 1 is stdout until ``resume``: a passthrough command's delegated tool
        owns stdout, its children and C code included (#35)"""
        with self._switch:
            with self._cond:
                if self._closed or self._pipe is not None:
                    return
                self._pipe = os.dup(1)
            os.dup2(self.saved, 1)

    def resume(self) -> None:
        """Descriptor 1 is the pipe to stderr again, after ``pause``"""
        with self._switch:
            with self._cond:
                pipe, self._pipe = self._pipe, None
            if pipe is None:
                return
            os.dup2(pipe, 1)
            os.close(pipe)

    def close(self) -> None:
        """Descriptor 1 is stdout again; the reader passes on what is left and stops. While
        a held handler thread lives, it stays a pipe to stderr instead, the redaction still
        installed: ``settle`` closes it once the last such thread ends, or the reader
        redacts until the process is gone (#263)"""
        self.resume()
        with self._cond:
            if self._closed:
                return
            held = self.deferred = _held()
            self._closed = not held
        if sys.__stdout__ is not None:
            sys.__stdout__.flush()  # an import-time print still in Python's buffer
        if held:
            # The reader is a daemon: what is in the pipe reaches stderr now, not lost as
            # the process exits. A thread that ended since ``_held`` found it settled
            # nothing, the close not yet put off: finish it here
            self.sync()
            if not _held():
                settle()
            return
        # The pipe's last write end, the reader sees its end; or the spool's, once retired
        os.dup2(self.saved, 1)
        if self._spool is not None:
            # Retired: the reader is gone, what the spool holds goes out here
            self._drain(final=True, close=True)
        else:
            self._thread.join(SYNC_SECONDS)
        os.close(self.saved)

    def retire(self) -> None:
        """At exit, a held handler thread still alive (#271): descriptor 1 turns from the
        pipe to a spool file, so what is written to it no longer needs the reader, a
        daemon thread that stops for good as the interpreter finalizes and would lose what
        it had not passed on. The reader passes on what the pipe still holds and ends;
        what reaches the spool after is passed on, redacted, by ``drain``: as a held
        thread ends, at ``close`` once the last one did, and at ``finish``, the
        interpreter's last flush. Descriptor 1 never leads to stdout while a held thread
        lives"""
        with self._switch:
            with self._cond:
                if self._closed or self._spool is not None or self._pipe is not None:
                    return
            write, read = _spool()
            with self._cond:
                self._spool = read
            os.dup2(write, 1)  # the pipe's last write end: the reader sees its end
            os.close(write)
        self._thread.join(SYNC_SECONDS)

    def finish(self) -> None:
        """What reached the spool since ``retire``, ``sys.__stdout__``'s buffer too, passed
        on to stderr redacted: called as the interpreter flushes the standard streams one
        last time, after every atexit hook, when no held thread can run Python any more.
        Every other thread is stopped for good then, the reader too: a lock one holds is
        never released, so none is waited for, and what the spool holds stays unread"""
        if not self._cond.acquire(blocking=False):
            return
        try:
            if self._closed or self._spool is None:
                return
        finally:
            self._cond.release()
        dunder = sys.__stdout__
        if dunder is not None and not dunder.closed:
            dunder.flush()
        self._drain(final=True, wait=False)

    def _drain(self, *, final: bool = False, close: bool = False, wait: bool = True) -> None:
        """What the spool holds, read to its end and passed on as the reader would, its
        unfinished line too; with ``close``, the spool is closed after. Past
        ``SYNC_SECONDS`` waiting for a reader that still passes on what the pipe held,
        stuck on a full stderr, it gives up: the exit is never blocked for it. Without
        ``wait``, it gives up at once"""
        if not self._order.acquire(timeout=SYNC_SECONDS if wait else 0):
            return
        try:
            spool = self._spool
            if spool is None:
                return
            while chunk := os.read(spool, 65536):
                self._pass_on(chunk)
            self._release(final=final)
            if close:
                self._spool = None
                os.close(spool)
        finally:
            self._order.release()

    def _pump(self) -> None:
        pending = b""
        while chunk := os.read(self._read, 65536):
            with self._order:
                pending = self._pump_chunk(pending + chunk)
        with self._order:
            self._pass_on(pending)
            self._release(final=True)
        os.close(self._read)

    def _pump_chunk(self, pending: bytes) -> bytes:
        """``pending`` passed on up to each marker, each marker counted; returns what may
        be the start of a marker still arriving, held back"""
        marker = self._marker
        while (at := pending.find(marker)) >= 0:
            self._pass_on(pending[:at])
            self._release()  # what was written before the envelope is out whole
            pending = pending[at + len(marker) :]
            with self._cond:
                self._synced += 1
                self._lingering = tuple(held for held in self._lingering if held[0] > self._synced)
                self._cond.notify_all()
        keep = next(
            (
                n
                for n in range(min(len(marker) - 1, len(pending)), 0, -1)
                if marker.startswith(pending[-n:])
            ),
            0,
        )
        self._pass_on(pending[: len(pending) - keep])
        return pending[len(pending) - keep :]

    def _pass_on(self, data: bytes) -> None:
        if not data:
            return
        # A character or an escape the read split waits for the rest of it
        text = self._held + self._decoder.decode(data)
        cut = open_escape(text)
        self._held = text[cut:]
        self._show(text[:cut])
        with self._cond:
            self._bytes += len(data)
            self._text += data[: max(TEXT_CAP - len(self._text), 0)]

    def _release(self, *, final: bool = False) -> None:
        """An unfinished escape and line still held, cleaned and redacted as they stand;
        with ``final``, a character the descriptor's last bytes left unfinished too"""
        held = self._held + self._decoder.decode(b"", final=final)
        self._held = ""
        self._show(held)
        self._emit(self._lines.drain(self._redaction()))

    def _show(self, text: str) -> None:
        self._emit(self._lines.add(text, self._redaction(), color=self.color))

    def _redaction(self) -> Callable[[str], str]:
        """Every attached run's secrets, and those of a ``sync`` that gave up"""
        lingering = self._lingering

        def redact(text: str) -> str:
            text = _redact(text)
            for _, more in lingering:
                text = more(text)
            return text

        return redact

    def _emit(self, shown: str) -> None:
        view = memoryview(shown.encode("utf-8", "surrogateescape"))
        while view:
            view = view[os.write(2, view) :]


class LineBuffer:
    """Cleaned text held a line at a time, so a secret split across two writes, or two reads
    of a pipe, is redacted whole (#254, #256). What a line has without its end waits for
    the end, a carriage return too, or for ``drain``; past ``LINE_CAP`` it is passed on
    redacted but for its last ``LINE_TAIL`` characters. Not thread-safe: its owner holds
    the writes in order.

    With ``clean=False`` the text is held and passed on as written, escapes and all, as
    ``App.call`` hands a host's own stream what a handler printed (#261); only the copy a
    secret is looked for in is cleaned, as a whole line, so an escape split across two
    writes is whole in it too"""

    def __init__(self, *, clean: bool = True) -> None:
        self._clean = clean
        self._line = ""
        """The cleaned text since the last line end, held to be redacted whole; as
        written, without ``clean``"""
        self._bare = ""
        """``_line`` without colors, where a color may split a secret; unused without
        ``clean``, where it is made from the whole line"""

    @property
    def held(self) -> bool:
        """Whether a line without its end is held"""
        return bool(self._line or self._bare)

    def add(self, text: str, redact: Callable[[str], str], *, color: bool) -> str:
        """``text`` cleaned as a ``ctx.log`` line is, colors only with ``color``, and held;
        returns what is ready, redacted: the text up to its last line end, and of a line
        past ``LINE_CAP`` all but its tail"""
        if not text:
            return ""
        if not self._clean:
            return self._add_raw(text, redact)
        # A carriage return stays: a progress line printed with "\r" rewrites itself
        shown = terminal_text(text, color=color, keep="\r", rewrite=True)
        bare = terminal_text(text, color=False, keep="\r", rewrite=True) if color else shown
        self._line += shown
        self._bare += bare
        line, bare = self._line, self._bare
        cut, cut_bare = _line_end(line), _line_end(bare)
        self._line, self._bare = line[cut:], bare[cut_bare:]
        ready = _redacted(line[:cut], bare[:cut_bare], redact)
        if len(self._line) > LINE_CAP:
            ready += self._overflow(redact, color=color)
        return ready

    def _add_raw(self, text: str, redact: Callable[[str], str]) -> str:
        """``add`` without ``clean``: the text as written, the lines it ends redacted"""
        line = self._line + text
        cut = _line_end(line)
        self._line = line[cut:]
        ready = _redacted(line[:cut], _bare(line[:cut]), redact)
        if len(self._line) > LINE_CAP:
            shown = _redacted(self._line, _bare(self._line), redact)
            keep = open_escape(shown[: len(shown) - LINE_TAIL])  # never cut in an escape
            self._line = shown[keep:]
            ready += shown[:keep]
        return ready

    def drain(self, redact: Callable[[str], str]) -> str:
        """The line held, redacted as it stands: an envelope is written next, or the run's
        secrets are about to be forgotten"""
        line, bare = self._line, self._bare
        self._line = self._bare = ""
        return _redacted(line, bare if self._clean else _bare(line), redact)

    def _overflow(self, redact: Callable[[str], str], *, color: bool) -> str:
        """A line past ``LINE_CAP`` passed on redacted but for its last ``LINE_TAIL``
        characters, kept redacted for the next write: the start of a secret the next write
        finishes is there, and a secret already whole is gone from it"""
        shown = _redacted(self._line, self._bare, redact)
        cut = open_escape(shown[: len(shown) - LINE_TAIL])  # a color is never cut in two
        self._line = shown[cut:]
        self._bare = strip_escapes(self._line) if color else self._line
        return shown[:cut]


def _redacted(line: str, bare: str, redact: Callable[[str], str]) -> str:
    """``line`` redacted, or ``bare``, its text without colors, when that finds a secret:
    one a color splits, such as ``hun\\x1b[0mter2``, is whole once the colors are gone"""
    if not line and not bare:
        return ""
    redacted = redact(bare)
    return redacted if redacted != bare or line == bare else redact(line)


def _bare(text: str) -> str:
    """``text`` without escapes, its controls shown as a log line has them, but for a
    carriage return: where a secret a color splits is whole"""
    return terminal_text(text, color=False, keep="\r", rewrite=True)


def _line_end(text: str) -> int:
    """Where the text after the last line end, a newline or a carriage return, starts"""
    return max(text.rfind("\n"), text.rfind("\r")) + 1


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
        _active.deferred = False  # adopted again: a run owns it until its own close
        return _active


def settle() -> None:
    """Finish the ``close`` that a held handler thread put off, once no such thread
    lives: called as one ends"""
    with _active_lock:
        found = _active
        if found is None or not found.deferred or _held():
            return
        found.deferred = False
    found.close()


def finish() -> None:
    """The interpreter's last flush of the standard streams, past every atexit hook: what
    descriptor 1 wrote since a held handler thread kept it from stdout at exit reaches
    stderr, redacted, rather than nowhere (#271). A ``sys.stdout`` or ``sys.stderr``
    stand-in calls it from its ``flush`` once the interpreter is finalizing"""
    found = _active
    if found is not None:
        found.finish()


def _at_exit() -> None:
    """Treaty's exit hook. Registered as this module is imported, it runs after every exit
    hook registered once treaty was imported, a host's before ``App.main()`` too:
    descriptor 1 is stdout again, or while a held handler thread lives, a spool the
    reader is not needed for (#271)"""
    found = active()
    if found is None:
        return
    found.close()
    if found.deferred:
        found.retire()


atexit.register(_at_exit)


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
