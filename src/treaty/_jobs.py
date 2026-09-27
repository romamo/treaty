"""Async jobs: a command that starts work it does not wait for returns a ``Job``.

Treaty runs no job system. An ``async_job=True`` command returns a ``treaty.Job``
descriptor (REQ-C-022); the framework adds ``terminal``, ``status_command``, and
``cancel_command`` to its ``data``, and serves ``job status <id>`` and ``job cancel <id>``
through the app's ``JobStore``, which asks the real backend.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, get_args

from ._schema import JsonSchema

if TYPE_CHECKING:
    from ._context import Ctx

JobStatus = Literal["running", "complete", "failed", "cancelled"]
TERMINAL: frozenset[str] = frozenset({"complete", "failed", "cancelled"})


@dataclass(frozen=True, slots=True)
class Job:
    """The job descriptor in ``data``; ``effect`` is for mutating commands (REQ-C-003)"""

    job_id: str
    status: JobStatus
    poll_interval_ms: int = 5_000
    """How long an agent waits between ``job status`` calls"""
    timeout_ms: int = 600_000
    """After this long the job counts as failed"""
    effect: str | None = None

    def __post_init__(self) -> None:
        if not self.job_id or not self.job_id.isprintable() or self.job_id != self.job_id.strip():
            raise ValueError(f"job_id {self.job_id!r} is not a printable, trimmed identifier")
        if self.status not in get_args(JobStatus):
            raise ValueError(f"job status {self.status!r} is not one of {get_args(JobStatus)}")
        for name in ("poll_interval_ms", "timeout_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive whole number of milliseconds")


class JobStore(Protocol):
    """The app's view of its jobs; user code, like a handler"""

    def status(self, job_id: str, ctx: Ctx) -> Job | None:
        """The job now, or None when there is no job with that id"""
        ...

    def cancel(self, job_id: str, ctx: Ctx) -> Job | None:
        """Ask the job to stop and return it, or None when there is no job with that id"""
        ...


# Added by the framework to every Job in data, so an agent needs nothing else to poll
_LINKS: dict[str, JsonSchema] = {
    "terminal": {"type": "boolean"},
    "status_command": {"type": "string"},
    "cancel_command": {"type": "string"},
}
_REQUIRED = (
    "job_id",
    "status",
    "terminal",
    "status_command",
    "cancel_command",
    "poll_interval_ms",
    "timeout_ms",
)


def descriptor_schema(schema: JsonSchema) -> JsonSchema:
    """A ``Job`` output's schema with the framework's fields, every spec field required"""
    required = [*_REQUIRED, *(r for r in schema.get("required", ()) if r not in _REQUIRED)]
    return {**schema, "properties": {**schema["properties"], **_LINKS}, "required": required}


def with_links(data: dict[str, object], app_name: str) -> dict[str, object]:
    job_id = shlex.quote(str(data["job_id"]))
    return {
        **data,
        "terminal": data["status"] in TERMINAL,
        "status_command": f"{app_name} job status {job_id}",
        "cancel_command": f"{app_name} job cancel {job_id}",
    }
