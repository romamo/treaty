"""``Out``, the output-field marker, ``Binary``, and the order ``data`` is written in.

Consumers cache, diff, and decode ``data`` byte for byte, so its arrays are sorted
(REQ-F-020): strings by code point, numbers ascending, booleans false first, and objects
by a declared ``sort_key`` or else by their canonical JSON. Fixed tuples, arrays mixing
kinds, and anything declared ``ordered`` keep the handler's order. ``--stable-output``
also drops the fields declared ``volatile`` (REQ-O-007).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import typing
from collections.abc import Callable, Mapping, Sequence
from dataclasses import MISSING, dataclass, field
from enum import Enum
from typing import Any

from ._adapters import OutputAdapters, branch, untyped
from ._errors import RegistrationError
from ._types import is_dataclass_type, resolve_alias, strip_optional, type_hints

OUT_META = "treaty.out"


@dataclass(frozen=True, slots=True)
class OutSpec:
    sort_key: str | None = None
    """The field of each array item that orders the array"""
    ordered: bool = False
    """The handler's order is the contract; the array is not sorted"""
    volatile: bool = False
    """Differs between identical calls; left out under ``--stable-output``"""
    high_entropy: bool | None = None
    """Masked unless ``--unmask``: True always, False never, None by the name (REQ-F-058)"""
    external: bool = False
    """Holds content from outside the tool; ``data`` gets trust tags (REQ-F-035)"""
    table: bool = True
    """A column of the plain table a list of these objects renders as; output only"""


NO_ORDER = OutSpec()


def Out(
    *,
    default: Any = MISSING,
    default_factory: Callable[[], Any] | Any = MISSING,
    sort_key: str | None = None,
    ordered: bool = False,
    volatile: bool = False,
    high_entropy: bool | None = None,
    external: bool = False,
    table: bool = True,
) -> Any:
    """Declare how a field of an output dataclass is written, the output ``Flag``

    ``sort_key`` names the item field an array of objects is sorted by; ``ordered=True``
    keeps the handler's order instead (a ranking), also of arrays inside an untyped
    (``object`` or ``dict[str, object]``) value; ``volatile=True`` marks a value that
    differs between identical calls, which ``--stable-output`` leaves out.
    ``high_entropy=True`` masks the value unless ``--unmask``, ``False`` exempts it (a
    content hash to compare), and the default masks it when the name says credential.
    ``external=True`` marks content from outside the tool: when it has a value, ``data``
    is tagged ``_trusted: false``. ``table=False`` leaves the field out of the aligned
    table ``--format plain`` prints for a list of these objects; JSON keeps it.
    """
    if sort_key is not None and ordered:
        raise RegistrationError("Out: sort_key orders the array, ordered=True keeps it; pick one")
    if high_entropy is not None and not isinstance(high_entropy, bool):
        raise RegistrationError("Out: high_entropy is True, False, or None (by the name)")
    if not isinstance(external, bool):
        raise RegistrationError("Out: external is True or False")
    if not isinstance(table, bool):
        raise RegistrationError("Out: table is True or False")
    spec = OutSpec(sort_key, ordered, volatile, high_entropy, external, table)
    if default is not MISSING and default_factory is not MISSING:
        raise RegistrationError("Out: pass default or default_factory, not both")
    if default_factory is not MISSING:
        return field(default_factory=default_factory, metadata={OUT_META: spec})
    if default is not MISSING:
        return field(default=default, metadata={OUT_META: spec})
    return field(metadata={OUT_META: spec})


def out_spec(f: dataclasses.Field[Any]) -> OutSpec:
    spec = f.metadata.get(OUT_META)
    return spec if isinstance(spec, OutSpec) else NO_ORDER


@dataclass(frozen=True, slots=True)
class Binary:
    """Bytes in ``data``, written as a base64 wrapper object with ``content_type``
    (REQ-F-017); a plain ``bytes`` value is written the same way without it"""

    data: bytes
    content_type: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise TypeError(f"Binary.data is bytes, not {type(self.data).__name__}")


@dataclass(frozen=True, slots=True)
class External:
    """A value of a failure's ``error.context`` that came from outside the tool, such
    as a child's output: ``Exit.X(msg, context={"output": External(done.stderr)})``.
    It is masked as external ``data`` is, and the context gets the trust tags
    ``_source: external`` and ``_trusted: false`` (REQ-F-035). It marks a top-level
    context value only; ``Out(external=True)`` and ``external=True`` mark ``data``."""

    value: object


