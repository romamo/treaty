"""Per-invocation context handed to every handler."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from ._mode import Format
from ._timeout import Timeout

LogSink = Callable[[str, Mapping[str, object]], None]


@dataclass(frozen=True, slots=True)
class Ctx:
    app_name: str
    version: str
    mode: Format
    request_id: str
    env: Mapping[str, str]
    state: Mapping[str, object]
    timeout: Timeout
    color: bool
    """Whether a renderer may color its text: never in JSON mode, under NO_COLOR, CI,
    or TERM=dumb, or when stdout is not a terminal (REQ-F-008)"""
    log_sink: LogSink = field(repr=False, compare=False)
    idempotency_key: str | None = None

    def log(self, message: str, **fields: object) -> None:
        """Write one diagnostic line to stderr, never stdout (REQ-F-006)

        A JSON object in JSON mode, ``message key=value`` otherwise. Declared secrets and
        fields named like credentials (token, password, api_key, Authorization, ...) are
        written as ``[REDACTED]`` (REQ-F-051).
        """
        self.log_sink(message, fields)
