"""Declarations an agent reads before it runs a command: which child binary gets which
argument (REQ-C-019), and the checks and manifest fields that go with them."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from ._errors import ParseError, RegistrationError
from ._flags import FieldInfo
from ._scan import EVERY_FIELD, CtxCall

# REQ-F-044: what a shell would act on, rejected in a declared user-controlled argument
_METACHARACTERS = re.compile(r"[;|&$()<>`\n\r]")
SHELL_METACHARACTER = "SHELL_METACHARACTER"


@dataclass(frozen=True, slots=True)
class Subprocess:
    """The child binary a command runs, the fields whose values become its arguments, and
    the arguments it always passes (REQ-C-019)

    Declared with ``subprocess=``, a user-controlled field is checked in phase 1: a value
    with a shell metacharacter or a leading ``-`` exits 2 with ``SHELL_METACHARACTER``.
    """

    binary: str
    user_controlled_args: tuple[str, ...] = ()
    """Field names of the args dataclass"""
    hardcoded_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.binary, str) or not self.binary.strip():
            raise RegistrationError("Subprocess(binary=) is the program name, such as 'git'")
        for name in ("user_controlled_args", "hardcoded_args"):
            value = getattr(self, name)
            if isinstance(value, str) or not all(isinstance(v, str) for v in value):
                raise RegistrationError(f"Subprocess({name}=) is a sequence of strings")
            object.__setattr__(self, name, tuple(value))

    def to_json(self, fields: Sequence[FieldInfo]) -> dict[str, object]:
        flags = {f.name: f.flag for f in fields}
        return {
            "binary": self.binary,
            "user_controlled_args": [flags[n] for n in self.user_controlled_args],
            "hardcoded_args": list(self.hardcoded_args),
        }


def check_subprocess(
    where: str, declared: Subprocess | None, fields: Sequence[FieldInfo]
) -> Subprocess | None:
    if declared is None:
        return None
    if not isinstance(declared, Subprocess):
        raise RegistrationError(f"{where}: subprocess takes treaty.Subprocess(...)")
    names = {f.name for f in fields}
    unknown = [n for n in declared.user_controlled_args if n not in names]
    if unknown:
        raise RegistrationError(
            f"{where}: Subprocess(user_controlled_args=) names {unknown}, which are not "
            f"fields of the args dataclass; it has {sorted(names)}"
        )
    controlled = declared.user_controlled_args
    objects = [f.name for f in fields if f.name in controlled and f.object_type is not None]
    if objects:
        raise RegistrationError(
            f"{where}: Subprocess(user_controlled_args=) names the object fields {objects}, "
            "whose text the shell-metacharacter check cannot see; pass the child a text field"
        )
    return declared


def derive_subprocess(calls: Sequence[CtxCall], fields: Sequence[FieldInfo]) -> Subprocess | None:
    """The declaration the handler's ``ctx.run([...])`` calls show, when every call runs
    one list literal starting with the same literal binary; None when any cannot be read.
    A field is user-controlled when an argument reads it, directly or through a local that
    holds its value, such as ``*extra`` after ``extra = list(args.extra)``"""
    runs = [c for c in calls if c.method in ("run", "pipeline")]
    if not runs or any(c.argv is None or not c.argv for c in runs):
        return None
    names = {f.name for f in fields}
    binaries: set[str] = set()
    user: dict[str, None] = {}
    hardcoded: dict[str, None] = {}
    for call in runs:
        assert call.argv is not None
        first, *rest = call.argv
        if first.literal is None:
            return None
        binaries.add(first.literal)
        for item in rest:
            if item.literal is not None:
                hardcoded[item.literal] = None
            if EVERY_FIELD in item.fields or any(f not in names for f in item.fields):
                # The arguments object passed whole, or a method or property of it such as
                # args.argv(): which fields reach the program is unknown
                return None
            # A local that holds a field's value carries it (ctx_calls follows it there)
            user.update(dict.fromkeys(f for f in item.fields if f in names))
    if len(binaries) != 1:
        return None
    return Subprocess(binaries.pop(), tuple(user), tuple(hardcoded))


def shell_safe(field: FieldInfo, value: object) -> ParseError | None:
    """A declared user-controlled value that a shell, or the child, would misread"""
    items = value if isinstance(value, (tuple, list)) else (value,)
    for item in items:
        if not isinstance(item, (str, os.PathLike)):
            continue
        text = os.fspath(item)
        found = _METACHARACTERS.search(text)
        if found is None and not text.startswith("-"):
            continue
        what = repr(found.group()) if found is not None else "a leading '-'"
        return ParseError(
            f"--{field.flag} holds {what}, which the command passes to a child program",
            code=SHELL_METACHARACTER,
            context={"field": field.flag, "character": found.group() if found else "-"},
            suggestion=f"pass --{field.flag} without ; | & $ ( ) < > ` or a line break, "
            "and not starting with -",
        )
    return None


# sys.platform values, freebsd and the like matched by prefix (freebsd14)
PLATFORMS = frozenset(
    {
        "aix",
        "android",
        "cygwin",
        "darwin",
        "emscripten",
        "freebsd",
        "ios",
        "linux",
        "netbsd",
        "openbsd",
        "sunos",
        "wasi",
        "win32",
    }
)
UNSUPPORTED_PLATFORM = "UNSUPPORTED_PLATFORM"


def check_platform(where: str, platform: Sequence[str]) -> tuple[str, ...]:
    """``platform=("linux", "darwin")``: ``sys.platform`` values; empty is every platform"""
    if isinstance(platform, str):
        raise RegistrationError(f"{where}: platform is a list, such as platform=[{platform!r}]")
    unknown = sorted(str(p) for p in platform if p not in PLATFORMS)
    if unknown:
        raise RegistrationError(
            f"{where}: platform {unknown} are not sys.platform values; use {sorted(PLATFORMS)}"
        )
    return tuple(dict.fromkeys(platform))


def supports(platform: Sequence[str], current: str) -> bool:
    return not platform or any(current.startswith(p) for p in platform)


class SideEffectType(StrEnum):
    CACHE = "cache"
    LOG = "log"
    TEMP = "temp"
    CREDENTIAL = "credential"
    CONFIG = "config"


@dataclass(frozen=True, slots=True)
class SideEffect:
    """A filesystem location a command writes (REQ-C-011)

    ``path`` is absolute or starts with ``~/``; ``{name}`` placeholders and ``*`` match any
    one path segment, so ``"/tmp/tool-{session}/"`` covers every session. ``type`` is
    ``cache``, ``log``, ``temp``, ``credential``, or ``config``; ``cleanup`` removes the
    ``temp``, ``cache``, and ``log`` ones, and ``status`` lists them all.
    ``clearable_with`` is the invocation that removes it, such as ``"tool cache clear"``,
    checked to name a command when the manifest is built.
    """

    path: str
    type: str
    ttl_seconds: int | None = None
    clearable_with: str | None = None

    def __post_init__(self) -> None:
        where = f"SideEffect({self.path!r})"
        if not isinstance(self.path, str) or not (
            self.path.startswith(("/", "~/")) or re.match(r"[A-Za-z]:[\\/]", self.path)
        ):
            raise RegistrationError(f"{where}: path is absolute or starts with ~/")
        segments = re.split(r"[\\/]", re.sub(r"^(~/|/|[A-Za-z]:[\\/])", "", self.path))
        if any(s in (".", "..") for s in segments):
            raise RegistrationError(f"{where}: path has no . or .. segments")
        if not re.sub(r"\{[^{}/]*\}|\*", "", segments[0]):
            # cleanup would remove all of / or the home directory
            raise RegistrationError(
                f"{where}: path names a directory under / or ~/, not every entry of either"
            )
        if self.type not in SideEffectType:
            kinds = ", ".join(t.value for t in SideEffectType)
            raise RegistrationError(f"{where}: type={self.type!r} is not one of {kinds}")
        ttl = self.ttl_seconds
        if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 0):
            raise RegistrationError(f"{where}: ttl_seconds is a whole number of seconds")
        if self.clearable_with is not None and not (
            isinstance(self.clearable_with, str) and self.clearable_with.strip()
        ):
            raise RegistrationError(f"{where}: clearable_with is an invocation of this tool")

    @property
    def kind(self) -> SideEffectType:
        return SideEffectType(self.type)

    def to_json(self) -> dict[str, object]:
        out: dict[str, object] = {"path": self.path, "type": self.type}
        if self.ttl_seconds is not None:
            out["ttl_seconds"] = self.ttl_seconds
        if self.clearable_with is not None:
            out["clearable_with"] = self.clearable_with
        return out

    def pattern(self, home: str | None) -> str | None:
        """The glob of every path it covers; None for ``~/`` without a home"""
        path = self.path
        if path.startswith("~/"):
            if not home:
                return None
            path = home.rstrip("/") + path[1:]
        return re.sub(r"\{[^{}/]*\}", "*", path).rstrip("/") or "/"


def check_side_effects(where: str, effects: Sequence[SideEffect]) -> tuple[SideEffect, ...]:
    if isinstance(effects, (str, SideEffect)) or not all(
        isinstance(e, SideEffect) for e in effects
    ):
        raise RegistrationError(
            f"{where}: filesystem_side_effects is a list of treaty.SideEffect(path, type)"
        )
    return tuple(effects)


@dataclass(frozen=True, slots=True)
class Background:
    """A command that starts a process outliving the run, through ``ctx.spawn`` (REQ-C-010)

    ``cleanup_command`` is the invocation that stops it, such as ``"tool stop-watcher"``,
    checked to name a command when the manifest is built; ``max_lifetime_seconds`` is how
    long it may run before a later ``ctx.spawn`` of the command stops it.
    """

    cleanup_command: str
    max_lifetime_seconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.cleanup_command, str) or not self.cleanup_command.strip():
            raise RegistrationError(
                "Background(cleanup_command=) is the invocation that stops the process, "
                "such as 'tool stop-watcher'"
            )
        seconds = self.max_lifetime_seconds
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 1:
            raise RegistrationError("Background(max_lifetime_seconds=) is a whole number >= 1")

    def to_json(self) -> dict[str, object]:
        return {
            "spawns_background_process": True,
            "cleanup_command": self.cleanup_command,
            "max_lifetime_seconds": self.max_lifetime_seconds,
        }
