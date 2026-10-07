"""Typed stdin records: ``stdin_records=Sec`` reads another command's output as ``Sec`` (#32).

Built on line mode (#33): each non-blank input line is one JSON object, either a bare record
(as ``--format ndjson`` and a stream's item lines write) or a treaty envelope (as
``--format json`` or ``jsonl`` writes; an exec line's stream events too). An envelope is
unwrapped here, so no consumer reimplements it, and so none can mistake a failed upstream
run for short valid input:

- ``ok: true`` yields its ``data``: an object is one record, an array one record per item,
  ``null`` none
- a stream's terminal envelope (``meta.end``), a whole response (no ``meta.seq``), or
  REQ-O-004's ``{"_summary": true, ...}`` line after bare records ends the input; nothing
  after it is read
- a numbered item line loses its ``_seq`` before it is built
- ``ok: false`` ends the run with ``UPSTREAM_FAILED``, the upstream error in ``context``
- stream events or numbered item lines that stop before their terminal line (the producer
  was killed) end the run with ``UPSTREAM_INCOMPLETE``, rather than look like the end of
  the input
- a heartbeat line is skipped

Each record is checked as an exec line's argument values are, field by field against the
dataclass's annotations, then built, so its ``__post_init__`` runs. A line that fails ends
the run with ``RECORD_INVALID``, its 1-based number and the field in ``context``. Every
failure here exits 1: the handler has started (REQ-F-002).
"""

from __future__ import annotations

import dataclasses
import typing
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass

from ._adapters import OutputAdapters
from ._errors import CliExit, ParseError, RegistrationError, SchemaError
from ._flags import apply_scalar
from ._json5 import loads_strict
from ._lines import Lines
from ._parse import check_json_base
from ._protect import SOURCE_KEY, TRUSTED_KEY, protect
from ._redact import scrub
from ._scalars import ScalarRegistry
from ._schema import JsonSchema, schema_for
from ._types import Classified, FlagType, classify, type_hints
from ._values import ExitCodeName, InvalidValue

RECORD_INVALID = "RECORD_INVALID"
UPSTREAM_FAILED = "UPSTREAM_FAILED"
UPSTREAM_INCOMPLETE = "UPSTREAM_INCOMPLETE"
UPSTREAM_TRUNCATED = "UPSTREAM_TRUNCATED"
"""A warning: an upstream envelope said its ``data`` was cut or was one page of more"""

ENVELOPE_KEYS = frozenset({"ok", "data", "error", "meta", "warnings"})
_HEARTBEAT_KEYS = frozenset({"status", "heartbeat", "elapsed_ms"})
_HEARTBEAT_STEP = "step"
"""A heartbeat inside ``ctx.step`` names the step in progress (REQ-C-008)"""
SUMMARY_KEY = "_summary"
"""``"_summary": true`` marks REQ-O-004's terminal line of a stream of bare items"""
SEQ_KEY = "_seq"
"""A numbered stream's item line position, from 1: framework metadata, never a field"""
COUNT_KEY = "_count"
"""A numbered stream's item lines, on its summary line"""
ITEMS_EMITTED_KEY = "items_emitted"
"""``meta`` of a numbered stream's error envelope: the last ``_seq`` it wrote"""
_TRUST_KEYS = frozenset({SOURCE_KEY, TRUSTED_KEY})
"""Trust tags an external upstream adds (REQ-F-035); not fields of the record"""
_GENERAL = ExitCodeName("GENERAL_ERROR")

WarnSink = Callable[[str, str, Mapping[str, object]], None]


@dataclass(frozen=True, slots=True)
class RecordField:
    name: str
    target: Classified
    required: bool


