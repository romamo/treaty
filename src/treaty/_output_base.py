"""Where a relative ``--output`` lands (REQ-O-001): the working directory, the project
root, or a directory the app resolves itself, from a resource or a function of the run.

``output_file=True`` keeps the working directory. ``output_file=OutputBase.PROJECT_ROOT``
takes the command's ``project_root=``; ``output_file=Project``, a resource class with a
``directory``, or ``output_file=locate``, a ``def locate(ctx: Ctx, *resources) -> Path``,
take the directory the run resolves. An absolute ``--output`` is used as given.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ._context import Ctx
from ._errors import RegistrationError
from ._resources import refuse_async, resource_spec
from ._types import signature, type_hints


class OutputBase(StrEnum):
    """The directory a relative ``--output`` resolves against, as the manifest names it"""

    CWD = "cwd"
    """The working directory, or ``--cwd``: what ``output_file=True`` means"""
    PROJECT_ROOT = "project_root"
    """The directory holding one of the command's ``project_root=`` markers"""


@dataclass(frozen=True, slots=True)
class OutputRoot:
    """A command's declared base for a relative ``--output``"""

    label: str
    """``cwd``, ``project_root``, or the resource class's or function's name"""
    deps: tuple[type, ...] = ()
    """The resources ``locate`` takes after ``ctx``"""
    locate: Callable[..., object] | None = None
    """``(ctx, *deps)`` to the directory; None for the cwd and the project root"""

    @property
    def is_cwd(self) -> bool:
        return self.label == OutputBase.CWD

    def directory(self, ctx: Ctx, resources: Sequence[object], where: str) -> Path:
        """The base this run resolved; a relative one is under ``ctx.cwd``"""
        assert self.locate is not None
        found = self.locate(ctx, *resources)
        if not isinstance(found, Path):
            raise TypeError(
                f"{where}: output_file={self.label} gave {type(found).__qualname__}, not the "
                "pathlib.Path a relative --output resolves against"
            )
        return ctx.cwd / found


CWD_ROOT = OutputRoot(OutputBase.CWD.value)
PROJECT_ROOT = OutputRoot(OutputBase.PROJECT_ROOT.value)

_TAKES = (
    "output_file takes True (the working directory), treaty.OutputBase.PROJECT_ROOT, a "
    "resource class with a directory, or a function (ctx: Ctx, *resources) -> Path"
)


def output_root(value: object, where: str, project_root: Sequence[str]) -> OutputRoot | None:
    """The base ``output_file=`` declares; None when the command has no ``--output``"""
    if value is False:
        return None
    if value is True or value is OutputBase.CWD:
        return CWD_ROOT
    if value is OutputBase.PROJECT_ROOT:
        if not project_root:
            raise RegistrationError(
                f"{where}: output_file=OutputBase.PROJECT_ROOT resolves --output in the project "
                "root, and the command declares no project_root= markers, such as "
                "project_root=('.git',)"
            )
        return PROJECT_ROOT
    if isinstance(value, type):
        return _resource_root(value, where)
    if inspect.isfunction(value) or inspect.ismethod(value):
        return _function_root(value, where)
    raise RegistrationError(f"{where}: {_TAKES}, not {value!r}")


def _resource_root(cls: type, where: str) -> OutputRoot:
    try:
        resource_spec(cls)
    except RegistrationError as exc:
        raise RegistrationError(f"{where}: output_file={cls.__qualname__}: {exc}") from None
    if "directory" not in type_hints(cls) and not hasattr(cls, "directory"):
        raise RegistrationError(
            f"{where}: output_file={cls.__qualname__} resolves --output in the resource's "
            f"directory, and {cls.__qualname__} declares none; add directory: Path"
        )
    return OutputRoot(cls.__qualname__, (cls,), _directory_of)


def _directory_of(ctx: Ctx, resource: object) -> object:
    return getattr(resource, "directory")  # noqa: B009 - checked at registration


def _function_root(fn: Callable[..., object], where: str) -> OutputRoot:
    name = getattr(fn, "__qualname__", repr(fn))
    refuse_async(fn, f"{where}: output_file={name}")
    params = list(signature(fn).parameters.values())
    hints = type_hints(fn)
    if (
        not params
        or any(p.kind not in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) for p in params)
        or hints.get(params[0].name) is not Ctx
    ):
        raise RegistrationError(
            f"{where}: output_file={name} must take (ctx: Ctx, *resources) positionally"
        )
    deps: list[type] = []
    for p in params[1:]:
        hint = hints.get(p.name)
        if not isinstance(hint, type) or hint in deps:
            raise RegistrationError(
                f"{where}: output_file={name}: parameter {p.name!r} must be annotated with a "
                "resource class, each once"
            )
        try:
            resource_spec(hint)
        except RegistrationError as exc:
            raise RegistrationError(f"{where}: output_file={name}: {exc}") from None
        deps.append(hint)
    return OutputRoot(name, tuple(deps), fn)
