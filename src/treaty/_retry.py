"""Internal retries a command declares, counted into ``meta.retries`` (REQ-F-078).

A handler wraps a flaky call in ``ctx.retry(fn)``; the command's ``retry=Retry(...)``
says how often, on what, and how the wait grows, and ``--retries`` and ``--retry-delay``
let an agent change the budget to fit its own. The command's timeout bounds every attempt
together: no delay sleeps past the deadline.
"""

from __future__ import annotations

import math
import random
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, TypeVar

from ._errors import CliExit, ParseError, RegistrationError
from ._page import whole_number
from ._values import ExitCodeName, InvalidValue

RETRIES_FLAG = "retries"
RETRY_DELAY_FLAG = "retry-delay"
MAX_DELAY_MS = 3_600_000
MAX_RETRY_AFTER_MS = 60_000
"""A longer ``Retry-After`` ends the run with it, for the caller to wait, rather than sleep"""

T = TypeVar("T")


def _number(name: str, value: object, *, low: float, high: float | None) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < low
        or (high is not None and value > high)
    ):
        bound = f"from {low} to {high}" if high is not None else f"of at least {low}"
        raise RegistrationError(f"Retry({name}=...) is a number {bound}, not {value!r}")


@dataclass(frozen=True, slots=True)
class Retry:
    """``retry=`` of a command: ``ctx.retry`` calls again, up to ``retries`` times, when
    the call raises one of ``on`` or ``retry_if`` holds for its result; after the last,
    the run exits with ``exhausted``"""

    retries: int = 3
    delay_ms: int = 500
    """The first wait; ``--retry-delay`` replaces it"""
    on: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError)
    exhausted: str = "UNAVAILABLE"
    """A declared exit code of the command"""
    backoff: float = 1.0
    """Each wait is the one before times ``backoff``; 1.0 waits ``delay_ms`` every time"""
    max_delay_ms: int | None = None
    """The longest one wait may be; None caps it at an hour, as ``--retry-delay`` is"""
    jitter: float = 0.0
    """Moves each wait by up to this fraction of it either way, so callers spread out"""
    retry_if: Callable[[Any], object] | None = None
    """Called with what the call returned: a truthy answer retries it, as ``on`` does"""

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
        _number("backoff", self.backoff, low=1.0, high=None)
        _number("jitter", self.jitter, low=0.0, high=1.0)
        cap = self.max_delay_ms
        if cap is not None and (
            isinstance(cap, bool)
            or not isinstance(cap, int)
            or not self.delay_ms <= cap <= MAX_DELAY_MS
        ):
            raise RegistrationError(
                f"Retry(max_delay_ms=...) is whole milliseconds from delay_ms "
                f"({self.delay_ms}) to an hour, not {cap!r}"
            )
        if self.retry_if is not None and not callable(self.retry_if):
            raise RegistrationError(
                f"Retry(retry_if=...) is a function of the result, not {self.retry_if!r}"
            )


def cap_ms(policy: Retry) -> int:
    """The longest one wait of ``policy`` may be"""
    return MAX_DELAY_MS if policy.max_delay_ms is None else policy.max_delay_ms


def waits_ms(policy: Retry, retries: int, delay_ms: int) -> Iterator[float]:
    """The wait before each of ``retries`` retries, before jitter: ``delay_ms``, times
    ``backoff`` after each, never over the cap"""
    cap = cap_ms(policy)
    wait = float(min(delay_ms, cap))
    for _ in range(retries):
        yield wait
        wait = min(wait * policy.backoff, cap)


def budget_ms(policy: Retry) -> float:
    """The most ``policy`` may sleep in one run with its declared defaults: every wait at
    its cap and moved the full ``jitter`` up"""
    cap = cap_ms(policy)
    waits = waits_ms(policy, policy.retries, policy.delay_ms)
    return sum(min(w * (1 + policy.jitter), cap) for w in waits)


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


def retry_after(exc: BaseException) -> float | None:
    """The seconds a raised ``CliExit`` asks for in ``retry_after_ms``, such as an
    upstream's ``Retry-After``: the next wait is at least that. One over
    ``MAX_RETRY_AFTER_MS`` is too long to sleep, so the run ends with the error instead"""
    if isinstance(exc, CliExit):
        after = exc.retry_after_ms
        if isinstance(after, (int, float)) and not isinstance(after, bool) and after >= 0:
            return math.inf if after > MAX_RETRY_AFTER_MS else after / 1000
    return None


