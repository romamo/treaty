"""democli built with treaty: the same business logic as the argparse and click builds.

The framework supplies the envelope, exit codes, manifest, dry-run gating, and
`--confirm-destructive`; the handlers only declare and return.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _fixture as fx  # noqa: E402

from treaty import App, Ctx, Exit, Flag  # noqa: E402

# Idempotency records live beside the fixture state, which the harness isolates per trial
app = App(
    "democli",
    version="2.1.0",
    description="Demo deployment tool",
    state_dir=Path(os.environ.get("TMPDIR", "/tmp")) / "treaty-state",
)
app.exit_code(
    "LOCK_HELD",
    79,
    description="Another deployment holds the lock; retry after error.retry_after_ms",
    retryable=True,
    side_effects="none",
)


@dataclass(frozen=True, slots=True)
class Deployment:
    id: str
    version: str
    env: str
    status: str
    created_at: str
    note: str


@dataclass(frozen=True, slots=True)
class Pagination:
    page: int
    pages: int
    total: int
    has_more: bool
    next_page: int | None


@dataclass(frozen=True, slots=True)
class DeploymentPage:
    items: list[Deployment]
    pagination: Pagination


@dataclass(frozen=True, slots=True)
class ListArgs:
    limit: int = Flag(default=5, description="Items per page")
    page: int = Flag(default=1, description="Page number, starting at 1")


deployments = app.group("deployments", description="Manage deployments")


@deployments.command(
    "list",
    description="List deployments, five per page by default",
    required_scopes=["deployments:read"],
    examples=[("Second page", "democli deployments list --page 2")],
)
def list_(args: ListArgs, ctx: Ctx) -> DeploymentPage:
    try:
        items, pages = fx.page(args.limit, args.page)
    except ValueError as exc:
        raise Exit.ARG_ERROR(str(exc), context={"limit": args.limit, "page": args.page}) from exc
    return DeploymentPage(
        items=[Deployment(**item) for item in items],
        pagination=Pagination(
            page=args.page,
            pages=pages,
            total=len(fx.all_deployments()),
            has_more=args.page < pages,
            next_page=args.page + 1 if args.page < pages else None,
        ),
    )


@dataclass(frozen=True, slots=True)
class Target:
    id: str
    env: str
    age_days: int


@dataclass(frozen=True, slots=True)
class DeleteResult:
    effect: Literal["would_delete", "deleted"]
    deployments: list[Target]
    count: int


@dataclass(frozen=True, slots=True)
class DeleteArgs:
    filter: str = Flag(description="Selection expression such as env=staging")
    dry_run: bool = Flag(default=False, description="Preview without deleting")


@deployments.command(
    "delete",
    description="Delete deployments matching a filter",
    danger_level="destructive",
    required_scopes=["deployments:write"],
    examples=[("Preview a staging cleanup", "democli deployments delete --filter env=staging --dry-run")],
)
def delete(args: DeleteArgs, ctx: Ctx) -> DeleteResult:
    try:
        targets = [Target(**t) for t in fx.matching_stale(args.filter)]  # type: ignore[arg-type]
    except ValueError as exc:
        raise Exit.ARG_ERROR(str(exc), context={"filter": args.filter}) from exc
    effect: Literal["would_delete", "deleted"] = "would_delete" if args.dry_run else "deleted"
    return DeleteResult(effect=effect, deployments=targets, count=len(targets))


@dataclass(frozen=True, slots=True)
class DeployArgs:
    version: str = Flag(description="Version to deploy")
    env: Literal["staging", "production"] = Flag(description="Target environment")


@dataclass(frozen=True, slots=True)
class DeployResult:
    effect: Literal["created"]
    status: str
    version: str
    env: str
    deploy_id: str


@app.command(
    "deploy",
    description="Deploy an application version",
    danger_level="mutating",
    required_scopes=["deploy:write"],
    has_network_io=True,
    exit_codes=["LOCK_HELD"],
    examples=[("Deploy to staging", "democli deploy --version 2.1.0 --env staging")],
)
def deploy(args: DeployArgs, ctx: Ctx) -> DeployResult:
    if args.env == "production":
        try:
            record = fx.deploy_production(args.version, ctx.idempotency_key)
        except fx.ResponseLost as exc:
            raise Exit.GENERAL_ERROR(
                "Connection reset while reading the response",
                code="RESPONSE_LOST",
                context={"version": args.version, "env": args.env},
                suggestion="Run `democli deployments list` to see whether the deployment was "
                "created, or re-run with the same --idempotency-key to get the original result",
            ) from exc
        return DeployResult(
            effect="created",
            status="deployed",
            version=args.version,
            env=args.env,
            deploy_id=record["id"],
        )
    if not fx.acquire_deploy_lock():
        raise Exit.LOCK_HELD(
            "Another deployment is in progress",
            context={"lock_holder": fx.LOCK_HOLDER},
            retry_after_ms=fx.LOCK_RETRY_MS,
        )
    return DeployResult(
        effect="created",
        status="deployed",
        version=args.version,
        env=args.env,
        deploy_id="deploy-new-001",
    )


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    service: str
    status: Literal["ok", "failed", "timeout"]


@dataclass(frozen=True, slots=True)
class CheckArgs:
    deep: bool = Flag(default=False, description="Also probe the CDN edge (slow)")


@dataclass(frozen=True, slots=True)
class HealthReport:
    services: list[ServiceStatus]
    cache_age_hours: int


health = app.group("health", description="Service health commands")


@health.command(
    "check",
    description="Run health checks against all services",
    required_scopes=["health:read"],
    has_network_io=True,
    timeout=5,
    examples=[("Check every service", "democli health check")],
)
def check(args: CheckArgs, ctx: Ctx) -> HealthReport:
    services = [
        ServiceStatus("api-server", "ok"),
        ServiceStatus("database", "ok"),
        ServiceStatus("registry", "failed"),
    ]
    if args.deep:
        budget = ctx.timeout.seconds
        deadline = None if budget is None else max(budget - 1.0, 0.5)
        services.append(ServiceStatus("cdn", fx.probe_cdn(deadline)))
    report = HealthReport(services=services, cache_age_hours=fx.CACHE_AGE_HOURS)
    raise Exit.AUTH_REQUIRED(
        "Registry credential expired",
        code="TOKEN_EXPIRED",
        context={"service": "registry", "expired_at": fx.REGISTRY_EXPIRED_AT, "cache_age_hours": fx.CACHE_AGE_HOURS},
        fix_required="Refresh the registry credential, then re-run the health check",
        data=report,
    )


if __name__ == "__main__":
    app.main()
