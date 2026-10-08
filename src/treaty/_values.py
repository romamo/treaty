"""Value objects shared across the package.

Every domain concept that would otherwise travel as a bare ``str`` or ``int``
has a frozen dataclass here that validates on construction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PATH_RE = re.compile(r"^[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*)*$")
_EXIT_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")
_INSTANCE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_PROTOCOL_RE = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")


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
class InstanceId:
    """``--instance-id``: one agent's namespace for the user config file and state"""

    value: str

    def __post_init__(self) -> None:
        if not _INSTANCE_ID_RE.fullmatch(self.value):
            raise InvalidValue(
                "an instance id is 1 to 64 letters, digits, '.', '_', or '-', "
                "starting with a letter or digit"
            )


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
class StdioProtocol:
    """The protocol a command serves on stdin and stdout instead of envelopes, in lowercase
    kebab-case, such as ``mcp-stdio`` (CommandEntry.protocol, REQ-C-032)"""

    value: str

    def __post_init__(self) -> None:
        if not _PROTOCOL_RE.fullmatch(self.value):
            raise InvalidValue(f"invalid protocol name {self.value!r}; use lowercase kebab-case")

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


# response-envelope.json ResponseMeta.tool_version
_SEMVER_RE = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?"
)
_SCHEMA_VERSION_RE = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)")
# A PEP 440 public version as packaging normalizes it, such as importlib.metadata reports
_N = r"(?:0|[1-9]\d*)"
_PEP440_RE = re.compile(rf"({_N}\.{_N}\.{_N})(?:(a|b|rc)({_N}))?(?:\.post({_N}))?(?:\.dev({_N}))?")
_PRE_LABELS = {"a": "alpha", "b": "beta", "rc": "rc"}


@dataclass(frozen=True, slots=True)
class ToolVersion:
    """Semver version of the tool, such as ``2.4.1``; ``meta.tool_version`` (REQ-F-023)"""

    value: str

    def __post_init__(self) -> None:
        if not _SEMVER_RE.fullmatch(self.value):
            raise InvalidValue(f"version {self.value!r} is not semver MAJOR.MINOR.PATCH")

    @classmethod
    def of_release(cls, text: str) -> ToolVersion:
        """A semver version, or the semver spelling of a PEP 440 public version as packaging
        normalizes it: a pre-release ``1.0.0rc1`` is ``1.0.0-rc.1``, with ``a`` and ``b``
        as ``alpha`` and ``beta`` so the order holds; a development release ``1.0.0.dev0``
        is ``1.0.0-dev.0``; a post release ``1.0.0.post1`` is ``1.0.0+post.1``, build
        metadata, since semver has no version after a release but before the next one.

        A ``.devN`` of a post release stays in the build metadata, ``1.0.0.post1.dev0`` as
        ``1.0.0+post.1.dev.0``, so it never sorts below ``1.0.0``. PEP 440 reads every
        spelling without ``+post`` back as the same version. Semver orders ``dev`` between
        ``beta`` and ``rc`` and a ``.devN`` of a pre-release after it, where PEP 440 puts
        development releases first; the spelling keeps the parts, not that order. Epochs
        (``1!1.0.0``) and local versions (``1.0.0rc1+abc``) have no semver spelling."""
        if (match := _PEP440_RE.fullmatch(text)) is not None:
            release, label, pre, post, dev = match.groups()
            pre_parts = [] if label is None else [_PRE_LABELS[label], pre]
            build_parts = [] if post is None else ["post", post]
            if dev is not None:
                (pre_parts if post is None else build_parts).extend(["dev", dev])
            spelled = release
            if pre_parts:
                spelled += "-" + ".".join(pre_parts)
            if build_parts:
                spelled += "+" + ".".join(build_parts)
            return cls(spelled)
        if _SEMVER_RE.fullmatch(text):
            return cls(text)
        if "!" in text:
            reason = "a PEP 440 epoch has no semver spelling"
        elif "+" in text and _PEP440_RE.fullmatch(text.partition("+")[0]):
            reason = "a PEP 440 local version has no semver spelling"
        else:
            reason = (
                "it is neither semver MAJOR.MINOR.PATCH nor a PEP 440 release with optional "
                "aN, bN, or rcN, .postN, and .devN parts"
            )
        raise InvalidValue(f"version {text!r}: {reason}")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class SchemaVersion:
    """``MAJOR.MINOR`` version of one command's output contract (REQ-F-022)"""

    value: str

    def __post_init__(self) -> None:
        if not _SCHEMA_VERSION_RE.fullmatch(self.value):
            raise InvalidValue(f"schema version {self.value!r} is not MAJOR.MINOR, such as 1.0")

    @property
    def major(self) -> int:
        return int(self.value.partition(".")[0])

    @property
    def minor(self) -> int:
        return int(self.value.partition(".")[2])

    @property
    def key(self) -> tuple[int, int]:
        """For ordering: 1.10 comes after 1.9"""
        return self.major, self.minor

    def __str__(self) -> str:
        return self.value