@dataclass(frozen=True, slots=True)
class RecordSpec:
    """The record type of a ``stdin_records=`` command, read once at registration"""

    cls: type
    fields: tuple[RecordField, ...]
    schema: JsonSchema

    @classmethod
    def inspect(cls, record: object, scalars: ScalarRegistry, where: str) -> RecordSpec:
        """A frozen dataclass whose fields take what an argument field takes: scalars,
        enums, Literals, registered scalars, ``X | None``, and arrays of scalars"""
        if not isinstance(record, type) or not dataclasses.is_dataclass(record):
            raise RegistrationError(
                f"{where}: stdin_records takes a frozen dataclass, not {record!r}"
            )
        if not record.__dataclass_params__.frozen:  # type: ignore[attr-defined]
            raise RegistrationError(
                f"{where}: stdin_records={record.__qualname__} must be frozen: records are values"
            )
        hints = type_hints(record)
        fields: list[RecordField] = []
        for f in dataclasses.fields(record):
            if not f.init:
                continue
            try:
                target = classify(hints[f.name], scalars)
            except SchemaError as exc:
                raise RegistrationError(
                    f"{where}: stdin_records {record.__qualname__}.{f.name}: {exc}"
                ) from None
            defaulted = (
                f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING
            )
            fields.append(RecordField(f.name, target, not defaulted and not target.optional))
        return cls(record, tuple(fields), schema_for(record, scalars))

    def has(self, name: str) -> bool:
        """Whether the record type has a field of that name"""
        return any(f.name == name for f in self.fields)

    def build(self, value: object, line: int) -> object:
        """One record from a JSON value, or ``RECORD_INVALID`` naming the line and field"""
        if not isinstance(value, dict):
            raise _invalid(line, None, f"line {line} is a JSON {_kind(value)}, not an object")
        known = {f.name: f for f in self.fields}
        values: dict[str, object] = {}
        for key, raw in value.items():
            if key in _TRUST_KEYS:
                continue
            spec = known.get(key)
            if spec is None:
                raise _invalid(
                    line,
                    key,
                    f"line {line} has {key!r}, which {self.cls.__qualname__} has no field for",
                    suggestion="drop the key, or have the producer keep only these fields "
                    f"with --fields {','.join(known)}",
                )
            values[key] = _value(spec, raw, line)
        missing = [f.name for f in self.fields if f.required and f.name not in values]
        if missing:
            raise _invalid(line, missing[0], f"line {line} lacks {', '.join(missing)}")
        for f in self.fields:
            if f.name not in values and f.target.optional and not _has_default(self.cls, f.name):
                values[f.name] = None
        try:
            return self.cls(**values)
        except ParseError as exc:
            raise _invalid(line, exc.field, f"line {line}: {exc.message}") from None
        except InvalidValue as exc:
            raise _invalid(line, None, f"line {line}: {exc}") from None


def _has_default(cls: type, name: str) -> bool:
    f = next(f for f in dataclasses.fields(cls) if f.name == name)
    return f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING


def _value(spec: RecordField, raw: object, line: int) -> object:
    target = spec.target
    if raw is None:
        if target.optional:
            return None
        raise _invalid(line, spec.name, f"line {line}: {spec.name!r} is null")
    try:
        if target.flag_type is FlagType.ARRAY:
            item = target.item
            if not isinstance(raw, list) or item is None:
                raise ParseError(f"{spec.name!r} expects an array")
            items = [_scalar(item, v, spec.name) for v in raw]
            return list(items) if typing.get_origin(target.base) is list else tuple(items)
        return _scalar(target, raw, spec.name)
    except ParseError as exc:
        raise _invalid(line, spec.name, f"line {line}: {exc.message}") from None


def _scalar(target: Classified, raw: object, name: str) -> object:
    base = check_json_base(target, raw, name)
    if target.scalar is None:
        return base
    return apply_scalar(target.scalar, base, name)


def _kind(value: object) -> str:
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, bool):
        return "boolean"
    return "null" if value is None else "number"


def _invalid(
    line: int, field: str | None, message: str, *, suggestion: str | None = None
) -> CliExit:
    context: dict[str, object] = {"line": line}
    if field is not None:
        context["field"] = field
    return CliExit(
        _GENERAL,
        message,
        code=RECORD_INVALID,
        context=context,
        suggestion=suggestion or "fix the producer, or pass only its records that match",
    )


