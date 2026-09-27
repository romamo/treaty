"""Multi-step commands: the step manifest and its tracking (REQ-C-008, O-010, O-011).

A command declares ``steps=["backup", "apply_schema", ...]`` and its handler calls
``ctx.step(name)`` before each one. A call completes the step in progress and starts the
next; the last completes when the handler returns. Every response of the command says
which steps completed, which failed, and which were skipped, in ``data``.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ._values import InvalidValue

if TYPE_CHECKING:
    from ._context import Ctx

_STEP_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")

Rollback = Callable[[Any, "Ctx", "tuple[StepName, ...]"], None]
"""``rollback(args, ctx, completed)``: undo the completed steps, newest first"""

StepLog = Callable[[str, Mapping[str, object]], None]

STEP_KEYS = frozenset(
    {
        "completed_steps",
        "failed_step",
        "skipped_steps",
        "partial",
        "resume_from",
        "rollback_status",
        "rollback_error",
    }
)
"""``data`` keys a ``steps=`` command's responses carry, which its output cannot declare"""


@dataclass(frozen=True, slots=True)
class StepName:
    """One declared step, such as ``apply_schema``"""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not _STEP_RE.fullmatch(self.value):
            raise InvalidValue(
                f"step name {self.value!r} is lowercase letters, digits, '_', and '-', "
                "starting with a letter"
            )

    def __str__(self) -> str:
        return self.value


class RollbackStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    NOT_ATTEMPTED = "not_attempted"


class StepError(Exception):
    """``ctx.step`` broke the declared order: ``INVALID_STEP``, exit 1"""


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Where a run stands: the step fields of its response"""

    completed: tuple[StepName, ...]
    current: StepName | None
    skipped: tuple[StepName, ...]
    """Declared steps neither completed nor in progress"""


class StepTracker:
    """One run's progress through its declared steps

    The handler's thread advances it; the main thread reads it when the run times out or
    is cancelled, so every read and write holds the lock.
    """

    def __init__(
        self,
        steps: Sequence[StepName],
        log: StepLog,
        *,
        resume_from: StepName | None = None,
        rollback: Rollback | None = None,
    ) -> None:
        self.steps = tuple(steps)
        self._log = log
        self._start = 0 if resume_from is None else self.steps.index(resume_from)
        self._rollback = rollback
        """The command's rollback hook when ``--rollback-on-failure`` asks for it"""
        self._lock = threading.Lock()
        self._current: int | None = None
        self._last = -1
        self._completed: list[StepName] = []
        self.rollback_status = RollbackStatus.NOT_ATTEMPTED
        self.rollback_failure: Exception | None = None

    def step(self, name: str) -> bool:
        """``ctx.step``: complete the step in progress and start ``name``; False for a step
        before ``--resume-from``, which the handler skips"""
        index = next((i for i, s in enumerate(self.steps) if s.value == name), None)
        if index is None:
            declared = ", ".join(s.value for s in self.steps)
            raise StepError(f"ctx.step({name!r}) names no declared step; steps= are {declared}")
        with self._lock:
            if index <= self._last:
                again = "again" if index == self._last else "after a later step"
                raise StepError(f"ctx.step({name!r}) was called {again}; steps run in order")
            self._last = index
            done = self._complete()
            runs = index >= self._start
            if runs:
                self._current = index
        if done is not None:
            self._event("step completed", done)
        if runs:
            self._event("step started", index)
        return runs

    def finish(self) -> None:
        """The handler returned: the step in progress completed"""
        with self._lock:
            done = self._complete()
        if done is not None:
            self._event("step completed", done)

    @property
    def current(self) -> StepName | None:
        with self._lock:
            return None if self._current is None else self.steps[self._current]

    def snapshot(self) -> Snapshot:
        with self._lock:
            completed = tuple(self._completed)
            current = None if self._current is None else self.steps[self._current]
        skipped = tuple(s for s in self.steps if s not in completed and s != current)
        return Snapshot(completed, current, skipped)

    def roll_back(self, args: object, ctx: Ctx) -> None:
        """A step failed: undo the completed ones, newest first, when asked to"""
        completed = self.snapshot().completed
        if self._rollback is None or not completed:
            return
        try:
            self._rollback(args, ctx, completed[::-1])
        except Exception as exc:  # noqa: BLE001 - rollback= is user code
            self.rollback_status = RollbackStatus.FAILED
            self.rollback_failure = exc
            return
        self.rollback_status = RollbackStatus.COMPLETED

    def _complete(self) -> int | None:
        """Under the lock: move the step in progress to completed"""
        index, self._current = self._current, None
        if index is not None:
            self._completed.append(self.steps[index])
        return index

    def _event(self, message: str, index: int) -> None:
        fields = {"step": self.steps[index].value, "index": index + 1, "total": len(self.steps)}
        self._log(message, fields)
