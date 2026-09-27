"""Batch results: one outcome per item, and a summary (REQ-C-009).

A handler that works through many items returns ``Batch[T]``, one ``Item`` per item with
either its ``value`` or its ``error``. The response's ``data`` is ``summary`` (total,
succeeded, failed) and ``results``, one entry per item in the handler's order, so a
caller retries only the failed ones. Any failed item exits 3, ``PARTIAL_FAILURE``.
"""

from __future__ import annotations

import re
import typing
from collections.abc import Sequence
from dataclasses import dataclass

from ._errors import CliExit
from ._schema import JsonSchema

_CODE = re.compile(r"[A-Z][A-Z0-9_]+")

ITEM_KEYS = frozenset({"id", "ok", "error"})
"""Keys of a ``results`` entry that a value's fields cannot take"""


@dataclass(frozen=True, slots=True)
class ItemError:
    """Why one item failed, in the shape of the response's own ``error`` (REQ-C-013)"""

    code: str
    message: str
    retryable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not _CODE.fullmatch(self.code):
            raise TypeError(f"ItemError code {self.code!r} is not UPPER_SNAKE_CASE")
        if not isinstance(self.message, str) or not self.message:
            raise TypeError("ItemError message is non-empty text")
        if not isinstance(self.retryable, bool):
            raise TypeError("ItemError retryable is a bool")

    def to_json(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


@dataclass(frozen=True, slots=True)
class Item[T]:
    """One item's outcome: its ``value``, or its ``error``; a raised ``treaty.Exit`` is an
    error too, whose ``retryable`` comes from its exit code"""

    id: str | int
    value: T | None = None
    error: ItemError | CliExit | None = None

    def __post_init__(self) -> None:
        if isinstance(self.id, bool) or not isinstance(self.id, (str, int)):
            raise TypeError(f"Item id {self.id!r} is a string or an int")
        if (self.value is None) == (self.error is None):
            raise TypeError(f"Item {self.id!r} has exactly one of value and error")
        if self.error is not None and not isinstance(self.error, (ItemError, CliExit)):
            raise TypeError(f"Item {self.id!r} error is a treaty.ItemError or a treaty.Exit")

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True, slots=True)
class Batch[T]:
    """Every item's outcome, in the order they were worked through"""

    items: Sequence[Item[T]]

    def __post_init__(self) -> None:
        if isinstance(self.items, (str, bytes)) or not isinstance(self.items, Sequence):
            raise TypeError("Batch.items must be a list or tuple")
        if not all(isinstance(i, Item) for i in self.items):
            raise TypeError("Batch.items holds treaty.Item values")


def batch_item(annotation: object) -> object | None:
    """``T`` of a ``Batch[T]`` return annotation, else None"""
    if annotation is Batch:
        return object
    if typing.get_origin(annotation) is Batch:
        item: object = typing.get_args(annotation)[0]
        return item
    return None


def batch_schema(item: JsonSchema) -> JsonSchema:
    """``data`` of a batch command: the summary and one result per item"""
    count: JsonSchema = {"type": "integer", "minimum": 0}
    error: JsonSchema = {
        "type": "object",
        "required": ["code", "message", "retryable"],
        "properties": {
            "code": {"type": "string"},
            "message": {"type": "string"},
            "retryable": {"type": "boolean"},
        },
        "additionalProperties": False,
    }
    succeeded: JsonSchema = {
        **item,
        "required": ["id", "ok", *item.get("required", [])],
        "properties": {
            "id": {"type": ["string", "integer"]},
            "ok": {"const": True},
            **item.get("properties", {}),
        },
    }
    failed: JsonSchema = {
        "type": "object",
        "required": ["id", "ok", "error"],
        "properties": {
            "id": {"type": ["string", "integer"]},
            "ok": {"const": False},
            "error": error,
        },
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "required": ["summary", "results"],
        "properties": {
            "summary": {
                "type": "object",
                "required": ["total", "succeeded", "failed"],
                "properties": {"total": count, "succeeded": count, "failed": count},
                "additionalProperties": False,
            },
            "results": {
                "type": "array",
                "items": {"anyOf": [succeeded, failed]},
                "x-ordered": True,
            },
            "partial": {"type": "boolean"},
        },
    }
