"""Internal retries a command declares, counted into ``meta.retries`` (REQ-F-078).

A handler wraps a flaky call in ``ctx.retry(fn)``; the command's ``retry=Retry(...)``
says how often and on what, and ``--retries`` and ``--retry-delay`` let an agent change
the budget to fit its own. The command's timeout bounds every attempt together: no
delay sleeps past the deadline.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from ._errors import CliExit, ParseError, RegistrationError
from ._page import whole_number
from ._values import ExitCodeName, InvalidValue

RETRIES_FLAG = "retries"
RETRY_DELAY_FLAG = "retry-delay"
MAX_DELAY_MS = 3_600_000

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Retry:
    """``retry=`` of a command: ``ctx.retry`` calls again, up to ``retries`` times, when
    the call raises one of ``on``; after the last, the run exits with ``exhausted``"""

    retries: int = 3
    delay_ms: int = 500
    on: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError)
    exhausted: str = "UNAVAILABLE"
    """A declared exit code of the command"""

    def __post_init__(self) -> None:
        for name, value in (("retries", self.retries), ("delay_ms", self.delay_ms)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RegistrationError(f"Retry({name}=...) is a whole number, not {value!r}")
        if not self.on or not all(
            isinstance(e, type) and issubclass(e, BaseException) for e in self.on
        ):
            raise RegistrationError("Retry(on=...) is a tuple of exception classes")
        try:
            ExitCodeName(self.exhausted)
        except InvalidValue as exc:
            raise RegistrationError(f"Retry(exhausted=...): {exc}") from None


_DURATION = re.compile(r"(\d{1,10})(ms|s)?")


def parse_delay(raw: object) -> int:
    """``--retry-delay``: ``500ms``, ``2s``, or whole milliseconds, as milliseconds"""
    expects = "a duration such as 500ms or 2s"
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        ms = raw
    else:
        match = _DURATION.fullmatch(raw) if isinstance(raw, str) else None
        if match is None:
            raise ParseError(
                f"'{RETRY_DELAY_FLAG}' expects {expects}",
                context={"flag": RETRY_DELAY_FLAG, "value": raw if isinstance(raw, str) else None},
            )
        ms = int(match[1]) * (1000 if match[2] == "s" else 1)
    if ms > MAX_DELAY_MS:
        raise ParseError(
            f"'{RETRY_DELAY_FLAG}' is at most an hour", context={"flag": RETRY_DELAY_FLAG}
        )
    return ms


def parse_retries(raw: object) -> int:
    """``--retries``: a whole number; 0 turns retries off"""
    expects = "a whole number of retries; 0 turns them off"
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    if isinstance(raw, str):
        return whole_number(raw, RETRIES_FLAG, expects)
    raise ParseError(f"'{RETRIES_FLAG}' expects {expects}", context={"flag": RETRIES_FLAG})


class RetriesExhausted(CliExit):
    """``ctx.retry`` gave up: the declared ``exhausted`` code, never retryable once the
    tool itself retried, so an agent does not retry on top (REQ-F-078)"""

    def __init__(self, policy: Retry, cause: BaseException, retried: int) -> None:
        super().__init__(
            ExitCodeName(policy.exhausted),
            f"{type(cause).__name__}: {cause} (after {retried} retries)",
            context={"exception": type(cause).__qualname__, "retries": retried},
        )
        self.retried = retried


class Retrier:
    """``ctx.retry`` of one command run; ``count`` is every retry it made, for meta"""

    def __init__(
        self, policy: Retry, *, retries: int, delay_ms: int, deadline: float | None
    ) -> None:
        self.policy = policy
        self.retries = retries
        self.delay = delay_ms / 1000
        self.deadline = deadline
        self.count = 0

    def call(
        self,
        fn: Callable[[], T],
        *,
        on: tuple[type[BaseException], ...] | None = None,
        give_up: Callable[[BaseException, int], BaseException] | None = None,
    ) -> T:
        """``fn`` under the budget; ``on`` and ``give_up`` replace the policy's exceptions
        and ``RetriesExhausted``, for ``ctx.http``, whose failures carry their own error"""
        retried = 0
        retry_on = self.policy.on if on is None else on
        while True:
            try:
                return fn()
            except retry_on as exc:
                late = self.deadline is not None and time.monotonic() + self.delay >= self.deadline
                if retried >= self.retries or late:
                    if give_up is not None:
                        raise give_up(exc, retried) from exc
                    raise RetriesExhausted(self.policy, exc, retried) from exc
            time.sleep(self.delay)
            retried += 1
            self.count += 1
