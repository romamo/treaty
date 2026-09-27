"""``ctx.walk``: a directory walk that cannot loop or run away in depth (REQ-F-061).

Only ``recursive_traversal=True`` commands get it, with ``--no-follow-symlinks`` and
``--max-depth N`` (REQ-O-040). The walk is depth first over ``os.scandir`` with an
explicit stack, entries in name order. A followed symlink that leads back to a directory
on the current path exits 4 ``SYMLINK_LOOP``; two links to one directory are not a loop
(10-D1). An entry deeper than ``--max-depth`` exits 4 ``DEPTH_EXCEEDED`` rather than
being left out, so a delete or copy never reports success on part of a tree (10-D2).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ._errors import CliExit, ParseError
from ._page import whole_number
from ._values import ExitCodeName

NO_FOLLOW_FLAG = "no-follow-symlinks"
MAX_DEPTH_FLAG = "max-depth"
DEFAULT_MAX_DEPTH = 50
_LIMIT = 10_000
DEPTH_HINT = f"Use --{MAX_DEPTH_FLAG} to adjust the limit"


def parse_max_depth(raw: object) -> int:
    """``--max-depth``: a whole number of directory levels, at least 1"""
    expects = f"a whole number of directory levels from 1 to {_LIMIT}"
    if isinstance(raw, int) and not isinstance(raw, bool):
        raw = str(raw)
    if not isinstance(raw, str):
        raise ParseError(f"'{MAX_DEPTH_FLAG}' expects {expects}", context={"flag": MAX_DEPTH_FLAG})
    value = whole_number(raw, MAX_DEPTH_FLAG, expects)
    if not 1 <= value <= _LIMIT:
        raise ParseError(
            f"'{MAX_DEPTH_FLAG}' expects {expects}",
            context={"flag": MAX_DEPTH_FLAG, "value": value},
        )
    return value


@dataclass(frozen=True, slots=True)
class WalkEntry:
    """One entry under the root; ``depth`` 1 is the root's own entries"""

    path: Path
    depth: int
    is_dir: bool
    """A directory the walk enters: a symlink to one only when symlinks are followed"""
    is_symlink: bool


class TraversalStopped(CliExit):
    """``SYMLINK_LOOP`` or ``DEPTH_EXCEEDED``: exit 4, with the flag that avoids it as
    ``error.hint``"""

    def __init__(self, message: str, *, code: str, context: dict[str, object], hint: str) -> None:
        super().__init__(ExitCodeName("PRECONDITION"), message, code=code, context=context)
        self.hint = hint


class Walk:
    """Iterate once for every entry under ``root``; ``count`` and ``symlinks_skipped``
    are the entries yielded and the symlinks yielded but not entered"""

    def __init__(self, root: Path, *, follow_symlinks: bool, max_depth: int) -> None:
        self.root = root
        self.follow_symlinks = follow_symlinks
        self.max_depth = max_depth
        self.count = 0
        self.symlinks_skipped = 0

    def __iter__(self) -> Iterator[WalkEntry]:
        top = os.stat(self.root)
        ancestors = {(top.st_dev, top.st_ino): self.root}
        # Each frame: a directory's remaining entries, their depth, and the directories
        # on the path to it, by (device, inode)
        stack = [(iter(self._entries(self.root, 1)), 1, ancestors)]
        while stack:
            entries, depth, above = stack[-1]
            entry = next(entries, None)
            if entry is None:
                stack.pop()
                continue
            path = Path(entry.path)
            link = entry.is_symlink()
            enter = entry.is_dir(follow_symlinks=self.follow_symlinks)
            if link and not self.follow_symlinks:
                self.symlinks_skipped += 1
            if enter:
                st = os.stat(path)
                key = (st.st_dev, st.st_ino)
                if key in above:
                    raise TraversalStopped(
                        f"Circular symlink: {path} leads back to {above[key]}",
                        code="SYMLINK_LOOP",
                        context={
                            "path": path,
                            "loop_target": above[key],
                            "completed_count": self.count,
                        },
                        hint=f"Use --{NO_FOLLOW_FLAG} to walk without entering symlinks",
                    )
            self.count += 1
            yield WalkEntry(path, depth, enter, link)
            if enter:
                frame = (iter(self._entries(path, depth + 1)), depth + 1, {**above, key: path})
                stack.append(frame)

    def _entries(self, directory: Path, depth: int) -> list[os.DirEntry[str]]:
        with os.scandir(directory) as it:
            entries = sorted(it, key=lambda e: e.name)
        if entries and depth > self.max_depth:
            raise TraversalStopped(
                f"Traversal depth limit of {self.max_depth} exceeded at {entries[0].path}",
                code="DEPTH_EXCEEDED",
                context={"max_depth": self.max_depth, "path": Path(entries[0].path)},
                hint=DEPTH_HINT,
            )
        return entries


@dataclass(frozen=True, slots=True)
class Traversal:
    """A ``recursive_traversal`` run's ``--no-follow-symlinks`` and ``--max-depth``"""

    follow_symlinks: bool = True
    max_depth: int = DEFAULT_MAX_DEPTH

    def walk(self, root: Path) -> Walk:
        return Walk(root, follow_symlinks=self.follow_symlinks, max_depth=self.max_depth)