class RetriesExhausted(CliExit):
    """``ctx.retry`` gave up: the declared ``exhausted`` code, never retryable once the
    tool itself retried, so an agent does not retry on top (REQ-F-078)"""

    def __init__(
        self, policy: Retry, message: str, retried: int, context: dict[str, object]
    ) -> None:
        super().__init__(
            ExitCodeName(policy.exhausted),
            f"{message} (after {retried} retries)",
            context={**context, "retries": retried},
        )
        self.retried = retried

    @classmethod
    def raised(cls, policy: Retry, cause: BaseException, retried: int) -> RetriesExhausted:
        """The last attempt raised one of ``Retry.on``"""
        context: dict[str, object] = {"exception": type(cause).__qualname__}
        return cls(policy, f"{type(cause).__name__}: {cause}", retried, context)

    @classmethod
    def returned(cls, policy: Retry, result: object, retried: int) -> RetriesExhausted:
        """``Retry.retry_if`` still held for what the last attempt returned"""
        kind = type(result).__qualname__
        message = f"Retry(retry_if=...) still held for the {kind} returned"
        return cls(policy, message, retried, {"result": kind})


class Retrier:
    """``ctx.retry`` of one command run; ``count`` is every retry it made, for meta.
    ``rng`` and ``sleep`` are the jitter source and the wait, which a test replaces"""

    def __init__(
        self,
        policy: Retry,
        *,
        retries: int,
        delay_ms: int,
        deadline: float | None,
        rng: Callable[[], float] = random.random,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.policy = policy
        self.retries = retries
        self.delay_ms = delay_ms
        self.deadline = deadline
        self.rng = rng
        self.sleep = sleep
        self.count = 0

    def call(
        self,
        fn: Callable[[], T],
        *,
        on: tuple[type[BaseException], ...] | None = None,
        give_up: Callable[[BaseException, int], BaseException] | None = None,
        wait: Callable[[BaseException], float | None] = retry_after,
    ) -> T:
        """``fn`` under the budget; ``on`` and ``give_up`` replace the policy's exceptions
        and ``RetriesExhausted`` and leave ``retry_if`` out, for ``ctx.http``, whose
        failures carry their own error. ``wait`` is the seconds a failure asks for, a
        ``CliExit``'s ``retry_after_ms`` unless replaced: the delay is at least that, and a
        wait too long to sleep gives up with the failure itself."""
        retried = 0
        waits = waits_ms(self.policy, self.retries, self.delay_ms)
        retry_on = self.policy.on if on is None else on
        retry_if = self.policy.retry_if if on is None else None
        while True:
            try:
                result = fn()
            except retry_on as exc:
                asked = wait(exc)
                delay = self._delay(next(waits, None), asked)
                if delay is None:
                    if give_up is not None:
                        raise give_up(exc, retried) from exc
                    if asked == math.inf:
                        raise
                    raise RetriesExhausted.raised(self.policy, exc, retried) from exc
            else:
                if retry_if is None or not retry_if(result):
                    return result
                delay = self._delay(next(waits, None), None)
                if delay is None:
                    raise RetriesExhausted.returned(self.policy, result, retried)
            self.sleep(delay)
            retried += 1
            self.count += 1

    def _delay(self, planned_ms: float | None, asked: float | None) -> float | None:
        """Seconds to sleep before the next attempt: the planned wait with jitter, or what
        the failure asked for when that is longer; None once the retries are spent, when
        the wait is too long to sleep, or when it would end past the deadline"""
        if planned_ms is None:
            return None
        spread = self.policy.jitter * (2 * self.rng() - 1)
        seconds = min(planned_ms * (1 + spread), cap_ms(self.policy)) / 1000
        if asked is not None:
            seconds = max(seconds, asked)
        if seconds * 1000 > MAX_DELAY_MS:
            return None
        if self.deadline is not None and time.monotonic() + seconds >= self.deadline:
            return None
        return seconds
