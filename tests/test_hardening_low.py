"""Regressions for the low findings of the pre-1.0 review: background pid identity and
escalation, pruning a live session, flag defaults, empty config flags, schema version
pins, a global's name as a flag value, renderers, app import errors, and cleanup."""

import io
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import needs_posix_signals

from treaty import App, Ctx, Flag, NoArgs, RegistrationError, SideEffect
from treaty._mode import Format
from treaty._session import STALE_SESSION_SECONDS, Session, SessionRoot, prune
from treaty._subprocess import reap, started

BASE_ENV = {"PATH": os.environ["PATH"], "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}


def run(app: App, argv: list[str]) -> tuple[int, dict[str, object], str]:
    out, err = io.StringIO(), io.StringIO()
    code = app.run([*argv, "--format", "json"], stdout=out, stderr=err, env=BASE_ENV)
    return code, json.loads(out.getvalue()), err.getvalue()


def error_code(envelope: dict[str, object]) -> object:
    error = envelope["error"]
    assert isinstance(error, dict)
    return error["code"]


# Background processes: a pid file entry names one process, and SIGKILL follows SIGTERM

STUBBORN = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"


def leader(code: str = "import time; time.sleep(60)") -> subprocess.Popen[bytes]:
    """A process group leader, as ctx.spawn starts, once its code is running"""
    ready = f"{code.rpartition(';')[0]}; print('ready', flush=True); {code.rpartition(';')[2]}"
    proc = subprocess.Popen(
        [sys.executable, "-c", ready], stdout=subprocess.PIPE, start_new_session=True
    )
    assert proc.stdout is not None and proc.stdout.readline() == b"ready\n"
    return proc


@needs_posix_signals
def test_reap_leaves_a_process_that_reused_an_expired_childs_pid_alone(tmp_path: Path) -> None:
    other = leader()
    try:
        pid_file = tmp_path / "go.pids"
        pid_file.write_text(f"{other.pid} 1 not-its-start-time\n")
        assert reap(pid_file) == []
        time.sleep(0.2)
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


@needs_posix_signals
def test_reap_kills_a_child_that_outlived_sigterm_past_its_grace(tmp_path: Path) -> None:
    child = leader(STUBBORN)
    try:
        start = started(child.pid)
        pid_file = tmp_path / "go.pids"
        pid_file.write_text(f"{child.pid} 1 {start}\n")
        [entry] = reap(pid_file)  # SIGTERM, which it ignores
        assert entry.split()[3] == "stopping"
        assert child.poll() is None
        pid_file.write_text(f"{child.pid} 1 {start} stopping\n")
        assert reap(pid_file) == []
        assert child.wait(timeout=10) == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


# Temp directories: a run's session directory outlives a day while the run does


def test_prune_leaves_a_live_runs_session_directory(tmp_path: Path) -> None:
    root = SessionRoot(tmp_path / f"prunectl-{os.getpid()}")
    live = Session(root, "live")
    directory = live.directory()
    dead = root.make() / "dead"
    dead.mkdir()
    old = time.time() - STALE_SESSION_SECONDS - 60
    for path in (directory, dead):
        os.utime(path, (old, old))
    prune(root, time.time())
    assert directory.is_dir() and not dead.exists()
    live.remove()
    assert not directory.exists()


# Flag defaults, empty config flags, and schema version pins


def test_a_default_that_breaks_its_own_pattern_or_size_is_refused() -> None:
    for spec in (dict(pattern="[a-z]+", default="ABC"), dict(max_bytes=3, default="toolong")):
        app = App("defctl", version="1.0.0")

        @dataclass(frozen=True, slots=True)
        class Args:
            name: str = Flag(description="A name", **spec)  # type: ignore[call-overload]

        with pytest.raises(RegistrationError, match="default"):

            @app.command("go", description="Go", danger_level="safe", exit_codes=())
            def go(args: Args, ctx: Ctx) -> None:
                return None


def plain_app() -> App:
    app = App("pinctl", version="1.0.0")

    @app.command("go", description="Go", danger_level="safe", exit_codes=())
    def go(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    return app


@pytest.mark.parametrize("flag", ["--config=", "--context=", "--instance-id="])
def test_an_empty_config_flag_is_an_argument_error(flag: str) -> None:
    code, envelope, _ = run(plain_app(), ["go", flag])
    assert code == 2, envelope


@pytest.mark.parametrize(
    ("pin", "code"), [("1", 0), ("1.0", 0), ("1.5", 2), ("1.garbage", 2), ("1.0.0", 2)]
)
def test_schema_version_takes_a_major_or_a_served_major_minor(pin: str, code: int) -> None:
    assert run(plain_app(), ["go", "--schema-version", pin])[0] == code


# A global option's name as a command flag's value


@dataclass(frozen=True, slots=True)
class Said:
    say: str = Flag(description="What to say")


def echo_app() -> App:
    app = App("echoctl", version="1.0.0")

    @app.command("echo", description="Echo", danger_level="safe", exit_codes=())
    def echo(args: Said, ctx: Ctx) -> dict[str, str]:
        return {"said": args.say}

    return app


@pytest.mark.parametrize("value", ["-h", "--help", "--format", "--schema"])
def test_a_global_options_name_is_a_flags_value_when_the_flag_takes_one(value: str) -> None:
    code, envelope, _ = run(echo_app(), ["echo", "--say", value])
    assert code == 0, envelope
    assert envelope["data"] == {"said": value}


def test_help_after_a_flags_value_is_still_help() -> None:
    code, envelope, _ = run(echo_app(), ["echo", "--say", "hi", "-h"])
    assert code == 0 and envelope["data"] != {"said": "hi"}


# Renderers and app import errors


def test_a_renderer_that_returns_no_text_fails_the_run_with_an_error() -> None:
    app = App("rendctl", version="1.0.0")
    app.format(Format.PLAIN, render=lambda data: None)  # type: ignore[arg-type,return-value]

    @app.command("go", description="Go", danger_level="safe", exit_codes=())
    def go(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"a": "b"}

    err = io.StringIO()
    code = app.run(["go", "--format", "plain"], stdout=io.StringIO(), stderr=err, env=BASE_ENV)
    assert code == 1
    assert "returned NoneType, not str" in err.getvalue()
    assert "HANDLER_CRASHED: the plain renderer failed" in err.getvalue()


def test_an_app_that_fails_to_register_is_a_precondition_not_a_treaty_crash(
    tmp_path: Path,
) -> None:
    (tmp_path / "badctl.py").write_text(
        "from treaty import App, Ctx\n"
        "app = App('badctl', version='1.0.0')\n"
        "@app.command('go', description='Go', danger_level='safe', exit_codes=())\n"
        "def go(args: int, ctx: Ctx) -> None:\n"
        "    return None\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", "from treaty._cli import main; main()", "audit", "badctl:app"],
        cwd=tmp_path,
        env={**BASE_ENV, "TREATY_AUDIT_LOG": "off"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 4, proc.stdout + proc.stderr
    error = json.loads(proc.stdout)["error"]
    assert error["code"] == "APP_IMPORT_FAILED"
    assert error["context"]["exception"] == "RegistrationError"


# cleanup


def test_the_cleanup_preview_names_the_scope_once(tmp_path: Path) -> None:
    (tmp_path / "cache").mkdir()
    app = App("fx", version="1.0.0")

    @app.command(
        "fetch",
        description="Fetch",
        danger_level="safe",
        exit_codes=(),
        filesystem_side_effects=[SideEffect(f"{tmp_path}/cache/", "cache")],
    )
    def fetch(args: NoArgs, ctx: Ctx) -> None:
        return None

    for scope, summary in (("all", "Removes 1 paths"), ("cache", "Removes 1 cache paths")):
        code, envelope, _ = run(app, ["cleanup", "--dry-run", "--scope", scope])
        assert code == 0, envelope
        data = envelope["data"]
        assert isinstance(data, dict) and data["would_affect"]["summary"] == summary
