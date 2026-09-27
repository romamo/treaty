"""Async jobs and config writes: REQ-C-022, C-025."""

import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest
from conftest import spec_validator

from examples.deployctl import app as deployctl
from treaty import App, Arg, Ctx, Flag, Job, NoArgs, RegistrationError
from treaty._atomic import write_atomic
from treaty._audit import audit
from treaty._config import user_config

DEPLOYCTL = Path(__file__).resolve().parents[1] / "examples" / "deployctl.py"
DESCRIPTOR = {
    "job_id",
    "status",
    "terminal",
    "status_command",
    "cancel_command",
    "poll_interval_ms",
    "timeout_ms",
}


def run(
    app: App, argv: list[str], env: dict[str, str] | None = None
) -> tuple[int, dict[str, object]]:
    out = io.StringIO()
    code = app.run(
        [*argv, "--format", "json"],
        stdin=io.StringIO(),
        stdout=out,
        stderr=io.StringIO(),
        env={"PATH": "/usr/bin", **(env or {})},
    )
    return code, json.loads(out.getvalue())


def error_code(envelope: dict[str, object]) -> object:
    error = envelope["error"]
    assert isinstance(error, dict)
    return error["code"]


# C-022: async jobs


class Store:
    jobs = {
        "done": Job("done", "complete"),
        "busy": Job("busy", "running", poll_interval_ms=2000),
        "broke": Job("broke", "failed"),
        "gone": Job("gone", "cancelled"),
    }

    def status(self, job_id: str, ctx: Ctx) -> Job | None:
        return self.jobs.get(job_id)

    def cancel(self, job_id: str, ctx: Ctx) -> Job | None:
        job = self.jobs.get(job_id)
        return None if job is None else Job(job_id, "cancelled")


@dataclass(frozen=True, slots=True)
class Build:
    target: str = Arg(description="Build target")


def job_app() -> App:
    app = App("tool", version="1", jobs=Store())

    @app.command(
        "build",
        description="Start a build",
        danger_level="mutating",
        exit_codes=(),
        async_job=True,
    )
    def build(args: Build, ctx: Ctx) -> Job:
        return Job(f"build {args.target}", "running", effect="created")

    return app


def test_the_job_descriptor_has_every_required_field() -> None:
    code, env = run(job_app(), ["build", "api"])
    data = env["data"]
    assert code == 0 and isinstance(data, dict)
    assert DESCRIPTOR <= set(data) and data["terminal"] is False
    assert data["status_command"] == "tool job status 'build api'"
    assert data["cancel_command"] == "tool job cancel 'build api'"


def test_schema_declares_async_and_the_job_descriptor_schema() -> None:
    _, env = run(job_app(), ["build", "--schema"])
    data = env["data"]
    assert isinstance(data, dict) and data["async"] is True
    schema = data["job_descriptor_schema"]
    assert DESCRIPTOR <= set(schema["required"]) and schema == data["output_schema"]


@pytest.mark.parametrize(
    ("job_id", "exit_code", "code"),
    [
        ("done", 0, None),
        ("busy", 3, "JOB_RUNNING"),
        ("broke", 4, "JOB_FAILED"),
        ("gone", 4, "JOB_CANCELLED"),
        ("nope", 5, "JOB_NOT_FOUND"),
    ],
)
def test_job_status_exit_codes(job_id: str, exit_code: int, code: str | None) -> None:
    got, env = run(job_app(), ["job", "status", job_id])
    assert got == exit_code
    if code is not None:
        assert error_code(env) == code
    if job_id != "nope":
        assert env["data"]["job_id"] == job_id  # type: ignore[index]


def test_a_running_job_says_when_to_poll_again() -> None:
    _, env = run(job_app(), ["job", "status", "busy"])
    error = env["error"]
    assert isinstance(error, dict)
    assert error["context"]["poll_interval_ms"] == 2000 and "2000 ms" in error["suggestion"]


def test_job_cancel_returns_the_job() -> None:
    code, env = run(job_app(), ["job", "cancel", "busy"])
    assert code == 0
    assert env["data"]["status"] == "cancelled" and env["data"]["effect"] == "updated"  # type: ignore[index]
    code, env = run(job_app(), ["job", "cancel", "nope"])
    assert code == 5