def is_binary(value: object) -> bool:
    """A JSON value that is a binary wrapper, which no cut or sort may look inside"""
    return (
        isinstance(value, dict)
        and value.get("type") == "binary"
        and value.get("encoding") == "base64"
        and "value" in value
    )


def data_path(path: Sequence[str | int]) -> str:
    """Where a value sits in the envelope, as warnings name it: ``data.items[3].token``"""
    out = "data"
    for key in path:
        out += f"[{key}]" if isinstance(key, int) else f".{key}"
    return out


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def arrange(
    value: object,
    tp: object,
    spec: OutSpec = NO_ORDER,
    *,
    adapters: OutputAdapters,
    stable: bool = False,
) -> object:
    """``value``, the JSON form of an instance of ``tp``, with its arrays sorted and, when
    ``stable``, its volatile fields dropped; ``spec`` declares the array ``value`` is. A
    class an output adapter writes is arranged by its schema's ``x-`` options."""
    base, _ = strip_optional(resolve_alias(tp))
    if adapters.for_type(base) is not None:
        assert isinstance(base, type)
        return arrange_node(value, adapters.node(base), spec, stable=stable)
    # Untyped content declares nothing of its own, so an ordered declaration covers it
    inner = spec if spec.ordered else NO_ORDER
    if isinstance(value, list):
        origin = typing.get_origin(base)
        args = typing.get_args(base)
        if origin is tuple and args and args[-1] is not Ellipsis:
            return [
                arrange(v, a, adapters=adapters, stable=stable)
                for v, a in zip(value, args, strict=False)
            ]
        item = args[0] if origin in (list, tuple) and args else object
        items = [
            arrange(
                v, item, inner if _untyped(item) else NO_ORDER, adapters=adapters, stable=stable
            )
            for v in value
        ]
        return items if spec.ordered else [items[i] for i in sorted_indices(items, spec.sort_key)]
    if not isinstance(value, dict) or is_binary(value):
        return value
    if is_dataclass_type(base):
        assert isinstance(base, type)
        hints = type_hints(base)
        out = dict(value)
        for f in dataclasses.fields(base):
            fspec = out_spec(f)
            if f.name not in out:
                continue
            if stable and fspec.volatile:
                del out[f.name]
                continue
            out[f.name] = arrange(
                out[f.name], hints[f.name], fspec, adapters=adapters, stable=stable
            )
        return out
    item = typing.get_args(base)[1] if typing.get_origin(base) is dict else object
    return {
        k: arrange(v, item, inner if _untyped(item) else NO_ORDER, adapters=adapters, stable=stable)
        for k, v in value.items()
    }


def arrange_node(
    value: object, node: Mapping[str, Any], spec: OutSpec = NO_ORDER, *, stable: bool
) -> object:
    """``arrange`` by a JSON Schema: a property's ``x-sort-key`` and ``x-ordered`` order
    its array, and ``x-volatile`` drops it under ``stable``"""
    sort_key = node.get("x-sort-key")
    if isinstance(sort_key, str):
        spec = OutSpec(sort_key=sort_key)
    elif node.get("x-ordered") is True:
        spec = OutSpec(ordered=True)
    node = branch(dict(node), value)
    if isinstance(value, list):
        items = node.get("items")
        if isinstance(items, list):
            extra = node.get("additionalItems")
            rest = extra if isinstance(extra, dict) else {}
            return [
                arrange_node(v, items[i] if i < len(items) else rest, stable=stable)
                for i, v in enumerate(value)
            ]
        item = items if isinstance(items, dict) else {}
        inner = spec if spec.ordered and untyped(item) else NO_ORDER
        arranged = [arrange_node(v, item, inner, stable=stable) for v in value]
        if spec.ordered:
            return arranged
        return [arranged[i] for i in sorted_indices(arranged, spec.sort_key)]
    if not isinstance(value, dict) or is_binary(value):
        return value
    properties = node.get("properties")
    props = properties if isinstance(properties, dict) else {}
    extra = node.get("additionalProperties")
    rest = extra if isinstance(extra, dict) else {}
    inner = spec if spec.ordered and untyped(rest) else NO_ORDER
    out: dict[str, object] = {}
    for key, v in value.items():
        prop = props.get(key)
        if prop is None:
            out[key] = arrange_node(v, rest, inner, stable=stable)
        elif not (stable and prop.get("x-volatile") is True):
            out[key] = arrange_node(v, prop, stable=stable)
    return out


