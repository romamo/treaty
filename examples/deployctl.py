"""Example CLI built on treaty. Run: uv run examples/deployctl.py deploy rollback api --dry-run"""

import json
from dataclasses import dataclass
from typing import Literal

from treaty import Affects, App, Arg, Ctx, Exit, Flag, Job


class Deployments:
    """Where jobs live; a real app asks its deploy API"""

    def status(self, job_id: str, ctx: Ctx) -> Job | None:
        return Job(job_id, "complete") if job_id.startswith("deploy-") else None

    def cancel(self, job_id: str, ctx: Ctx) -> Job | None:
        return Job(job_id, "complete", effect="noop") if job_id.startswith("deploy-") else None


app = App("deployctl", version="1.4.0", description="Manage deployments", jobs=Deployments())
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
    would_affect: Affects | None = None


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
    release = args.to or "previous"
    if args.dry_run:
        affects = Affects(
            f"Rolls {args.service} back to {release}", (f"service/{args.service}",), 1
        )
        return Plan("would_update", args.service, release, args.strategy, affects)
    return Plan("updated", args.service, release, args.strategy)


@dataclass(frozen=True, slots=True)
class Start:
    service: str = Arg(description="Service name")


@deploy.command(
    "start",
    description="Start deploying a service; poll the returned job for the outcome",
    danger_level="mutating",
    exit_codes=(),
    async_job=True,
    examples=[("Start a deployment", "deployctl deploy start api")],
)
def start(args: Start, ctx: Ctx) -> Job:
    return Job(f"deploy-{args.service}", "running", effect="created")


@dataclass(frozen=True, slots=True)
class Setting:
    name: str = Arg(description="Setting name, such as region")
    value: str = Arg(description="New value")


@dataclass(frozen=True, slots=True)
class Written:
    effect: Literal["updated"]
    name: str
    value: str
    path: str


config = app.group("config", description="Change settings")


@config.command(
    "set",
    description="Set a setting in the project config, or the user config with --global",
    danger_level="mutating",
    exit_codes=(),
    config_write_scope="local",
    examples=[("Set the region", "deployctl config set region eu-west-1")],
)
def set_(args: Setting, ctx: Ctx) -> Written:
    assert ctx.config_path is not None
    old = ctx.config_path.read_text().splitlines() if ctx.config_path.exists() else []
    kept = [line for line in old if not line.startswith(f"{args.name} =")]
    path = ctx.write_config("\n".join([*kept, f"{args.name} = {json.dumps(args.value)}"]) + "\n")
    return Written("updated", args.name, args.value, str(path))


if __name__ == "__main__":
    app.main()
