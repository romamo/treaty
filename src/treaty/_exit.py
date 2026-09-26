"""Exit code entries and the per-app registry.

Framework codes 0 to 13 are pre-registered from the spec's ``exit-code.json``.
Command-specific codes must fall in 79 to 125.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum

from ._errors import RegistrationError
from ._values import ExitCode, ExitCodeName


class SideEffects(StrEnum):
    NONE = "none"
    PARTIAL = "partial"
    COMPLETE = "complete"


class FrameworkCode(IntEnum):
    SUCCESS = 0
    GENERAL_ERROR = 1
    ARG_ERROR = 2
    PARTIAL_FAILURE = 3
    PRECONDITION = 4
    NOT_FOUND = 5
    CONFLICT = 6
    PERMISSION_DENIED = 7
    AUTH_REQUIRED = 8
    PAYMENT_REQUIRED = 9
    TIMEOUT = 10
    RATE_LIMITED = 11
    UNAVAILABLE = 12
    REDIRECTED = 13


@dataclass(frozen=True, slots=True)
class ExitCodeEntry:
    name: ExitCodeName
    code: ExitCode
    description: str
    retryable: bool
    side_effects: SideEffects

    def __post_init__(self) -> None:
        if self.retryable and self.side_effects is not SideEffects.NONE:
            raise RegistrationError(
                f"{self.name}: retryable exit codes must declare side_effects 'none'"
            )
        if not 1 <= len(self.description) <= 120:
            raise RegistrationError(f"{self.name}: description must be 1..120 characters")
        if self.description.endswith("."):
            raise RegistrationError(f"{self.name}: description must not end with a period")
        if not (self.code.is_framework or self.code.is_command_specific or self.code.is_signal):
            raise RegistrationError(
                f"{self.name}: code {self.code.value} must be 0..13 (framework) or 79..125"
            )

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name.value,
            "description": self.description,
            "retryable": self.retryable,
            "side_effects": self.side_effects.value,
        }


_FRAMEWORK: tuple[tuple[FrameworkCode, str, bool, SideEffects], ...] = (
    (FrameworkCode.SUCCESS, "Operation completed as intended", False, SideEffects.NONE),
    (
        FrameworkCode.GENERAL_ERROR,
        "Unclassified execution failure; inspect state before retrying",
        False,
        SideEffects.PARTIAL,
    ),
    (
        FrameworkCode.ARG_ERROR,
        "Input validation failed before any side effect occurred",
        False,
        SideEffects.NONE,
    ),
    (
        FrameworkCode.PARTIAL_FAILURE,
        "Operation started but did not complete; external state may be partially modified",
        False,
        SideEffects.PARTIAL,
    ),
    (
        FrameworkCode.PRECONDITION,
        "A required precondition was not satisfied",
        False,
        SideEffects.NONE,
    ),
    (FrameworkCode.NOT_FOUND, "The addressed resource does not exist", False, SideEffects.NONE),
    (
        FrameworkCode.CONFLICT,
        "The resource already exists or a version conflict was detected",
        False,
        SideEffects.NONE,
    ),
    (
        FrameworkCode.PERMISSION_DENIED,
        "The caller has valid credentials but lacks permission",
        False,
        SideEffects.NONE,
    ),
    (
        FrameworkCode.AUTH_REQUIRED,
        "Credentials are missing, invalid, or expired",
        False,
        SideEffects.NONE,
    ),
    (FrameworkCode.PAYMENT_REQUIRED, "A payment is required to proceed", False, SideEffects.NONE),
    (
        FrameworkCode.TIMEOUT,
        "The operation exceeded its time limit; external state may be partially modified",
        False,
        SideEffects.PARTIAL,
    ),
    (
        FrameworkCode.RATE_LIMITED,
        "An upstream rate limit was hit; retry after error.retry_after_ms",
        True,
        SideEffects.NONE,
    ),
    (
        FrameworkCode.UNAVAILABLE,
        "The service is temporarily unavailable; retry with exponential back-off",
        True,
        SideEffects.NONE,
    ),
    (
        FrameworkCode.REDIRECTED,
        "The command or flag does not exist at this path; read error.redirect",
        False,
        SideEffects.NONE,
    ),
)


_SIGNALS: tuple[tuple[str, int, str], ...] = (
    ("CANCELLED_SIGINT", 130, "Cancelled by SIGINT; external state may be partially modified"),
    ("CANCELLED_SIGTERM", 143, "Cancelled by SIGTERM; external state may be partially modified"),
    (
        "OUTPUT_CLOSED",
        141,
        "The reader closed stdout (SIGPIPE); output was cut short, state may be partial",
    ),
)


def framework_entries() -> tuple[ExitCodeEntry, ...]:
    framework = tuple(
        ExitCodeEntry(ExitCodeName(code.name), ExitCode(code.value), desc, retryable, effects)
        for code, desc, retryable, effects in _FRAMEWORK
    )
    signals = tuple(
        ExitCodeEntry(ExitCodeName(name), ExitCode(code), desc, False, SideEffects.PARTIAL)
        for name, code, desc in _SIGNALS
    )
    return framework + signals


class ExitCodeRegistry:
    """Name and integer lookups for every exit code an app may emit"""

    def __init__(self) -> None:
        self._by_name: dict[ExitCodeName, ExitCodeEntry] = {}
        self._by_code: dict[ExitCode, ExitCodeEntry] = {}
        for entry in framework_entries():
            self._add(entry)

    def _add(self, entry: ExitCodeEntry) -> None:
        if entry.name in self._by_name:
            raise RegistrationError(f"exit code name {entry.name} already registered")
        if entry.code in self._by_code:
            raise RegistrationError(f"exit code {entry.code.value} already registered")
        self._by_name[entry.name] = entry
        self._by_code[entry.code] = entry

    def register(self, entry: ExitCodeEntry) -> ExitCodeEntry:
        if not entry.code.is_command_specific:
            raise RegistrationError(f"{entry.name}: command-specific exit codes must be in 79..125")
        self._add(entry)
        return entry

    def __contains__(self, name: ExitCodeName) -> bool:
        return name in self._by_name

    def by_name(self, name: ExitCodeName) -> ExitCodeEntry:
        try:
            return self._by_name[name]
        except KeyError:
            raise RegistrationError(f"unknown exit code name {name}") from None

    def framework(self, code: FrameworkCode) -> ExitCodeEntry:
        return self._by_code[ExitCode(code.value)]

    def by_code(self, code: int) -> ExitCodeEntry:
        try:
            return self._by_code[ExitCode(code)]
        except KeyError:
            raise RegistrationError(f"unknown exit code {code}") from None