def _untyped(tp: object) -> bool:
    """Content no annotation describes: ``object``, ``Any``, or a list, tuple, or dict of it,
    such as the ``dict[str, object]`` of a ``model_dump()``"""
    base, _ = strip_optional(resolve_alias(tp))
    if base in (object, Any):
        return True
    origin = typing.get_origin(base)
    args = typing.get_args(base)
    if origin is dict and len(args) == 2:
        return _untyped(args[1])
    if (origin is list and args) or (origin is tuple and len(args) == 2 and args[1] is Ellipsis):
        return _untyped(args[0])
    return False


def _kind(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    return "container"


def sorted_indices(items: list[object], sort_key: str | None = None) -> list[int]:
    """The order ``items``, JSON values, are written in: by ``sort_key`` for objects, by
    value for scalars of one kind, by canonical JSON for containers; mixed kinds keep
    their order. Ties keep a total order through the canonical JSON."""
    indices = list(range(len(items)))
    kinds = {_kind(v) for v in items}
    if len(kinds) != 1:
        return indices
    kind = kinds.pop()
    if kind in ("boolean", "number", "string"):
        return sorted(indices, key=lambda i: items[i])  # type: ignore[return-value,arg-type]
    if kind != "container":
        return indices
    texts = [canonical(v) for v in items]
    if sort_key is not None and all(isinstance(v, dict) and sort_key in v for v in items):
        keys = [v[sort_key] for v in items]  # type: ignore[index]
        if len({_kind(k) for k in keys}) == 1:
            return sorted(indices, key=lambda i: (keys[i], texts[i]))
    return sorted(indices, key=lambda i: texts[i])


# REQ-F-020: the kinds a sort_key field may have, each written as one JSON scalar kind
_KEY_TYPES = (str, int, dt.date, dt.datetime, dt.time)


def can_sort_by(tp: object) -> bool:
    """Whether a field annotated ``tp`` can be a ``sort_key``: a str, int, Enum, or date"""
    key_type = resolve_alias(tp)
    return (
        isinstance(key_type, type)
        and issubclass(key_type, (*_KEY_TYPES, Enum))
        and key_type is not bool
    )


def check_order(
    tp: object, where: str, spec: OutSpec = NO_ORDER, *, adapters: OutputAdapters
) -> None:
    """Each ``sort_key`` of an output type names a scalar field of the array's items"""
    base, _ = strip_optional(resolve_alias(tp))
    origin = typing.get_origin(base)
    args = typing.get_args(base)
    if spec.sort_key is not None:
        item: object = None
        if origin is list and args:
            item = resolve_alias(args[0])
        elif origin is tuple and len(args) == 2 and args[1] is Ellipsis:
            item = resolve_alias(args[0])  # a fixed tuple is never sorted
        if adapters.for_type(item) is not None:
            assert isinstance(item, type)
            _check_adapted_key(adapters.node(item), item, where, spec.sort_key)
        elif not is_dataclass_type(item):
            raise RegistrationError(
                f"{where}: sort_key={spec.sort_key!r} orders an array of dataclasses, not {tp!r}"
            )
        else:
            assert isinstance(item, type)
            hints = type_hints(item)
            if spec.sort_key not in {f.name for f in dataclasses.fields(item)} or not can_sort_by(
                hints.get(spec.sort_key)
            ):
                raise RegistrationError(
                    f"{where}: sort_key={spec.sort_key!r} must name a str, int, Enum, or date "
                    f"field of {item.__qualname__}"
                )
    if spec.ordered and origin not in (list, tuple, dict) and not _untyped(base):
        raise RegistrationError(f"{where}: ordered=True is for arrays, not {tp!r}")
    for arg in args:
        if arg is not Ellipsis:
            check_order(arg, where, adapters=adapters)
    if is_dataclass_type(base):
        assert isinstance(base, type)
        hints = type_hints(base)
        for f in dataclasses.fields(base):
            check_order(
                hints[f.name],
                f"{where}: {base.__qualname__}.{f.name}",
                out_spec(f),
                adapters=adapters,
            )


def _check_adapted_key(node: Mapping[str, Any], item: type, where: str, sort_key: str) -> None:
    properties = node.get("properties")
    prop = properties.get(sort_key) if isinstance(properties, dict) else None
    kind = prop.get("type") if isinstance(prop, dict) else None
    if kind not in ("string", "integer"):
        raise RegistrationError(
            f"{where}: sort_key={sort_key!r} must name a string or integer property of "
            f"{item.__qualname__}"
        )
