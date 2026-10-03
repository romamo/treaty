"""An app whose ``mcp serve`` binds startup arguments into the command tools it serves
(#285): a positional project, a list of inventories, an object of options, a field a
variable may fill, and a field a conditional rule names. Each ``--mode`` makes ``bind``
return something else, the bad ones refused before serving."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from treaty import App, Arg, Ctx, Flag, McpServe, McpTool, NoArgs, ParseError, RequiredWhen


@dataclass(frozen=True, slots=True)
class ServeArgs:
    project: str = Flag(default="/srv/fleet", description="Project directory the tools work in")
    mode: Literal[
        "fleet",
        "strict",
        "unknown",
        "secret",
        "wrong-type",
        "not-json",
        "not-mapping",
        "raises",
        "off",
        "escape",
    ] = Flag(default="fleet", description="What bind returns")


def bind(args: ServeArgs) -> Mapping[str, object]:
    match args.mode:
        case "fleet":
            return {
                "project": args.project,
                "inventory": ["web", "db"],
                "options": {"parallel": 2, "tags": ["blue"]},
                "region": "eu-west-1",
            }
        case "strict":
            return {"project": args.project, "policy": "strict"}
        case "unknown":
            return {"project": args.project, "repository": "/srv/other"}
        case "secret":
            return {"token": "bound-secret-0123456789"}
        case "wrong-type":
            return {"inventory": "web"}
        case "not-json":
            return {"project": Path(args.project)}
        case "not-mapping":
            return [("project", args.project)]  # type: ignore[return-value]
        case "raises":
            raise RuntimeError("bind failed")
        case "escape":
            return {"project": "/"}  # DeployArgs.__post_init__ refuses it on each call
        case "off":
            return {"project": args.project, "approval": "yes"}  # approve is mcp=False


@dataclass(frozen=True, slots=True)
class Options:
    parallel: int
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeployArgs:
    project: str = Arg(description="Project directory")
    target: str = Flag(description="Host to deploy")
    inventory: tuple[str, ...] = Flag(default=(), description="Inventories to read")
    options: Options | None = Flag(default=None, description="Deploy options")
    region: str = Flag(default="us-east-1", description="Region", env=("BINDCTL_REGION_NAME",))
    token: str | None = Flag(default=None, description="Gateway token", secret=True)

    def __post_init__(self) -> None:
        if self.project == "/":
            raise ParseError("project cannot be the root directory", context={"field": "project"})


@dataclass(frozen=True, slots=True)
class AuditArgs:
    project: str = Flag(description="Project directory")
    policy: Literal["lenient", "strict"] = Flag(default="lenient", description="Policy")
    reason: str | None = Flag(default=None, description="Why a strict audit runs")


@dataclass(frozen=True, slots=True)
class ApproveArgs:
    approval: str = Flag(description="Approval text")


app = App(
    "bindctl",
    version="1.0.0",
    description="Bind control",
    mcp=McpServe(args=ServeArgs, bind=bind, tools=lambda args, ctx: _provided()),
)


@dataclass(frozen=True, slots=True)
class Seen:
    seen: dict[str, object]


def _look(arguments: Mapping[str, object], ctx: Ctx) -> Seen:
    return Seen(dict(arguments))


def _provided() -> list[McpTool]:
    # A provided tool with a field named like a bound one: bind leaves it alone
    schema: dict[str, object] = {
        "type": "object",
        "properties": {"project": {"type": "string"}},
        "additionalProperties": False,
    }
    return [McpTool(name="look", description="Look", input_schema=schema, handler=_look)]


@app.command("deploy", description="Deploy a host", danger_level="safe", exit_codes=())
def deploy(args: DeployArgs, ctx: Ctx) -> dict[str, object]:
    options = None if args.options is None else {"parallel": args.options.parallel}
    return {
        "project": args.project,
        "target": args.target,
        "inventory": list(args.inventory),
        "options": options,
        "region": args.region,
    }


@app.command(
    "audit",
    description="Audit a project",
    danger_level="safe",
    exit_codes=(),
    requires=[RequiredWhen("policy", "strict", then=("reason",))],
)
def audit(args: AuditArgs, ctx: Ctx) -> dict[str, object]:
    return {"project": args.project, "policy": args.policy, "reason": args.reason}


@app.command("ping", description="Answer pong", danger_level="safe", exit_codes=())
def ping(args: NoArgs, ctx: Ctx) -> dict[str, str]:
    return {"pong": "pong"}


@app.command(
    "approve", description="Approve; a person runs this", danger_level="safe", exit_codes=(),
    mcp=False,
)  # fmt: skip
def approve(args: ApproveArgs, ctx: Ctx) -> dict[str, str]:
    return {"approval": args.approval}


if __name__ == "__main__":
    app.main()
