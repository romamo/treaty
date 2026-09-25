"""Per-invocation context handed to every handler."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ._mode import OutputMode
from ._timeout import Timeout


@dataclass(frozen=True, slots=True)
class Ctx:
    app_name: str
    version: str
    mode: OutputMode
    request_id: str
    env: Mapping[str, str]
    state: Mapping[str, object]
    timeout: Timeout
    idempotency_key: str | None = None
