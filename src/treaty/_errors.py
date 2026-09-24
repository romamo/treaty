"""Exception hierarchy.

``TreatyError`` and its subclasses mean the framework was misused and are raised
at import or registration time. ``ParseError`` and ``CliExit`` are the two ways a
run ends with a non-zero exit and an envelope.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from ._values import ExitCodeName


class TreatyError(Exception):
    """Framework misuse detected at registration or build time"""


class RegistrationError(TreatyError):
    """A command, flag, or exit code declaration violates the contract"""


class SchemaError(TreatyError):
    """A type cannot be expressed as JSON Schema or serialized to JSON"""


class ParseError(Exception):
    """Argument parsing failed before the handler ran; maps to ``ARG_ERROR``"""

    def __init__(self, message: str, *, context: Mapping[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, object] = dict(context or {})


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
        exit_name = ExitCodeName(name)

        def make(message: str, **kwargs: object) -> CliExit:
            return CliExit(exit_name, message, **kwargs)  # type: ignore[arg-type]

        return make


Exit = _ExitFactory()
