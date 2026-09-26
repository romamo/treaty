"""Custom scalar types: an app-level registry mapping a domain class to how it parses.

A registered class can annotate a field or an output attribute. It travels as
its base JSON type (``str``, ``int``, or ``float``), is parsed through the
registered callable on every input route, and the manifest and JSON Schema
carry the declared pattern or bounds. The domain class never imports treaty.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ._errors import RegistrationError

# REQ-C-020 presets a scalar may claim instead of a pattern; filepath belongs to Path fields
PATTERN_TYPES = frozenset({"alphanumeric_id", "uuid", "semver", "url"})
# What each preset accepts; alphanumeric_id rejects /, ., ?, #, and % (REQ-C-020)
PRESET_PATTERNS = {
    "alphanumeric_id": r"[A-Za-z0-9][A-Za-z0-9_-]*",
    "uuid": r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
    "semver": r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?",
}
_BASES: dict[type, str] = {str: "string", int: "integer", float: "number"}
_BUILTIN = (str, int, float, bool, Path)


@dataclass(frozen=True, slots=True)
class ScalarSpec:
    """How one registered class is parsed, checked, serialized, and described"""

    cls: type
    parse: Callable[[Any], object]
    serialize: Callable[[Any], object]
    base: type
    pattern: str | None = None
    pattern_type: str | None = None
    minimum: int | float | None = None
    maximum: int | float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.cls, type):
            raise RegistrationError(f"scalar must be a class, got {self.cls!r}")
        name = self.cls.__qualname__
        if self.cls in _BUILTIN or issubclass(self.cls, Enum):
            raise RegistrationError(f"{name} is a built-in scalar type and cannot be registered")
        if self.base not in _BASES:
            raise RegistrationError(f"{name}: base must be str, int, or float, got {self.base!r}")
        if not callable(self.parse):
            raise RegistrationError(f"{name}: parse must be callable")
        if self.pattern is not None and self.pattern_type is not None:
            raise RegistrationError(f"{name}: pattern and pattern_type are mutually exclusive")
        if self.pattern_type is not None and self.pattern_type not in PATTERN_TYPES:
            raise RegistrationError(
                f"{name}: pattern_type must be one of {', '.join(sorted(PATTERN_TYPES))}"
            )
        if (self.pattern is not None or self.pattern_type is not None) and self.base is not str:
            raise RegistrationError(f"{name}: a pattern needs base=str")
        if self.pattern is not None:
            re.compile(self.pattern)
        if (self.minimum is not None or self.maximum is not None) and self.base is str:
            raise RegistrationError(f"{name}: minimum and maximum need base=int or base=float")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise RegistrationError(f"{name}: minimum exceeds maximum")

    @property
    def json_type(self) -> str:
        return _BASES[self.base]

    def json_schema(self) -> dict[str, object]:
        schema: dict[str, object] = {"type": self.json_type, "title": self.cls.__name__}
        if self.pattern is not None:
            # Checked with re.fullmatch; JSON Schema patterns search, so anchor them
            schema["pattern"] = anchored(self.pattern)
        if self.pattern_type in ("alphanumeric_id", "semver"):
            schema["pattern"] = anchored(PRESET_PATTERNS[self.pattern_type])
        if self.pattern_type == "uuid":
            schema["format"] = "uuid"
        if self.pattern_type == "url":
            schema["format"] = "uri"
        if self.minimum is not None:
            schema["minimum"] = self.minimum
        if self.maximum is not None:
            schema["maximum"] = self.maximum
        return schema

    def violation(self, base_value: object) -> tuple[str, dict[str, object]] | None:
        """The declared constraint the base value breaks, before the parser sees it"""
        if self.pattern is not None:
            assert isinstance(base_value, str)
            if not re.fullmatch(self.pattern, base_value):
                return "does not match pattern", {"pattern": self.pattern}
        if self.pattern_type is not None:
            assert isinstance(base_value, str)
            if not _matches_preset(self.pattern_type, base_value):
                return f"is not a valid {self.pattern_type}", {"pattern_type": self.pattern_type}
        if self.minimum is not None:
            assert isinstance(base_value, (int, float))
            if base_value < self.minimum:
                return f"must be at least {self.minimum}", {"minimum": self.minimum}
        if self.maximum is not None:
            assert isinstance(base_value, (int, float))
            if base_value > self.maximum:
                return f"must be at most {self.maximum}", {"maximum": self.maximum}
        return None


def anchored(pattern: str) -> str:
    """A ``re.fullmatch`` pattern as the anchored form JSON Schema and FlagEntry expect"""
    return f"^(?:{pattern})$"


def _matches_preset(preset: str, value: str) -> bool:
    if preset == "url":
        try:
            parts = urlsplit(value)
        except ValueError:
            return False  # e.g. an unclosed IPv6 bracket: not a URL, not a crash
        return parts.scheme in ("http", "https") and bool(parts.netloc)
    return re.fullmatch(PRESET_PATTERNS[preset], value) is not None


def default_serializer(cls: type, base: type) -> Callable[[Any], object]:
    """A dataclass with a ``value`` field serializes as that field; a str base falls back to str"""
    if dataclasses.is_dataclass(cls) and any(f.name == "value" for f in dataclasses.fields(cls)):
        return lambda value: value.value
    if base is str:
        return str
    raise RegistrationError(
        f"{cls.__qualname__}: cannot infer how to serialize it; pass serialize="
    )


class ScalarRegistry:
    def __init__(self) -> None:
        self._specs: dict[type, ScalarSpec] = {}

    def register(self, spec: ScalarSpec) -> ScalarSpec:
        if spec.cls in self._specs:
            raise RegistrationError(f"{spec.cls.__qualname__} is already a registered scalar")
        self._specs[spec.cls] = spec
        return spec

    def get(self, cls: object) -> ScalarSpec | None:
        return self._specs.get(cls) if isinstance(cls, type) else None

    def for_value(self, value: object) -> ScalarSpec | None:
        return self._specs.get(type(value))

    def __len__(self) -> int:
        return len(self._specs)


EMPTY = ScalarRegistry()
"""Shared registry for callers that never see custom scalars"""
