"""Custom scalar types: an app-level registry mapping a domain class to how it parses.

A registered class can annotate a field or an output attribute. It travels as
its base JSON type (``str``, ``int``, or ``float``), is parsed through the
registered callable on every input route, and the manifest and JSON Schema
carry the declared pattern or bounds. The domain class never imports treaty.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ._adapters import OutputAdapter, OutputAdapters
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
            check_pattern_publishable(self.pattern, name)
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
            if not matches_preset(self.pattern_type, base_value):
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


def check_pattern_publishable(pattern: str, where: str) -> None:
    """The pattern and its anchored manifest form must both compile; a leading (?i)
    works with re.fullmatch but not once wrapped in ^(?:...)$"""
    try:
        re.compile(pattern)
        re.compile(anchored(pattern))
    except re.error as exc:
        raise RegistrationError(
            f"{where}: pattern {pattern!r} is not publishable ({exc}); scope inline flags "
            "as (?i:...) instead of a leading (?i)"
        ) from None


def anchored(pattern: str) -> str:
    """A ``re.fullmatch`` pattern as the anchored form JSON Schema and FlagEntry expect;
    one its author anchored already is kept as written, not wrapped twice (#388)"""
    return pattern if _whole_anchored(pattern) else f"^(?:{pattern})$"


def _whole_anchored(pattern: str) -> bool:
    """True when ``pattern`` is ``^...$`` with no ``|`` outside a group or class, so a
    search with it matches what ``re.fullmatch`` does"""
    if len(pattern) < 2 or not pattern.startswith("^"):
        return False
    depth = 0
    in_class = False
    index = 1
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            if index == len(pattern) - 2:
                return False  # the closing $ is escaped: a literal dollar
            index += 2
            continue
        if in_class:
            if char == "]":
                in_class = False
        elif char == "[":
            in_class = True
            index += 2 if pattern[index + 1 : index + 2] == "^" else 1
            if pattern[index : index + 1] == "]":
                # A literal to Python, but ECMA closes an empty [] or an any-character
                # [^] there, which may leave a | at its top level: wrap it
                return False
            continue
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "|" and depth == 0:
            return False
        index += 1
    return pattern.endswith("$") and depth == 0 and not in_class


def matches_preset(preset: str, value: str) -> bool:
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
    """The app's own value types: registered scalars, and the output adapters that
    describe and write whole class families (``app.output_adapter``)"""

    def __init__(self) -> None:
        self._specs: dict[type, ScalarSpec] = {}
        self.adapters = OutputAdapters()

    def register(self, spec: ScalarSpec) -> ScalarSpec:
        if spec.cls in self._specs:
            raise RegistrationError(f"{spec.cls.__qualname__} is already a registered scalar")
        if (adapter := self.adapters.for_type(spec.cls)) is not None:
            raise RegistrationError(
                f"{spec.cls.__qualname__} is written by the output adapter for "
                f"{adapter.base.__qualname__}; a class is a scalar or adapted, not both"
            )
        self._specs[spec.cls] = spec
        return spec

    def register_adapter(self, adapter: OutputAdapter) -> OutputAdapter:
        taken = sorted(c.__qualname__ for c in self._specs if issubclass(c, adapter.base))
        if taken:
            raise RegistrationError(
                f"output_adapter: {adapter.base.__qualname__} covers {taken[0]}, a registered "
                "scalar; a class is a scalar or adapted, not both"
            )
        return self.adapters.register(adapter)

    def get(self, cls: object) -> ScalarSpec | None:
        return self._specs.get(cls) if isinstance(cls, type) else None

    def for_value(self, value: object) -> ScalarSpec | None:
        return self._specs.get(type(value))

    def __len__(self) -> int:
        return len(self._specs)


EMPTY = ScalarRegistry()
"""Shared registry for callers that never see custom scalars"""

DECIMAL_TEXT = r"-?[0-9]+(\.[0-9]+)?"
"""Fixed-point text, the one form a ``Decimal`` travels in: no exponent, NaN, or Infinity"""
DECIMAL_HINT = "pass a fixed-point number such as 12.30, with no exponent, NaN, or comma"


def _parse_decimal(text: str) -> Decimal:
    """Text ``DECIMAL_TEXT`` matched; ``-0`` and ``-0.00`` are zero, so a handler never
    sees a signed zero. The scale is kept: ``12.30`` stays ``12.30``"""
    value = Decimal(text)
    return value.copy_abs() if value.is_zero() else value


def _serialize_decimal(value: Decimal) -> str:
    return format(value, "f")


DECIMAL = ScalarSpec(
    cls=Decimal,
    parse=_parse_decimal,
    serialize=_serialize_decimal,
    base=str,
    pattern=DECIMAL_TEXT,
)
"""``Decimal`` arguments, built in: a string on every route, parsed without a float. An
app's own ``app.scalar(Decimal, ...)`` is found first and replaces it"""

DATE_TEXT = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
"""RFC 3339 full-date, the one form a ``datetime.date`` argument travels in: narrower than
``date.fromisoformat``, which also takes ``20240101`` and ``2024-W01-1``"""
DATE_HINT = "pass a date as YYYY-MM-DD, such as 2024-01-31"
DATETIME_TEXT = (
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T([01][0-9]|2[0-3]):[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?"
    r"(Z|[+-][0-9]{2}:[0-5][0-9])"
)
"""RFC 3339 date-time with an offset, an upper-case ``T`` and ``Z``, and at most
microseconds: what ``datetime.fromisoformat`` reads without dropping digits. A naive value
names no instant, so it does not match. The hour stops at 23 and an offset's minute at 59,
which RFC 3339 requires and ``fromisoformat`` does not: it reads ``24:00`` as the next
day's midnight and ``+05:60`` as ``+06:00``"""
DATETIME_HINT = (
    "pass a date-time with an offset, such as 2024-01-31T09:30:00Z or 2024-01-31T09:30:00+02:00"
)


def _parse_date(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


def _parse_datetime(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text)


def _serialize_date(value: dt.date) -> str:
    return value.isoformat()


def _serialize_datetime(value: dt.datetime) -> str:
    """ISO 8601 with ``Z`` for UTC, as output writes it; a naive value has no offset, so its
    text fails ``DATETIME_TEXT`` where a default is checked"""
    text = value.isoformat()
    return text.removesuffix("+00:00") + "Z" if text.endswith("+00:00") else text


DATE = ScalarSpec(
    cls=dt.date, parse=_parse_date, serialize=_serialize_date, base=str, pattern=DATE_TEXT
)
"""``datetime.date`` arguments, built in: ``YYYY-MM-DD`` on every route. An app's own
``app.scalar(datetime.date, ...)`` is found first and replaces it"""
DATETIME = ScalarSpec(
    cls=dt.datetime,
    parse=_parse_datetime,
    serialize=_serialize_datetime,
    base=str,
    pattern=DATETIME_TEXT,
)
"""``datetime.datetime`` arguments, built in: RFC 3339 text with an offset, so a handler
always gets an aware value. An app's own ``app.scalar(datetime.datetime, ...)`` replaces it"""

BUILT_IN = (DECIMAL, DATE, DATETIME)
"""The scalars treaty registers itself, each replaced by an app's own of the same class"""
BUILT_IN_TEXT: dict[type, tuple[str, str, str]] = {
    Decimal: ("a fixed-point decimal", DECIMAL_HINT, "decimal"),
    dt.date: ("a YYYY-MM-DD date", DATE_HINT, "date"),
    dt.datetime: ("a date-time with an offset", DATETIME_HINT, "date-time"),
}
"""Per built-in scalar's class: what its text is, how to fix a value that is not it, and
the type name an object's shape gives it in help and the manifest"""


def built_in(spec: ScalarSpec | None) -> bool:
    """True for one of treaty's own scalars, not an app's registration of the same class"""
    return any(spec is b for b in BUILT_IN)
