"""Example CLI built on treaty. Run: uv run examples/deployctl.py deploy rollback api --dry-run"""

from dataclasses import dataclass
from typing import Literal

from treaty import App, Arg, Ctx, Exit, Flag

app = App("deployctl", version="1.4.0", description="Manage deployments")
app.exit_code(
    "DEPLOY_CONFLICT",
    79,
    description="Target already has a deployment in progress",
    retryable=False,
    side_effects="none",
)


@dataclass(frozen=True, slots=True)
class Rollback:
    service: str = Arg(description="Service name")
    to: str | None = Flag(default=None, description="Release tag to roll back to", short="t")
    strategy: Literal["fast", "safe"] = Flag(default="safe", description="Rollout strategy")
    dry_run: bool = Flag(default=False, description="Plan the rollback, write nothing")


@dataclass(frozen=True, slots=True)
class Plan:
    effect: Literal["would_update", "updated"]
    service: str
    release: str
    strategy: str


deploy = app.group("deploy", description="Manage deployments")


@deploy.command(
    "rollback",
    description="Roll a service back to its previous release",
    danger_level="destructive",
    required_scopes=["deploy:write"],
    exit_codes=["DEPLOY_CONFLICT"],
    supports_raw_payload=True,
    examples=[("Plan a rollback", "deployctl deploy rollback api --to 1.3.9 --dry-run")],
)
def rollback(args: Rollback, ctx: Ctx) -> Plan:
    if args.service == "locked":
        raise Exit.DEPLOY_CONFLICT("deployment in progress", context={"service": args.service})
    effect: Literal["would_update", "updated"] = "would_update" if args.dry_run else "updated"
    return Plan(effect, args.service, args.to or "previous", args.strategy)


if __name__ == "__main__":
    app.main()
