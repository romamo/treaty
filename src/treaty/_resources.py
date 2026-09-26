"""Typed resources: values a handler asks for by annotating extra parameters.

A resource is any class with an ``acquire`` classmethod taking ``(args, ctx)``
plus, optionally, further parameters annotated with other resource classes.
Resources are resolved after validation, once per run, in dependency order, and
a ``CliExit`` raised inside ``acquire`` becomes an ordinary envelope. The graph
is checked at registration so a missing ``acquire`` or a cycle never reaches a
run.
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ._context import Ctx
from ._errors import RegistrationError


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    """One resource class and the resources its ``acquire`` asks for"""

    cls: type
    acquire: Callable[..., object]
    deps: tuple[type, ...]
    args_type: type | None = None
    """The args class ``acquire`` is annotated to read, when it names one"""


def dependency_params(fn: Callable[..., object], where: str) -> tuple[type, ...]:
    """Resource classes named by the parameters after ``(args, ctx)``; validates that prefix"""
    params = list(inspect.signature(fn).parameters.values())
    if len(params) < 2 or any(
        p.kind not in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) for p in params
    ):
        raise RegistrationError(f"{where}: must take (args, ctx, *resources) positionally")
    hints = typing.get_type_hints(fn)
    if hints.get(params[1].name) is not Ctx:
        raise RegistrationError(f"{where}: second parameter must be annotated with Ctx")
    deps: list[type] = []
    for p in params[2:]:
        hint = hints.get(p.name)
        if not isinstance(hint, type):
            raise RegistrationError(
                f"{where}: parameter {p.name!r} must be annotated with a resource class"
            )
        if hint in deps:
            raise RegistrationError(f"{where}: resource {hint.__qualname__} asked for twice")
        deps.append(hint)
    return tuple(deps)


def resource_spec(cls: type) -> ResourceSpec:
    acquire = getattr(cls, "acquire", None)
    if not callable(acquire) or not inspect.ismethod(acquire) or acquire.__self__ is not cls:
        raise RegistrationError(
            f"{cls.__qualname__} is not a resource: it needs a classmethod acquire(cls, args, ctx)"
        )
    deps = dependency_params(acquire, f"{cls.__qualname__}.acquire")
    first = next(iter(inspect.signature(acquire).parameters))
    wanted = typing.get_type_hints(acquire).get(first)
    args_type = wanted if isinstance(wanted, type) and wanted is not object else None
    return ResourceSpec(cls=cls, acquire=acquire, deps=deps, args_type=args_type)


def resource_graph(
    roots: Sequence[type], where: str, args_type: type | None = None
) -> dict[type, ResourceSpec]:
    """Every resource reachable from ``roots``; fails on a cycle, a class without acquire,
    or an ``acquire`` that reads an args class the command's args do not extend"""
    specs: dict[type, ResourceSpec] = {}
    visiting: list[type] = []

    def visit(cls: type) -> None:
        if cls in visiting:
            chain = " -> ".join(c.__qualname__ for c in [*visiting, cls])
            raise RegistrationError(f"{where}: resource cycle {chain}")
        if cls in specs:
            return
        visiting.append(cls)
        spec = resource_spec(cls)
        wanted = spec.args_type
        if args_type is not None and wanted is not None and not issubclass(args_type, wanted):
            raise RegistrationError(
                f"{where}: {cls.__qualname__}.acquire reads {wanted.__qualname__}, but the "
                f"command's args are {args_type.__qualname__}; subclass {wanted.__qualname__}"
            )
        for dep in spec.deps:
            visit(dep)
        visiting.pop()
        specs[cls] = spec

    for root in roots:
        visit(root)
    return specs


class Resolver:
    """Acquires resources for one run, each at most once, in dependency order"""

    def __init__(self, graph: Mapping[type, ResourceSpec], args: object, ctx: Ctx) -> None:
        self._graph = graph
        self._args = args
        self._ctx = ctx
        self._cache: dict[type, object] = {}

    def get(self, cls: type) -> object:
        if cls in self._cache:
            return self._cache[cls]
        spec = self._graph[cls]
        deps = [self.get(dep) for dep in spec.deps]
        value = spec.acquire(self._args, self._ctx, *deps)
        if not isinstance(value, cls):
            raise TypeError(
                f"{cls.__qualname__}.acquire returned {type(value).__qualname__}, "
                f"not {cls.__qualname__}"
            )
        self._cache[cls] = value
        return value

    def all(self, classes: Sequence[type]) -> list[Any]:
        return [self.get(cls) for cls in classes]
