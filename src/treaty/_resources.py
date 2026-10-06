"""Typed resources: values a handler asks for by annotating extra parameters.

A resource is any class with an ``acquire`` classmethod taking ``(args, ctx)``
plus, optionally, further parameters annotated with other resource classes.
Resources are resolved after validation, once per run, in dependency order, and
a ``CliExit`` raised inside ``acquire`` becomes an ordinary envelope. The graph
is checked at registration so a missing ``acquire`` or a cycle never reaches a
run.
"""

from __future__ import annotations

import dataclasses
import inspect
import typing
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from ._aio import Loop
from ._context import Ctx
from ._errors import RegistrationError
from ._types import signature, type_hints


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    """One resource class and the resources its ``acquire`` asks for"""

    cls: type
    acquire: Callable[..., object]
    deps: tuple[type, ...]
    args_type: type | None = None
    """The args class ``acquire`` is annotated to read, when it names one"""
    releases: bool = False
    """The class defines ``release(self)``, called when the run ends (REQ-C-017)"""
    async_acquire: bool = False
    async_release: bool = False

    @property
    def is_async(self) -> bool:
        """Its ``acquire`` or ``release`` is ``async def``: only an async handler can use it"""
        return self.async_acquire or self.async_release


def refuse_async(fn: object, where: str) -> None:
    """REQ-F-049: only a handler and a resource's ``acquire`` and ``release`` run on the
    run's event loop; any other ``async def`` would return a coroutine that is never
    awaited, and its work would silently never happen"""
    if inspect.iscoroutinefunction(fn) or inspect.isasyncgenfunction(fn):
        raise RegistrationError(
            f"{where}: is async def, and treaty calls it without an event loop, so its body "
            "would never run; make it a plain def (only handlers and resource acquire and "
            "release may be async def) (REQ-F-049)"
        )


def refuse_async_generator(fn: object, where: str) -> None:
    if inspect.isasyncgenfunction(fn):
        raise RegistrationError(
            f"{where}: is an async generator, which treaty does not iterate; stream from a "
            "plain generator, or return a list from an async def (REQ-F-049)"
        )


def dependency_params(
    fn: Callable[..., object],
    where: str,
    *,
    allow_async: bool = False,
    allow_async_generator: bool = False,
) -> tuple[type, ...]:
    """Resource classes named by the parameters after ``(args, ctx)``; validates that prefix.
    ``allow_async`` lets a coroutine function through, for handlers and ``acquire``;
    ``allow_async_generator`` an async generator, for a streaming handler."""
    if not allow_async:
        refuse_async(fn, where)
    elif not allow_async_generator:
        refuse_async_generator(fn, where)
    params = list(signature(fn).parameters.values())
    if len(params) < 2 or any(
        p.kind not in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) for p in params
    ):
        raise RegistrationError(f"{where}: must take (args, ctx, *resources) positionally")
    hints = type_hints(fn)
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
    deps = dependency_params(acquire, f"{cls.__qualname__}.acquire", allow_async=True)
    first = next(iter(signature(acquire).parameters))
    wanted = type_hints(acquire).get(first)
    args_type = wanted if isinstance(wanted, type) and wanted is not object else None
    releases = _releases(cls)
    release = inspect.getattr_static(cls, "release", None)
    return ResourceSpec(
        cls=cls,
        acquire=acquire,
        deps=deps,
        args_type=args_type,
        releases=releases,
        async_acquire=inspect.iscoroutinefunction(acquire),
        async_release=releases and inspect.iscoroutinefunction(release),
    )


def _releases(cls: type) -> bool:
    """``release(self) -> None``: a method the run calls once, after the handler; an
    ``async def release`` runs on the run's event loop"""
    release = inspect.getattr_static(cls, "release", None)
    if release is None:
        return False
    where = f"{cls.__qualname__}.release"
    if not inspect.isfunction(release):
        raise RegistrationError(f"{where} must be a plain method: def release(self) -> None")
    refuse_async_generator(release, where)
    params = list(signature(release).parameters.values())
    if len(params) != 1 or params[0].kind not in (
        params[0].POSITIONAL_ONLY,
        params[0].POSITIONAL_OR_KEYWORD,
    ):
        raise RegistrationError(f"{where} takes only self: def release(self) -> None")
    return True


