import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest
from jsonschema import Draft7Validator
from referencing import Registry, Resource

from treaty import App, Arg, Ctx, Exit, Flag

SPEC_DIR = Path(
    os.environ.get("TREATY_SPEC_DIR", Path(__file__).resolve().parents[2] / "cli-agent-ergonomics")
)
SCHEMAS = SPEC_DIR / "schemas"


def spec_validator(name: str) -> Draft7Validator:
    if not SCHEMAS.is_dir():
        pytest.skip(f"spec schemas not found at {SCHEMAS}; set TREATY_SPEC_DIR")
    registry: Registry = Registry()
    for path in SCHEMAS.glob("*.json"):
        contents = json.loads(path.read_text())
        registry = registry.with_resource(path.name, Resource.from_contents(contents))
    schema = json.loads((SCHEMAS / f"{name}.json").read_text())
    return Draft7Validator(schema, registry=registry)


@dataclass(frozen=True, slots=True)
class Rollback:
    service: str = Arg(description="Service name as shown in deployctl ls")
    to: str | None = Flag(default=None, description="Release tag to roll back to", short="t")
    strategy: Literal["fast", "safe"] = Flag(default="safe", description="Rollout strategy")
    replicas: int = Flag(default=1, description="Replica count to keep warm")
    tags: tuple[str, ...] = Flag(default=(), description="Labels to attach")
    dry_run: bool = Flag(default=False, description="Plan the rollback, write nothing")


@dataclass(frozen=True, slots=True)
class Plan:
    service: str
    release: str
    strategy: str
    replicas: int
    tags: tuple[str, ...]
    dry_run: bool


@dataclass(frozen=True, slots=True)
class Status:
    service: str = Arg(description="Service name")


@pytest.fixture
def app() -> App:
    app = App("deployctl", version="1.4.0", description="Deploy things")
    app.exit_code(
        "DEPLOY_CONFLICT",
        79,
        description="Target already has a deployment in progress",
        retryable=False,
        side_effects="none",
    )
    app.exit_code(
        "UPSTREAM_TIMEOUT",
        80,
        description="Registry did not respond within the deadline",
        retryable=True,
        side_effects="none",
    )
    deploy = app.group("deploy", description="Manage deployments")

    @deploy.command(
        "rollback",
        description="Roll a service back to its previous release",
        danger_level="destructive",
        required_scopes=["deploy:write"],
        exit_codes=["DEPLOY_CONFLICT", "UPSTREAM_TIMEOUT"],
        examples=[("Plan a rollback", "deployctl deploy rollback api --to 1.3.9 --dry-run")],
        has_network_io=True,
    )
    def rollback(args: Rollback, ctx: Ctx) -> Plan:
        if args.service == "locked":
            raise Exit.DEPLOY_CONFLICT("deployment in progress", context={"active": "1.4.0"})
        if args.service == "slow":
            raise Exit.UPSTREAM_TIMEOUT("registry timed out", retry_after_ms=500)
        if args.service == "buggy":
            raise Exit.NOT_DECLARED("handler bug")
        return Plan(
            args.service,
            args.to or "previous",
            args.strategy,
            args.replicas,
            args.tags,
            args.dry_run,
        )

    @deploy.command("status", description="Show the active release of a service")
    def status(args: Status, ctx: Ctx) -> list[dict[str, str]]:
        return [{"service": args.service, "release": "1.4.0"}]

    return app
