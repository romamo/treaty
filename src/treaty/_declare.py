"""Declarations an agent reads before it runs a command: which child binary gets which
argument (REQ-C-019), and the checks and manifest fields that go with them."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

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


PROJECT_ROOT_PREFIX = "{project_root}/"
"""Where a ``SideEffect`` path under the command's project starts"""


class SideEffectType(StrEnum):
    CACHE = "cache"
    LOG = "log"
    TEMP = "temp"
    CREDENTIAL = "credential"
    CONFIG = "config"
    OUTPUT = "output"


@dataclass(frozen=True, slots=True)
class SideEffect:
    """A filesystem location a command writes (REQ-C-011)

    ``path`` is absolute, starts with ``~/``, or starts with ``{project_root}/`` for a
    path under the project the command's ``project_root=`` markers find, resolved from the
    working directory when ``cleanup`` and ``status`` run; ``{name}`` placeholders and
    ``*`` match any one path segment, so ``"/tmp/tool-{session}/"`` covers every session.
    ``type`` is
    ``cache``, ``log``, ``temp``, ``credential``, ``config``, or ``output``; ``cleanup``
    removes the ``temp``, ``cache``, and ``log`` ones, and ``status`` lists them all.
    ``output`` is a location the command writes as its product, such as a rendered
    dashboard, including its default when ``--output`` is absent: ``cleanup`` never
    removes it, and it takes no ``ttl_seconds`` or ``clearable_with``. A per-call
    ``--output`` path is declared by ``output_file=``, not here.
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
            self.path.startswith(("/", "~/", PROJECT_ROOT_PREFIX))
            or re.match(r"[A-Za-z]:[\\/]", self.path)
        ):
            raise RegistrationError(
                f"{where}: path is absolute or starts with ~/ or {PROJECT_ROOT_PREFIX}"
            )
        rest = re.sub(
            r"^(~/|/|[A-Za-z]:[\\/]|" + re.escape(PROJECT_ROOT_PREFIX) + ")", "", self.path
        )
        segments = re.split(r"[\\/]", rest)
        if any(s in (".", "..") for s in segments):
            # {project_root}/../x would reach outside the project cleanup is confined to
            raise RegistrationError(f"{where}: path has no . or .. segments")
        if not re.sub(r"\{[^{}/]*\}|\*", "", segments[0]):
            # cleanup would remove all of /, the home directory, or the project
            raise RegistrationError(
                f"{where}: path names a directory under /, ~/, or {PROJECT_ROOT_PREFIX}, not "
                "every entry of one"
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
        if self.type == SideEffectType.OUTPUT and (
            self.ttl_seconds is not None or self.clearable_with is not None
        ):
            # A product neither goes stale nor is the framework's to clear (REQ-C-011)
            raise RegistrationError(
                f"{where}: an output side effect is the command's product, which cleanup "
                "never removes, so it takes no ttl_seconds or clearable_with"
            )

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

    @property
    def in_project(self) -> bool:
        """Whether the path starts with ``{project_root}/``, under the command's project"""
        return self.path.startswith(PROJECT_ROOT_PREFIX)

    def pattern(self, home: str | None, project: Path | None = None) -> str | None:
        """The glob of every path it covers; None for ``~/`` without a home, and for a
        ``{project_root}/`` path without the project directory"""
        if self.in_project:
            if project is None:
                return None
            # The placeholders of the declared part only: a { in the project's own path
            # is a literal character
            rest = re.sub(r"\{[^{}/]*\}", "*", self.path.removeprefix(PROJECT_ROOT_PREFIX))
            return (project.as_posix().rstrip("/") + "/" + rest).rstrip("/")
        path = self.path
        if path.startswith("~/"):
            if not home:
                return None
            path = home.rstrip("/") + path[1:]
        return re.sub(r"\{[^{}/]*\}", "*", path).rstrip("/") or "/"


def check_side_effects(
    where: str, effects: Sequence[SideEffect], project_root: Sequence[str] = ()
) -> tuple[SideEffect, ...]:
    """The declarations as a tuple; a ``{project_root}/`` path needs the command's
    ``project_root=`` markers, or ``cleanup`` could not tell which project it is under"""
    if isinstance(effects, (str, SideEffect)) or not all(
        isinstance(e, SideEffect) for e in effects
    ):
        raise RegistrationError(
            f"{where}: filesystem_side_effects is a list of treaty.SideEffect(path, type)"
        )
    unbased = [e.path for e in effects if e.in_project]
    if unbased and not project_root:
        raise RegistrationError(
            f"{where}: SideEffect({unbased[0]!r}) is under the project, and the command "
            "declares no project_root= markers to find it, such as project_root=('.git',)"
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
