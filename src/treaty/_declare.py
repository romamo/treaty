"""Declarations an agent reads before it runs a command: which child binary gets which
argument (REQ-C-019), and the checks and manifest fields that go with them."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass

from ._errors import ParseError, RegistrationError
from ._flags import FieldInfo
from ._scan import CtxCall

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
    return declared


def derive_subprocess(calls: Sequence[CtxCall], fields: Sequence[FieldInfo]) -> Subprocess | None:
    """The declaration the handler's ``ctx.run([...])`` calls show, when every call runs
    one list literal starting with the same literal binary; None when any cannot be read"""
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
