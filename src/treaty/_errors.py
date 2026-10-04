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

    at: tuple[str | int, ...] = ()
    """Where in a value being serialized it failed: the keys and indexes from the root,
    empty at the root or for a type; ``treaty._schema.value_path`` spells it"""


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
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        """``error.code`` when this is the only error; ``ARG_ERROR`` when None"""
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
            if e.code is not None:
                item["code"] = e.code
            if e.field is not None:
                item["field"] = e.field
            if e.context:
                item["context"] = dict(e.context)
            if e.suggestion is not None:
                item["suggestion"] = e.suggestion
            out.append(item)
        return out


class UserCodeError(Exception):
    """User code the framework calls outside the handler raised: a ``cleanup=`` or
    ``release`` hook, ``rollback=``, or a settings ``__post_init__``"""

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


def user_code[T](call: Callable[[], T]) -> T:
    """Call user code; whatever it raises comes back as ``UserCodeError``. The one
    ``except Exception`` for user code besides the handler boundary in ``_app``."""
    try:
        return call()
    except Exception as exc:  # noqa: BLE001 - the user-code boundary
        raise UserCodeError(exc) from exc


class ArgsCrashed(Exception):
    """The args ``__post_init__`` raised something other than ``ParseError`` or
    ``InvalidValue``: a bug in user code, reported as ``HANDLER_CRASHED`` (exit 1)"""

    def __init__(self, cause: Exception, values: Mapping[str, object]) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.values = dict(values)
        """The field values it was given, so the crash report can redact secrets"""


class ArgsRefused(ParseError):
    """Phase 1 refused the arguments once every field had been read: an error of the
    args ``__post_init__`` or of a field, with the field values it was given. User code
    may quote a value in its message, so the envelope is redacted of the secrets among
    them, as a handler's error is"""

    def __init__(self, error: ParseError, values: Mapping[str, object]) -> None:
        super().__init__(
            error.message,
            context=error.context,
            suggestion=error.suggestion,
            errors=error.errors,
            code=error.code,
        )
        self.values = dict(values)
        """The field values read, so the envelope can redact the secrets among them"""


class CliExit(Exception):
    """Raised by a handler to end the run with a declared exit code

    ``data`` must have the handler's return type. In plain mode the command's renderer
    receives it on failed runs too, converted to JSON values like a successful result.
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
        retry_strategy: str | None = None,
        conflict_id: str | None = None,
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
        self.retry_strategy = retry_strategy
        """A ``treaty.RetryStrategy`` value; the exit code's default when None"""
        self.conflict_id = conflict_id
        """The id of the resource that already exists (REQ-C-028)"""
        self.data = data


def already_exists(existing: object, *, conflict_id: str, message: str | None = None) -> CliExit:
    """The create-or-get answer (REQ-C-028): exit 6 ``CONFLICT`` with code
    ``ALREADY_EXISTS`` and the existing resource as ``data``, so an agent retrying a create
    reads the resource from the failure without a ``get``. ``existing`` has the handler's
    return type, as ``data`` of any ``CliExit`` does."""
    return CliExit(
        ExitCodeName("CONFLICT"),
        message if message is not None else f"{conflict_id} already exists",
        code="ALREADY_EXISTS",
        context={"conflict_id": conflict_id},
        conflict_id=conflict_id,
        suggestion="use data, the existing resource, as the result of the create",
        data=existing,
    )


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
