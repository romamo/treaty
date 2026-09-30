"""Classification of Python annotations into the spec's flag and schema types.

Shared by the flag inspector and the JSON Schema generator so both agree on
which annotations are supported.
"""

from __future__ import annotations

import annotationlib
import dataclasses
import inspect
import types
import typing
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, StrEnum
from pathlib import Path

from ._errors import RegistrationError, SchemaError
from ._scalars import DECIMAL, ScalarRegistry, ScalarSpec


class FlagType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"
    ENUM = "enum"


_SCALARS: dict[type, FlagType] = {
    bool: FlagType.BOOLEAN,
    int: FlagType.INTEGER,
    float: FlagType.NUMBER,
    str: FlagType.STRING,
}


@dataclass(frozen=True, slots=True)
class Classified:
    """One annotation reduced to a flag type plus what is needed to parse it"""

    flag_type: FlagType
    optional: bool
    base: object
    enum_values: tuple[str, ...] = ()
    enum_cls: type[Enum] | None = None
    item: Classified | None = None
    path: bool = False
    """A ``pathlib.Path`` string: hardened against traversal and encoded bytes"""
    scalar: ScalarSpec | None = None
    """A registered custom scalar: parsed through its spec after the base type"""


def type_hints(obj: object) -> dict[str, typing.Any]:
    """``typing.get_type_hints``, but an annotation naming what is not defined at runtime,
    such as a class imported only under ``TYPE_CHECKING`` (annotations are lazy, so the
    module still imports), is a RegistrationError that lists every such name"""
    try:
        return typing.get_type_hints(obj)
    except NameError as exc:
        lazy = typing.get_type_hints(obj, format=annotationlib.Format.FORWARDREF)
        owner = getattr(obj, "__qualname__", repr(obj))
        entries: list[str] = []
        names: set[str] = set()
        for key, tp in lazy.items():
            undefined = list(dict.fromkeys(_undefined(tp)))
            if not undefined:
                continue
            names.update(undefined)
            if isinstance(obj, type):
                label = f"{owner}.{key}"
            else:
                label = f"{owner}: " + ("the return" if key == "return" else f"parameter {key}")
            entries.append(f"{label} names {', '.join(undefined)}")
        if not entries:
            entries.append(f"{owner}: an annotation names {exc.name}")
            names.add(str(exc.name))
        which = "that name is" if len(names) == 1 else "those names are"
        pronoun = "it" if len(names) == 1 else "them"
        raise RegistrationError(
            f"{'; '.join(entries)}; {which} not defined at runtime: import {pronoun} outside "
            "TYPE_CHECKING, since treaty reads annotations at registration to build the manifest"
        ) from None


def signature(fn: typing.Callable[..., object]) -> inspect.Signature:
    """``inspect.signature`` for parameter names and kinds, which leaves the annotations
    unevaluated, so ``type_hints`` is where an undefined name is reported"""
    return inspect.signature(fn, annotation_format=annotationlib.Format.FORWARDREF)


def _undefined(tp: object) -> typing.Iterator[str]:
    """The names a ``FORWARDREF``-format annotation could not resolve, nested ones too"""
    if isinstance(tp, annotationlib.ForwardRef):
        yield tp.__forward_arg__
        return
    for arg in typing.get_args(tp):
        yield from _undefined(arg)


def resolve_alias(tp: object) -> object:
    """``type Port = int`` (PEP 695) names its value; follow aliases of aliases too"""
    while True:
        if isinstance(tp, typing.TypeAliasType):
            tp = tp.__value__
            continue
        origin = typing.get_origin(tp)
        if isinstance(origin, typing.TypeAliasType):
            # A generic alias applied to arguments: type Vec[T] = list[T]; Vec[int]
            tp = origin.__value__[typing.get_args(tp)]
            continue
        return tp


def strip_optional(tp: object) -> tuple[object, bool]:
    tp = resolve_alias(tp)
    origin = typing.get_origin(tp)
    if origin is not types.UnionType and origin is not typing.Union:
        return tp, False
    members = [a for a in typing.get_args(tp) if a is not types.NoneType]
    if len(members) != 1 or len(members) == len(typing.get_args(tp)):
        raise SchemaError(f"unsupported union {tp!r}; only 'X | None' is allowed")
    return resolve_alias(members[0]), True


def classify(tp: object, scalars: ScalarRegistry) -> Classified:
    base, optional = strip_optional(tp)
    origin = typing.get_origin(base)
    if origin is typing.Literal:
        values = typing.get_args(base)
        if not values or not all(isinstance(v, str) for v in values):
            raise SchemaError(f"Literal values must all be strings: {base!r}")
        return Classified(FlagType.ENUM, optional, base, enum_values=tuple(values))
    if origin in (list, tuple):
        args = typing.get_args(base)
        if origin is tuple:
            if len(args) != 2 or args[1] is not Ellipsis:
                raise SchemaError(f"only homogeneous 'tuple[T, ...]' is supported: {base!r}")
        elif len(args) != 1:
            raise SchemaError(f"list needs exactly one item type: {base!r}")
        item = classify(args[0], scalars)
        if item.flag_type in (FlagType.ARRAY, FlagType.BOOLEAN) or item.optional:
            raise SchemaError(f"array items must be scalars: {base!r}")
        return Classified(FlagType.ARRAY, optional, base, item=item)
    if isinstance(base, type):
        if base in _SCALARS:
            return Classified(_SCALARS[base], optional, base)
        if base is Path:
            return Classified(FlagType.STRING, optional, base, path=True)
        if (spec := scalars.get(base)) is not None:
            return Classified(_SCALARS[spec.base], optional, base, scalar=spec)
        if base is Decimal:
            # Built in as fixed-point text; an app's own app.scalar(Decimal) above wins
            return Classified(FlagType.STRING, optional, base, scalar=DECIMAL)
        if issubclass(base, Enum):
            members = list(base)
            if not all(isinstance(m.value, str) for m in members):
                raise SchemaError(f"enum members must have string values: {base!r}")
            return Classified(
                FlagType.ENUM,
                optional,
                base,
                enum_values=tuple(m.value for m in members),
                enum_cls=base,
            )
    raise SchemaError(f"unsupported annotation {tp!r}; register a class with app.scalar(...)")


def is_dataclass_type(tp: object) -> bool:
    return isinstance(tp, type) and dataclasses.is_dataclass(tp)
