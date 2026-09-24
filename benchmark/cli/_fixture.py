"""Business logic shared by the three framework implementations.

Every framework build imports this module, so the only thing that differs between
argparse, click, and treaty is what the framework does with the same data.
"""

from __future__ import annotations

import os
from pathlib import Path

DEPLOYMENTS: list[dict[str, str]] = [
    {"id": "deploy-001", "version": "1.0.0", "env": "staging", "status": "retired", "created_at": "2026-01-15T10:00:00Z"},
    {"id": "deploy-002", "version": "1.0.1", "env": "staging", "status": "retired", "created_at": "2026-01-22T14:30:00Z"},
    {"id": "deploy-003", "version": "1.1.0", "env": "production", "status": "retired", "created_at": "2026-02-01T09:00:00Z"},
    {"id": "deploy-004", "version": "1.1.1", "env": "staging", "status": "retired", "created_at": "2026-02-05T16:00:00Z"},
    {"id": "deploy-005", "version": "1.2.0", "env": "production", "status": "active", "created_at": "2026-02-10T09:30:00Z"},
    {"id": "deploy-006", "version": "1.2.1", "env": "staging", "status": "active", "created_at": "2026-02-12T11:00:00Z"},
    {"id": "deploy-007", "version": "1.3.0", "env": "staging", "status": "active", "created_at": "2026-02-18T08:00:00Z"},
    {"id": "deploy-008", "version": "1.3.1", "env": "production", "status": "active", "created_at": "2026-02-20T13:00:00Z"},
    {"id": "deploy-009", "version": "1.4.0", "env": "staging", "status": "active", "created_at": "2026-02-25T10:00:00Z"},
    {"id": "deploy-010", "version": "1.4.1", "env": "production", "status": "active", "created_at": "2026-03-01T09:00:00Z"},
    {"id": "deploy-011", "version": "1.5.0", "env": "staging", "status": "active", "created_at": "2026-03-02T10:00:00Z"},
    {"id": "deploy-012", "version": "1.5.1", "env": "staging", "status": "active", "created_at": "2026-03-04T14:00:00Z"},
    {"id": "deploy-013", "version": "1.6.0", "env": "staging", "status": "active", "created_at": "2026-03-06T11:00:00Z"},
    {"id": "deploy-014", "version": "1.6.1", "env": "production", "status": "active", "created_at": "2026-03-08T09:30:00Z"},
    {"id": "deploy-015", "version": "2.0.0", "env": "staging", "status": "active", "created_at": "2026-03-10T10:00:00Z"},
    {"id": "deploy-016", "version": "2.0.1", "env": "staging", "status": "active", "created_at": "2026-03-11T12:00:00Z"},
    {"id": "deploy-017", "version": "2.0.2", "env": "production", "status": "active", "created_at": "2026-03-12T08:00:00Z"},
    {"id": "deploy-018", "version": "2.1.0", "env": "staging", "status": "active", "created_at": "2026-03-14T15:00:00Z"},
    {"id": "deploy-019", "version": "2.1.0", "env": "staging", "status": "active", "created_at": "2026-03-15T10:00:00Z"},
    {"id": "deploy-020", "version": "2.1.0", "env": "staging", "status": "active", "created_at": "2026-03-16T09:00:00Z"},
]

STALE_STAGING: list[dict[str, object]] = [
    {"id": "deploy-021", "env": "staging", "age_days": 45},
    {"id": "deploy-034", "env": "staging", "age_days": 38},
    {"id": "deploy-041", "env": "staging", "age_days": 33},
]

LOCK_HOLDER = "deploy-job-4421"
LOCK_RETRY_MS = 2000
REGISTRY_EXPIRED_AT = "2026-03-19T09:00:00Z"
CACHE_AGE_HOURS = 4
ENVS = ("staging", "production")


def page(limit: int, number: int) -> tuple[list[dict[str, str]], int]:
    """Items on page `number` (1-based) and the total page count."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if number < 1:
        raise ValueError("page must be at least 1")
    pages = -(-len(DEPLOYMENTS) // limit)
    start = (number - 1) * limit
    return DEPLOYMENTS[start : start + limit], pages


def _lock_file() -> Path:
    return Path(os.environ.get("TMPDIR", "/tmp")) / "democli_deploy_lock"


def acquire_deploy_lock() -> bool:
    """False on the first call in a fresh state directory, True afterwards.

    The first deploy in a state directory finds the lock held by another job; the
    second finds it released. This mirrors the benchmark's good and bad mocks.
    """
    marker = _lock_file()
    if marker.exists():
        marker.unlink()
        return True
    marker.touch()
    return False


def matching_stale(filter_expr: str) -> list[dict[str, object]]:
    if filter_expr != "env=staging":
        raise ValueError(f"unsupported filter {filter_expr!r}; use env=staging")
    return STALE_STAGING