class Records(Iterator[object]):
    """``ctx.stdin_records``: each record of the input, built as the handler iterates"""

    def __init__(self, lines: Lines, spec: RecordSpec, warn: WarnSink) -> None:
        self._lines = lines
        self._spec = spec
        self._warn = warn
        self._queue: deque[tuple[int, object]] = deque()
        self._ended = False
        self._events = 0
        """Stream events read, whose terminal envelope must follow"""

    def __iter__(self) -> Records:
        return self

    def __next__(self) -> object:
        while not self._queue:
            if self._ended:
                raise StopIteration
            self._read()
        line, value = self._queue.popleft()
        return self._spec.build(value, line)

    def _read(self) -> None:
        """Queue the records of the next line, or end the input"""
        try:
            text = next(self._lines)
        except StopIteration:
            self._ended = True
            if self._events:
                raise CliExit(
                    _GENERAL,
                    f"the input stopped after {self._events} stream events without its "
                    "terminal envelope: the producer did not finish",
                    code=UPSTREAM_INCOMPLETE,
                    context={"line": self._lines.number, "events": self._events},
                    suggestion="rerun the producer; its last records may be missing",
                ) from None
            raise
        line = self._lines.number
        if not text.strip():
            return
        try:
            # As exec reads a plan line: a Decimal field keeps the number as written
            value = loads_strict(text)
        except ValueError as exc:
            raise _invalid(line, None, f"line {line} is not JSON: {exc}") from None
        if not isinstance(value, dict) or not _is_envelope(value):
            if isinstance(value, dict) and _is_heartbeat(value):
                return
            if isinstance(value, dict) and value.get(SUMMARY_KEY) is True:
                # REQ-O-004's terminal line after bare items: never a record
                self._ended = True
                self._lines.close()
                return
            if isinstance(value, dict) and SEQ_KEY in value and not self._spec.has(SEQ_KEY):
                # A numbered stream's item line: _seq is the stream's, not the record's,
                # and the stream owes its terminal line (REQ-O-004). A record type with a
                # _seq field of its own is never numbered, so there it is the field
                value = {k: v for k, v in value.items() if k != SEQ_KEY}
                self._events += 1
            self._queue.append((line, value))
            return
        self._envelope(value, line)

    def _envelope(self, envelope: dict[str, object], line: int) -> None:
        meta = envelope["meta"]
        assert isinstance(meta, dict)  # _is_envelope
        if envelope["ok"] is not True:
            self._ended = True
            self._lines.close()
            raise _upstream_failed(envelope, meta, line)
        self._truncated(envelope, meta, line)
        data = envelope["data"]
        items = data if isinstance(data, list) else [] if data is None else [data]
        self._queue.extend((line, item) for item in items)
        if meta.get("end") is True or "seq" not in meta:
            self._ended = True  # a stream's last line, or a whole response
            self._lines.close()
        else:
            self._events += 1

    def _truncated(self, envelope: dict[str, object], meta: dict[str, object], line: int) -> None:
        pagination = meta.get("pagination")
        more = isinstance(pagination, dict) and pagination.get("has_more") is True
        warnings = envelope["warnings"]
        cut = isinstance(warnings, list) and any(
            isinstance(w, dict) and w.get("code") == "FIELD_TRUNCATED" for w in warnings
        )
        if more or cut:
            why = "is one page of more" if more else "had fields cut to fit its output cap"
            self._warn(
                UPSTREAM_TRUNCATED,
                f"the upstream response on line {line} {why}; the records read are not all",
                {"line": line, "has_more": more, "field_truncated": cut},
            )


def _is_envelope(value: dict[str, object]) -> bool:
    return (
        value.keys() == ENVELOPE_KEYS
        and isinstance(value["ok"], bool)
        and isinstance(value["meta"], dict)
    )


def _is_heartbeat(value: dict[str, object]) -> bool:
    keys = value.keys() - {_HEARTBEAT_STEP}
    return keys == _HEARTBEAT_KEYS and value["heartbeat"] is True


def _upstream_failed(envelope: dict[str, object], meta: dict[str, object], line: int) -> CliExit:
    """``UPSTREAM_FAILED`` with the upstream error, secret-named keys redacted and
    high-entropy values masked, as they would be in this command's own ``data``"""
    error = envelope["error"]
    # An undeclared object: no output adapter can apply to it
    upstream = protect(scrub("", error), object, unmask=False, adapters=OutputAdapters()).data
    code = error.get("code") if isinstance(error, dict) else None
    exit_code = meta.get("exit_code")
    context: dict[str, object] = {"line": line, "upstream": upstream}
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        context["upstream_exit_code"] = exit_code
    command = meta.get("command")
    if isinstance(command, str):
        context["upstream_command"] = command
    return CliExit(
        _GENERAL,
        f"the upstream command failed with {code if isinstance(code, str) else 'an error'}",
        code=UPSTREAM_FAILED,
        context=context,
        suggestion="fix the upstream failure in context.upstream, then rerun the pipeline",
    )