def test_async_job_without_a_job_output_fails_registration() -> None:
    app = App("tool", version="1", jobs=Store())
    with pytest.raises(RegistrationError, match="treaty.Job"):

        @app.command(
            "build", description="Build", danger_level="safe", exit_codes=(), async_job=True
        )
        def build(args: NoArgs, ctx: Ctx) -> dict[str, str]:
            return {}


def test_async_job_without_a_job_store_fails_registration() -> None:
    app = App("tool", version="1")
    with pytest.raises(RegistrationError, match="jobs="):

        @app.command(
            "build", description="Build", danger_level="safe", exit_codes=(), async_job=True
        )
        def build(args: NoArgs, ctx: Ctx) -> Job:
            return Job("x", "running")


def test_a_job_rejects_an_unknown_status() -> None:
    with pytest.raises(ValueError, match="status"):
        Job("x", "pending")  # type: ignore[arg-type]


def test_the_example_manifest_is_valid() -> None:
    _, env = run(deployctl, ["manifest"])
    assert list(spec_validator("manifest-response").iter_errors(env["data"])) == []


# C-025: config writes


def deployctl_in(cwd: Path, *argv: str, env: dict[str, str]) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, str(DEPLOYCTL), *argv],
        cwd=cwd,
        env={"PATH": os.environ["PATH"], **env},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    envelope = json.loads(result.stdout)
    assert isinstance(envelope, dict)
    return envelope


def test_config_set_writes_the_project_file_not_the_user_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = deployctl_in(tmp_path, "config", "set", "region", "eu-west-1", env={"HOME": str(home)})
    assert (tmp_path / ".deployctl.toml").read_text() == 'region = "eu-west-1"\n'
    assert not home.exists() and env["warnings"] == []


def test_global_writes_the_user_file_and_warns(tmp_path: Path) -> None:
    home = tmp_path / "home"
    argv = ("config", "set", "region", "us", "--global")
    env = deployctl_in(tmp_path, *argv, env={"HOME": str(home)})
    user_file = home / ".config" / "deployctl" / "config.toml"
    assert user_file.read_text() == 'region = "us"\n'
    assert not (tmp_path / ".deployctl.toml").exists()
    assert env["warnings"] == [
        {
            "code": "GLOBAL_CONFIG_MODIFIED",
            "message": "Wrote to the global config file",
            "context": {"path": str(user_file)},
        }
    ]


@dataclass(frozen=True, slots=True)
class Put:
    text: str = Flag(default="a = 1\n", description="New config text")
    broken: bool = Flag(default=False, description="Fail halfway through the write")


@dataclass(frozen=True, slots=True)
class Wrote:
    effect: Literal["updated"]
    path: str


def config_app(scope: str) -> App:
    app = App("tool", version="1")

    @app.command(
        "config.put",
        description="Replace the config",
        danger_level="mutating",
        exit_codes=(),
        config_write_scope=scope,
    )
    def put(args: Put, ctx: Ctx) -> Wrote:
        # A lone surrogate cannot be encoded: the write fails after the temp file opened
        path = ctx.write_config("\ud800" if args.broken else args.text)
        return Wrote("updated", str(path))

    return app


def test_xdg_config_home_locates_the_user_file(tmp_path: Path) -> None:
    code, env = run(
        config_app("local"), ["config", "put", "--global"], {"XDG_CONFIG_HOME": str(tmp_path)}
    )
    assert code == 0 and (tmp_path / "tool" / "config.toml").read_text() == "a = 1\n"


def test_a_global_only_command_requires_global(tmp_path: Path) -> None:
    home = {"HOME": str(tmp_path)}
    code, env = run(config_app("global"), ["config", "put"], home)
    assert code == 2 and not (tmp_path / ".config").exists()
    code, env = run(config_app("global"), ["config", "put", "--global"], home)
    assert code == 0 and (tmp_path / ".config" / "tool" / "config.toml").is_file()


def test_global_without_a_home_exits_4() -> None:
    code, env = run(config_app("local"), ["config", "put", "--global"])
    assert code == 4 and error_code(env) == "CONFIG_DIR_UNKNOWN"


