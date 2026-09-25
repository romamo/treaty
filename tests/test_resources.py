"""Typed resources: handler parameters after ctx name classes with an acquire classmethod."""

import io
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Self

import pytest

from treaty import App, Arg, Ctx, Exit, Flag, ParseError, RegistrationError

ACQUIRED: list[str] = []


@dataclass(frozen=True, slots=True)
class ProjectArgs:
    project: Path | None = Flag(default=None, description="Project directory")


@dataclass(frozen=True, slots=True, kw_only=True)
class Args(ProjectArgs):
    component: str = Arg(description="Component")
    dry_run: bool = Flag(default=False, description="Preview only")


@dataclass(frozen=True, slots=True)
class Project:
    directory: Path

    @classmethod
    def acquire(cls, args: Args, ctx: Ctx) -> Self:
        ACQUIRED.append("project")
        directory = args.project if args.project is not None else Path(ctx.env.get("PWD", "."))
        if directory.name == "missing":
            raise Exit.NO_PROJECT("no such project", context={"directory": str(directory)})
        if directory.name == "malformed":
            raise ParseError("project directory is not a project", context={"flag": "project"})
        return cls(directory)


@dataclass(frozen=True, slots=True)
class Config:
    project: Project
    component: str

    @classmethod
    def acquire(cls, args: Args, ctx: Ctx, project: Project) -> Self:
        ACQUIRED.append("config")
        return cls(project, args.component)


@dataclass(frozen=True, slots=True)
class Slow:
    @classmethod
    def acquire(cls, args: Args, ctx: Ctx) -> Self:
        time.sleep(0.5)
        return cls()


@dataclass(frozen=True, slots=True)
class Out:
    effect: str
    directory: str
    component: str
    same_project: bool


def resource_app() -> App:
    app = App("fleet", version="1")
    app.exit_code("NO_PROJECT", 80, description="Missing", retryable=False, side_effects="none")

    @app.command(
        "deploy", description="Deploy", danger_level="destructive", exit_codes=["NO_PROJECT"]
    )
    def deploy(args: Args, ctx: Ctx, config: Config, project: Project) -> Out:
        effect = "would_update" if args.dry_run else "updated"
        return Out(effect, str(project.directory), config.component, config.project is project)

    @app.command("slow", description="Slow resource", timeout=0.05)
    def slow(args: Args, ctx: Ctx, slow: Slow) -> None:
        return None

    return app


def run(argv: list[str], stdin: str = "", env: dict[str, str] | None = None) -> tuple[int, dict]:
    ACQUIRED.clear()
    out = io.StringIO()
    code = resource_app().run(
        argv,
        stdin=io.StringIO(stdin),
        stdout=out,
        stderr=io.StringIO(),
        env=env or {},
        isatty=False,
    )
    return code, json.loads(out.getvalue())


def test_resources_are_acquired_once_in_dependency_order_and_shared() -> None:
    code, env = run(
        ["deploy", "--component", "api", "--project", "/srv/app", "--confirm-destructive"]
    )
    assert code == 0
    assert env["data"] == {
        "effect": "updated",
        "directory": "/srv/app",
        "component": "api",
        "same_project": True,
    }
    assert ACQUIRED == ["project", "config"]


def test_resources_see_the_dry_run_args_of_an_unconfirmed_destructive_run() -> None:
    code, env = run(["deploy", "--component", "api", "--project", "/srv/app"])
    assert code == 2
    assert env["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert env["data"]["effect"] == "would_update"
    assert ACQUIRED == ["project", "config"]


def test_resources_read_the_context() -> None:
    code, env = run(["deploy", "--component", "api", "--confirm-destructive"], env={"PWD": "/work"})
    assert code == 0
    assert env["data"]["directory"] == "/work"


def test_cli_exit_inside_acquire_becomes_the_declared_exit() -> None:
    code, env = run(["deploy", "--component", "api", "--project", "/srv/missing"])
    assert code == 80
    assert env["error"]["code"] == "NO_PROJECT"
    assert env["error"]["context"] == {"directory": "/srv/missing"}
    assert ACQUIRED == ["project"]


def test_parse_error_inside_acquire_is_an_argument_error() -> None:
    code, env = run(["deploy", "--component", "api", "--project", "/srv/malformed"])
    assert code == 2
    assert env["error"]["code"] == "ARG_ERROR"
    assert env["error"]["errors"][0]["field"] == "project"


def test_acquisition_runs_under_the_command_timeout() -> None:
    code, env = run(["slow", "--component", "x"])
    assert code == 10
    assert env["error"]["code"] == "TIMEOUT"


def test_exec_route_acquires_resources_too() -> None:
    line = json.dumps(
        {
            "_cmd": "deploy",
            "component": "api",
            "_opts": {"project": "/srv/app", "confirm-destructive": True},
        }
    )
    code, env = run(["exec"], stdin=line + "\n")
    assert code == 0
    assert env["data"]["directory"] == "/srv/app"


def test_manifest_does_not_mention_resources() -> None:
    entry = resource_app().manifest()["commands"]["deploy"]
    assert set(entry["flags"]) == {
        "project",
        "component",
        "dry-run",
        "confirm-destructive",
        "idempotency-key",
    }


# Registration


@dataclass(frozen=True, slots=True)
class Plain:
    pass


class NoAcquire:
    def acquire(self, args: object, ctx: Ctx) -> Self:
        return self


class BadCtx:
    @classmethod
    def acquire(cls, args: object, ctx: object) -> Self:
        return cls()


class Left:
    @classmethod
    def acquire(cls, args: object, ctx: Ctx, right: Right) -> Self:
        return cls()


class Right:
    @classmethod
    def acquire(cls, args: object, ctx: Ctx, left: Left) -> Self:
        return cls()


def register(fn: object, match: str) -> None:
    app = App("fleet", version="1")
    with pytest.raises(RegistrationError, match=match):
        app.command("x", description="x")(fn)  # type: ignore[arg-type]


def test_resource_without_classmethod_acquire_is_refused() -> None:
    def handler(args: Args, ctx: Ctx, r: NoAcquire) -> None:
        return None

    register(handler, "NoAcquire is not a resource: it needs a classmethod acquire")


def test_plain_class_without_acquire_is_refused() -> None:
    def handler(args: Args, ctx: Ctx, r: Plain) -> None:
        return None

    register(handler, "Plain is not a resource")


def test_acquire_must_take_ctx_second() -> None:
    def handler(args: Args, ctx: Ctx, r: BadCtx) -> None:
        return None

    register(handler, "BadCtx.acquire: second parameter must be annotated with Ctx")


def test_resource_cycle_is_refused() -> None:
    def handler(args: Args, ctx: Ctx, left: Left) -> None:
        return None

    register(handler, r"resource cycle Left -> Right -> Left")


def test_unannotated_extra_parameter_is_refused() -> None:
    def handler(args: Args, ctx: Ctx, extra) -> None:  # type: ignore[no-untyped-def]  # noqa: ANN001
        return None

    register(handler, "parameter 'extra' must be annotated with a resource class")


def test_same_resource_twice_is_refused() -> None:
    def handler(args: Args, ctx: Ctx, a: Project, b: Project) -> None:
        return None

    register(handler, "resource Project asked for twice")


def test_keyword_only_resource_parameter_is_refused() -> None:
    def handler(args: Args, ctx: Ctx, *, project: Project) -> None:
        return None

    register(handler, r"handler: must take \(args, ctx, \*resources\) positionally")
