"""Value objects shared across the package.

Every domain concept that would otherwise travel as a bare ``str`` or ``int``
has a frozen dataclass here that validates on construction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PATH_RE = re.compile(r"^[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*)*$")
_EXIT_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")


class InvalidValue(ValueError):
    """A value object received input that does not satisfy its invariant"""


@dataclass(frozen=True, slots=True)
class CommandPath:
    """Dot-separated command path such as ``deploy.rollback``"""

    value: str

    def __post_init__(self) -> None:
        if not _PATH_RE.fullmatch(self.value):
            raise InvalidValue(f"invalid command path {self.value!r}")

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(self.value.split("."))

    @property
    def parent(self) -> CommandPath | None:
        head, sep, _ = self.value.rpartition(".")
        return CommandPath(head) if sep else None

    def child(self, name: str) -> CommandPath:
        return CommandPath(f"{self.value}.{name}")

    def is_direct_child_of(self, other: CommandPath) -> bool:
        return self.parent == other

    def is_ancestor_of(self, other: CommandPath) -> bool:
        """``db`` of ``db.migrate.up``; a path is not its own ancestor"""
        return len(other.parts) > len(self.parts) and other.parts[: len(self.parts)] == self.parts

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ExitCodeName:
    """Named exit code constant such as ``CONFLICT``"""

    value: str

    def __post_init__(self) -> None:
        if not _EXIT_NAME_RE.fullmatch(self.value):
            raise InvalidValue(f"invalid exit code name {self.value!r}")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ExitCode:
    """Process exit code in the range 0 to 255"""

    value: int

    def __post_init__(self) -> None:
        if not 0 <= self.value <= 255:
            raise InvalidValue(f"exit code {self.value} outside 0..255")

    @property
    def is_framework(self) -> bool:
        return 0 <= self.value <= 13

    @property
    def is_command_specific(self) -> bool:
        return 79 <= self.value <= 125

    @property
    def is_signal(self) -> bool:
        return self.value in (130, 141, 143)


@dataclass(frozen=True, slots=True)
class Scope:
    """Permission string a command requires from the active credential"""

    value: str

    def __post_init__(self) -> None:
        if not self.value or self.value != self.value.strip():
            raise InvalidValue(f"invalid scope {self.value!r}")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Etag:
    """Stable content hash of a manifest"""

    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise InvalidValue("etag must not be empty")

    def __str__(self) -> str:
        return self.value