def test_an_interrupted_global_write_leaves_the_old_config(tmp_path: Path) -> None:
    home = {"HOME": str(tmp_path)}
    run(config_app("local"), ["config", "put", "--global", "--text", "old = 1"], home)
    code, env = run(config_app("local"), ["config", "put", "--global", "--broken"], home)
    folder = tmp_path / ".config" / "tool"
    assert code == 1 and error_code(env) == "HANDLER_CRASHED"
    assert (folder / "config.toml").read_text() == "old = 1"
    assert sorted(p.name for p in folder.iterdir()) == ["config.toml", "config.toml.lock"]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no permission bits to keep")
def test_write_atomic_keeps_the_mode_of_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    path.write_text("a")
    path.chmod(0o640)
    write_atomic(path, "b")
    assert path.read_text() == "b" and path.stat().st_mode & 0o777 == 0o640


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_write_atomic_replaces_the_target_of_a_symlink(tmp_path: Path) -> None:
    (tmp_path / "dotfiles").mkdir()
    target = tmp_path / "dotfiles" / "c.toml"
    target.write_text("a")
    link = tmp_path / "c.toml"
    link.symlink_to(target)
    write_atomic(link, "b")
    assert link.is_symlink() and target.read_text() == "b"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["c.toml", "dotfiles"]


def test_a_relative_xdg_config_home_is_ignored(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path), "XDG_CONFIG_HOME": "relative"}
    assert user_config("tool", env) == tmp_path / ".config" / "tool" / "config.toml"
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
    assert user_config("tool", env) == tmp_path / "xdg" / "tool" / "config.toml"


def test_schema_declares_the_write_scope_and_global_flag() -> None:
    _, env = run(deployctl, ["config", "set", "--schema"])
    data = env["data"]
    assert isinstance(data, dict) and data["config_write_scope"] == "local"
    assert data["flags"]["global"]["default"] is False
    _, env = run(config_app("global"), ["config", "put", "--schema"])
    assert env["data"]["flags"]["global"]["required"] is True  # type: ignore[index]


def test_write_config_without_a_scope_fails_registration() -> None:
    app = App("tool", version="1")
    with pytest.raises(RegistrationError, match="config_write_scope"):

        @app.command("config.put", description="Put", danger_level="mutating", exit_codes=())
        def put(args: NoArgs, ctx: Ctx) -> Wrote:
            ctx.write_config("a = 1\n")
            return Wrote("updated", "")


@pytest.mark.parametrize(
    ("danger", "scope", "match"),
    [("safe", "local", "mutating"), ("mutating", "session", "not one of")],
)
def test_bad_config_declarations_fail_registration(danger: str, scope: str, match: str) -> None:
    app = App("tool", version="1")
    with pytest.raises(RegistrationError, match=match):

        @app.command(
            "config.put",
            description="Put",
            danger_level=danger,
            exit_codes=(),
            config_write_scope=scope,
        )
        def put(args: NoArgs, ctx: Ctx) -> Wrote:
            return Wrote("updated", "")


# Audit rules


def test_audit_flags_undeclared_config_writes_and_jobs() -> None:
    app = App("tool", version="1")

    @app.command("config.put", description="Put", danger_level="mutating", exit_codes=())
    def put(args: NoArgs, ctx: Ctx) -> Wrote:
        return Wrote("updated", "")

    @app.command("start", description="Start", danger_level="safe", exit_codes=())
    def start(args: NoArgs, ctx: Ctx) -> None:
        return None

    @app.command("flag.set", description="Set a flag", danger_level="mutating", exit_codes=())
    def set_flag(args: NoArgs, ctx: Ctx) -> Wrote:
        return Wrote("updated", "")  # a bare 'set' outside config writes no config

    rules = {r.id: r for r in audit(app, "tool", limit=10).rules}
    assert [f.command for f in rules["config-write-scope"].findings] == ["config.put"]
    assert [f.command for f in rules["async-job"].findings] == ["start"]
    rules = {r.id: r for r in audit(deployctl, "deployctl", limit=10).rules}
    assert rules["config-write-scope"].passed and rules["async-job"].passed
