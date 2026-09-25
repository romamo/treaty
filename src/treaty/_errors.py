"""Exception hierarchy.

``TreatyError`` and its subclasses mean the framework was misused and are raised
at import or registration time. ``ParseError`` and ``CliExit`` are the two ways a
run ends with a non-zero exit and an envelope.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from ._values import ExitCodeName, InvalidValue


class TreatyError(Exception):
    """Framework misuse detected at registration or build time"""


class RegistrationError(TreatyError):
    """A command, flag, or exit code declaration violates the contract"""


class SchemaError(TreatyError):
    """A type cannot be expressed as JSON Schema or serialized to JSON"""


class ParseError(Exception):
    """Argument parsing failed before the handler ran; maps to ``ARG_ERROR``

    ``errors`` holds the individual failures when the parser collected several
    in one run (REQ-F-015); a single failure is its own only item.
    """

    def __init__(
        self,
        message: str,
        *,
        context: Mapping[str, object] | None = None,
        suggestion: str | None = None,
        errors: Sequence[ParseError] = (),
    ) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, object] = dict(context or {})
        self.suggestion = suggestion
        self.errors: tuple[ParseError, ...] = tuple(errors)

    @classmethod
    def combine(cls, errors: Sequence[ParseError]) -> ParseError:
        """One error carrying all of ``errors``; a single error is returned unchanged"""
        if len(errors) == 1:
            return errors[0]
        fields = [e.field for e in errors if e.field is not None]
        return cls(
            f"Validation failed: {len(errors)} errors",
            context={"error_count": len(errors), "fields": fields},
            suggestion="fix every entry in errors, then reissue once",
            errors=errors,
        )

    @property
    def field(self) -> str | None:
        """The flag, field, or argument this error is about, when it is about one"""
        for key in ("flag", "field", "argument"):
            value = self.context.get(key)
            if isinstance(value, str):
                return value
        return None

    def items(self) -> list[dict[str, object]]:
        """The ``errors`` entries for the envelope: every collected error, or this one"""
        out: list[dict[str, object]] = []
        for e in self.errors or (self,):
            item: dict[str, object] = {"message": e.message}
            if e.field is not None:
                item["field"] = e.field
            if e.context:
                item["context"] = dict(e.context)
            if e.suggestion is not None:
                item["suggestion"] = e.suggestion
            out.append(item)
        return out


class CliExit(Exception):
    """Raised by a handler to end the run with a declared exit code

    ``data`` must have the handler's return type: the command's human renderer
    receives it on failed runs too.
    """

    def __init__(
        self,
        name: ExitCodeName,
        message: str,
        *,
        code: str | None = None,
        detail: str | None = None,
        context: Mapping[str, object] | None = None,
        suggestion: str | None = None,
        fix_command: str | None = None,
        fix_required: str | None = None,
        retry_after_ms: int | None = None,
        data: object = None,
    ) -> None:
        super().__init__(message)
        self.name = name
        self.message = message
        self.code = code if code is not None else name.value
        self.detail = detail
        self.context: dict[str, object] = dict(context or {})
        self.suggestion = suggestion
        self.fix_command = fix_command
        self.fix_required = fix_required
        self.retry_after_ms = retry_after_ms
        self.data = data


class _ExitFactory:
    """``Exit.CONFLICT("message", context=...)`` builds a ``CliExit`` by name"""

    def __getattr__(self, name: str) -> Callable[..., CliExit]:
        # hasattr(), copy, pickle, and inspect probe dunders; they must see AttributeError
        try:
            exit_name = ExitCodeName(name)
        except InvalidValue as exc:
            raise AttributeError(f"Exit has no exit code {name!r}: {exc}") from None

        def make(message: str, **kwargs: object) -> CliExit:
            return CliExit(exit_name, message, **kwargs)  # type: ignore[arg-type]

        return make


Exit = _ExitFactory()