def resource_graph(
    roots: Sequence[type],
    where: str,
    args_type: type | None = None,
    provided: Sequence[type] = (),
    *,
    fields: Collection[str] = (),
) -> dict[type, ResourceSpec]:
    """Every resource reachable from ``roots``; fails on a cycle, a class without acquire,
    or an ``acquire`` that reads an args class the command's args do not extend.
    ``provided`` classes, such as ``App(settings=)``, are values the run supplies."""
    specs: dict[type, ResourceSpec] = {}
    visiting: list[type] = []

    def visit(cls: type) -> None:
        if cls in visiting:
            chain = " -> ".join(c.__qualname__ for c in [*visiting, cls])
            raise RegistrationError(f"{where}: resource cycle {chain}")
        if cls in specs or cls in provided:
            return
        visiting.append(cls)
        spec = resource_spec(cls)
        wanted = spec.args_type
        if args_type is not None and wanted is not None and typing.is_protocol(wanted):
            # Structural: the args need the protocol's members, not a base class
            # The parsed fields: an args model's, which ``fields`` names, are not attributes
            # of its class
            names = set(fields) | (
                {f.name for f in dataclasses.fields(args_type)}
                if dataclasses.is_dataclass(args_type)
                else set()
            )
            missing = sorted(typing.get_protocol_members(wanted) - names - set(dir(args_type)))
            if missing:
                raise RegistrationError(
                    f"{where}: {cls.__qualname__}.acquire reads {wanted.__qualname__}, but "
                    f"the command's args {args_type.__qualname__} lack {missing}"
                )
        elif args_type is not None and wanted is not None and not issubclass(args_type, wanted):
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
    for spec in specs.values():
        if spec.is_async:
            continue
        for dep in spec.deps:
            if dep in specs and specs[dep].is_async:
                raise RegistrationError(
                    f"{where}: {spec.cls.__qualname__} is sync but needs "
                    f"{dep.__qualname__}, which is async; its release would run after the "
                    f"loop that {dep.__qualname__} lives on is gone: make "
                    f"{spec.cls.__qualname__}.acquire async def too (REQ-F-049)"
                )
    return specs


class Resolver:
    """Acquires resources for one run, each at most once, in dependency order"""

    def __init__(
        self,
        graph: Mapping[type, ResourceSpec],
        args: object,
        ctx: Ctx,
        provided: Mapping[type, object],
        loop: Loop | None = None,
    ) -> None:
        self._graph = graph
        self._args = args
        self._ctx = ctx
        self._cache: dict[type, object] = dict(provided)
        self._loop = loop

    def get(self, cls: type) -> object:
        if cls in self._cache:
            return self._cache[cls]
        spec = self._graph[cls]
        deps = [self.get(dep) for dep in spec.deps]
        return self._keep(cls, spec, spec.acquire(self._args, self._ctx, *deps))

    async def aget(self, cls: type) -> object:
        """``get`` on the run's event loop, awaiting an ``async def acquire``"""
        if cls in self._cache:
            return self._cache[cls]
        spec = self._graph[cls]
        deps = [await self.aget(dep) for dep in spec.deps]
        value = spec.acquire(self._args, self._ctx, *deps)
        if spec.async_acquire:
            value = await cast(Awaitable[object], value)
        return self._keep(cls, spec, value)

    def _keep(self, cls: type, spec: ResourceSpec, value: object) -> object:
        if not isinstance(value, cls):
            raise TypeError(
                f"{cls.__qualname__}.acquire returned {type(value).__qualname__}, "
                f"not {cls.__qualname__}"
            )
        self._cache[cls] = value
        teardown = self._ctx._teardown
        if spec.releases and teardown is not None:
            release = getattr(value, "release")  # noqa: B009
            if spec.async_release:
                assert self._loop is not None  # an async resource needs an async handler
                loop = self._loop
                teardown.add(f"{cls.__qualname__}.release", lambda: loop.run(release()))
            else:
                teardown.add(f"{cls.__qualname__}.release", release)
        return value

    def all(self, classes: Sequence[type]) -> list[Any]:
        return [self.get(cls) for cls in classes]

    async def aall(self, classes: Sequence[type]) -> list[Any]:
        return [await self.aget(cls) for cls in classes]
