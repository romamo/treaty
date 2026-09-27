"""Subprocess API: REQ-F-044, F-046, F-055, F-057, F-062, F-065, F-030, F-031."""

import gc
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import WINDOWS, needs_posix_signals, spec_validator

from treaty import App, CliExit, Ctx, Flag, NoArgs, RegistrationError, Timeout
from treaty._mode import is_headless, quiet_children
from treaty._signals import Cancelled, CancelSignal
from treaty._subprocess import Processes
from treaty._values import CommandPath

pytestmark = pytest.mark.skipif(WINDOWS, reason="the children are /bin/sh commands")

PROCCTL = Path(__file__).resolve().parent / "fixture_subprocess_app.py"
BASE_ENV = {"PATH": os.environ["PATH"]}


class TerminalInput(io.StringIO):
    """Stands in for a terminal on stdin"""

    def isatty(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class Argv:
    argv: tuple[str, ...] = Flag(default=(), description="The program and its arguments")
    stdin: str | None = Flag(default=None, description="Text for the child's stdin", multiline=True)
    check: bool = Flag(default=True, description="Fail on a non-zero exit")
    child_timeout: float | None = Flag(default=None, description="Seconds for the child")


def make_app() -> App:
    app = App("procs", version="1.0.0")

    @app.command("run", description="Run argv", danger_level="safe", exit_codes=())
    def run_(args: Argv, ctx: Ctx) -> dict[str, object]:
        limit = None if args.child_timeout is None else Timeout(args.child_timeout)
        done = ctx.run(list(args.argv), input=args.stdin, timeout=limit, check=args.check)
        return {"stdout": done.stdout, "returncode": done.returncode, "stage": done.stage}

    @app.command("pipe", description="Run stages", danger_level="safe", exit_codes=())
    def pipe(args: Argv, ctx: Ctx) -> dict[str, object]:
        # Stages separated by a lone "|" token
        stages: list[list[str]] = [[]]
        for token in args.argv:
            if token == "|":
                stages.append([])
            else:
                stages[-1].append(token)
        done = ctx.pipeline(stages, input=args.stdin, check=args.check)
        return {"stdout": done.stdout, "returncode": done.returncode, "stage": done.stage}

    @app.command("shell", description="Pass a string", danger_level="safe", exit_codes=())
    def shell(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        command = "ls -la"
        ctx.run(command)  # type: ignore[arg-type]
        return {}

    return app


APP = make_app()


def call(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    app: App = APP,
    stdin: io.StringIO | None = None,
    isatty: bool = False,
) -> tuple[int, dict[str, object]]:
    out = io.StringIO()
    code = app.run(
        [*argv, "--format", "json"],
        stdin=stdin if stdin is not None else io.StringIO(),
        stdout=out,
        stderr=io.StringIO(),
        env={**BASE_ENV, **(env or {})},
        isatty=isatty,
    )
    return code, json.loads(out.getvalue())


def run_argv(*argv: str, **kw: object) -> tuple[int, dict[str, object]]:
    return call(["run", *flags(argv)], **kw)  # type: ignore[arg-type]


def flags(argv: object) -> list[str]:
    """``--argv=<item>`` per item, so items that start with a dash stay values"""
    assert isinstance(argv, (list, tuple))
    return [f"--argv={item}" for item in argv]


def stdout_of(envelope: dict[str, object]) -> str:
    data = envelope["data"]
    assert isinstance(data, dict)
    return str(data["stdout"])


def error_of(envelope: dict[str, object]) -> dict[str, object]:
    error = envelope["error"]
    assert isinstance(error, dict)
    return error


# F-044, F-062: arguments reach the program literally


def test_injected_command_is_a_literal_argument() -> None:
    code, env = run_argv("sh", "-c", 'printf "%s|" "$1"', "_", "; rm -rf /")
    assert code == 0 and stdout_of(env) == "; rm -rf /|"


def test_spaces_and_globs_arrive_as_one_literal_argument_each() -> None:
    code, env = run_argv("sh", "-c", 'printf "%s|" "$@"', "_", "hello world", "*.json")
    assert code == 0 and stdout_of(env) == "hello world|*.json|"


def test_a_shell_string_at_run_time_is_shell_string_prohibited() -> None:
    code, env = call(["shell"])
    assert code == 1
    assert error_of(env)["code"] == "SHELL_STRING_PROHIBITED"


def literal(args: NoArgs, ctx: Ctx) -> dict[str, object]:
    ctx.run("git log")  # type: ignore[arg-type]
    return {}


def formatted(args: NoArgs, ctx: Ctx) -> dict[str, object]:
    ctx.run(f"git log {args}")  # type: ignore[arg-type]
    return {}


def concatenated(args: NoArgs, ctx: Ctx) -> dict[str, object]:
    ctx.run("git " + "log")  # type: ignore[arg-type]
    return {}


def stage_string(args: NoArgs, ctx: Ctx) -> dict[str, object]:
    ctx.pipeline([["ls"], "sort"])  # type: ignore[list-item]
    return {}


@pytest.mark.parametrize("handler", [literal, formatted, concatenated, stage_string])
def test_a_shell_string_in_the_source_fails_registration(handler: object) -> None:
    app = App("shells", version="1.0.0")
    register = app.command("x", description="x", danger_level="safe", exit_codes=())
    with pytest.raises(RegistrationError, match="SHELL_STRING_PROHIBITED"):
        register(handler)  # type: ignore[arg-type]


def test_a_shell_keyword_elsewhere_registers() -> None:
    app = App("shells", version="1.0.0")

    @app.command("x", description="x", danger_level="safe", exit_codes=())
    def x(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        ctx.log("started", shell="bash")
        return {}

    assert CommandPath("x") in app.commands


# F-008, F-010, F-046, F-055: the child's environment


def test_children_and_grandchildren_never_page_or_color() -> None:
    parent = {"PAGER": "less", "GIT_PAGER": "less", "MANPAGER": "less", "LESS": "-R"}
    script = 'echo "$PAGER $NO_COLOR $GIT_PAGER $MANPAGER [$LESS] [$MORE]"'
    code, env = run_argv("sh", "-c", script, env=parent)
    assert code == 0 and stdout_of(env) == "cat 1 cat cat [-F -X -R] []\n"
    code, env = run_argv("sh", "-c", f"sh -c '{script}'", env=parent)
    assert stdout_of(env) == "cat 1 cat cat [-F -X -R] []\n"


def test_env_overrides_single_variables(tmp_path: Path) -> None:
    app = App("envs", version="1.0.0")

    @app.command("x", description="x", danger_level="safe", exit_codes=())
    def x(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        done = ctx.run(["sh", "-c", 'echo "$PAGER $EXTRA"'], env={"EXTRA": "yes"}, cwd=tmp_path)
        return {"stdout": done.stdout}

    assert stdout_of(call(["x"], app=app)[1]) == "cat yes\n"


def test_editors_are_no_ops_off_a_terminal() -> None:
    script = 'echo "$EDITOR $VISUAL $GIT_EDITOR"'
    code, env = run_argv("sh", "-c", script, env={"EDITOR": "vim", "VISUAL": "vim"})
    assert code == 0 and stdout_of(env) == "true true true\n"


def test_editors_are_kept_on_a_terminal() -> None:
    script = 'echo "$EDITOR $VISUAL"'
    code, env = run_argv(
        "sh",
        "-c",
        script,
        env={"EDITOR": "vim", "VISUAL": "code"},
        stdin=TerminalInput(),
        isatty=True,
    )
    assert code == 0 and stdout_of(env) == "vim code\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_log_never_waits_for_a_pager(tmp_path: Path) -> None:
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], check=True, env=BASE_ENV)
    for n in range(5):
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", f"c{n}"], check=True)
    started = time.monotonic()
    code, env = run_argv(*git, "-p", "log", "--oneline", env={"PAGER": "less", "GIT_PAGER": "less"})
    assert code == 0 and stdout_of(env).count("\n") == 5
    assert time.monotonic() - started < 5


def test_app_main_settings_reach_every_child() -> None:
    env: dict[str, str] = {"EDITOR": "vim"}
    quiet_children(env, stdout_isatty=False, stdin_isatty=False)
    assert env["MANPAGER"] == "cat" and env["LESS"] == "-F -X -R" and env["MORE"] == ""
    assert env["EDITOR"] == env["VISUAL"] == env["GIT_EDITOR"] == "true"
    kept = {"EDITOR": "vim"}
    quiet_children(kept, stdout_isatty=True, stdin_isatty=True)
    assert kept["EDITOR"] == "vim" and kept["PAGER"] == "cat"


# Stdin, failures, timeouts


def test_stdin_is_dev_null_unless_input_is_given() -> None:
    started = time.monotonic()
    assert stdout_of(run_argv("cat")[1]) == ""
    assert time.monotonic() - started < 5
    code, env = call(["run", *flags(["cat"]), "--stdin", "fed"])
    assert stdout_of(env) == "fed"


def test_a_non_zero_exit_is_subprocess_failed_with_stderr() -> None:
    code, env = run_argv("sh", "-c", "echo broken >&2; exit 3")
    error = error_of(env)
    assert code == 1 and error["code"] == "SUBPROCESS_FAILED"
    assert error["context"] == {
        "argv": ["sh", "-c", "echo broken >&2; exit 3"],
        "returncode": 3,
        "stage": 0,
        "stderr": "broken\n",
    }


def test_check_false_returns_the_exit_code() -> None:
    code, env = call(["run", *flags(["sh", "-c", "exit 3"]), "--no-check"])
    assert code == 0 and env["data"] == {"stdout": "", "returncode": 3, "stage": 0}


def test_a_missing_program_is_subprocess_failed() -> None:
    code, env = run_argv("no-such-program-treaty")
    assert code == 1 and error_of(env)["code"] == "SUBPROCESS_FAILED"
    assert error_of(env)["context"]["cause"]  # type: ignore[index]


def test_a_child_past_its_timeout_is_stopped(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    script = 'echo $$ > "$1"; exec sleep 30'
    started = time.monotonic()
    code, env = call(
        ["run", *flags(["sh", "-c", script, "_", str(pidfile)])] + ["--child-timeout", "0.5"]
    )
    assert code == 10 and error_of(env)["code"] == "TIMEOUT"
    assert error_of(env)["context"]["argv"][0] == "sh"  # type: ignore[index]
    assert time.monotonic() - started < 5
    assert_gone(int(pidfile.read_text()))


def test_the_command_deadline_limits_its_children(tmp_path: Path) -> None:
    app = App("deadline", version="1.0.0", default_timeout=0.5)

    @app.command("x", description="x", danger_level="safe", exit_codes=())
    def x(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        ctx.run(["sh", "-c", f'echo $$ > "{tmp_path / "pid"}"; exec sleep 30'])
        return {}

    code, env = call(["x"], app=app)
    assert code == 10 and error_of(env)["code"] == "TIMEOUT"
    assert_gone(int((tmp_path / "pid").read_text()))


# F-065: pipelines


def test_a_pipeline_connects_stages_with_pipes() -> None:
    code, env = call(["pipe", *flags(["cat", "|", "sort"]), "--stdin", "b\na\n"])
    assert code == 0 and stdout_of(env) == "a\nb\n"


def test_a_failed_first_stage_fails_the_pipeline() -> None:
    code, env = call(["pipe", *flags(["false", "|", "true"])])
    error = error_of(env)
    assert code == 1 and error["code"] == "SUBPROCESS_FAILED"
    assert error["context"]["stage"] == 0 and error["context"]["argv"] == ["false"]  # type: ignore[index]


def test_check_false_reports_the_first_failing_stage() -> None:
    code, env = call(["pipe", *flags(["true", "|", "false", "|", "true"]), "--no-check"])
    assert code == 0 and env["data"] == {"stdout": "", "returncode": 1, "stage": 1}


# F-030, F-031: signals reach tracked children


@needs_posix_signals
@pytest.mark.parametrize("command", ["hold", "hold-threaded"])
def test_sigterm_stops_the_child_before_the_envelope(command: str, tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    proc = subprocess.Popen(
        [sys.executable, str(PROCCTL), command, "--pidfile", str(pidfile)],
        env=BASE_ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not (pidfile.exists() and pidfile.read_text().strip()):
            assert time.monotonic() < deadline, "the child never started"
            time.sleep(0.02)
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 143
    assert json.loads(out)["error"]["code"] == "CANCELLED"
    assert_gone(int(pidfile.read_text()))


def assert_gone(pid: int) -> None:
    """The pid no longer names a live process; a zombie awaiting its reaper counts"""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
        ).stdout.strip()
        if not state or state.startswith("Z"):
            return
        time.sleep(0.05)
    raise AssertionError(f"process {pid} still runs")


# F-057, C-024: headless runs


@dataclass(frozen=True, slots=True)
class Login:
    status: str
    open_url: str | None = None


def browser_app() -> App:
    app = App("gui", version="1.0.0")

    @app.command(
        "login",
        description="Log in through the browser",
        danger_level="safe",
        exit_codes=(),
        gui_operations=["browser_open"],
    )
    def login(args: NoArgs, ctx: Ctx) -> Login:
        opened = ctx.open_url("https://example.com/device?code=ABC")
        return Login(status="opened" if opened else "waiting")

    return app


def test_open_url_headless_returns_the_url_in_data() -> None:
    started = time.monotonic()
    code, env = call(["login"], app=browser_app())
    assert time.monotonic() - started < 2
    assert code == 0
    assert env["data"] == {"status": "waiting", "open_url": "https://example.com/device?code=ABC"}
    assert env["meta"]["headless"] is True  # type: ignore[index]


def test_every_response_says_headless() -> None:
    code, env = call(["run", "--no-such-flag"])
    assert code == 2 and env["meta"]["headless"] is True  # type: ignore[index]


def test_the_manifest_declares_gui_operations() -> None:
    manifest = browser_app().manifest()
    entry = manifest["commands"]["login"]  # type: ignore[index]
    assert entry["gui_operations"] == ["browser_open"]
    assert entry["headless_behavior"] == "emit_in_output"
    spec_validator("manifest-response").validate(manifest)


def test_open_url_without_gui_operations_fails_registration() -> None:
    app = App("gui", version="1.0.0")
    with pytest.raises(RegistrationError, match="gui_operations"):

        @app.command("x", description="x", danger_level="safe", exit_codes=())
        def x(args: NoArgs, ctx: Ctx) -> Login:
            ctx.open_url("https://example.com")
            return Login(status="x")


def test_gui_operations_need_an_open_url_field() -> None:
    app = App("gui", version="1.0.0")
    with pytest.raises(RegistrationError, match="open_url"):

        @app.command(
            "x",
            description="x",
            danger_level="safe",
            exit_codes=(),
            gui_operations=["browser_open"],
        )
        def x(args: NoArgs, ctx: Ctx) -> Argv:
            return Argv()


@pytest.mark.parametrize(
    ("env", "interactive", "platform", "headless"),
    [
        ({}, False, "darwin", True),
        ({"CI": "true"}, True, "darwin", True),
        ({}, True, "darwin", False),
        ({"SSH_TTY": "/dev/ttys001"}, True, "darwin", True),
        ({}, True, "linux", True),
        ({"DISPLAY": ":0"}, True, "linux", False),
        ({"WAYLAND_DISPLAY": "wayland-0"}, True, "linux", False),
    ],
)
def test_headless_detection(
    env: dict[str, str], interactive: bool, platform: str, headless: bool
) -> None:
    assert is_headless(env, interactive=interactive, platform=platform) is headless


# Audit


def test_a_command_that_uses_os_system_directly_is_flagged_by_the_registration_linter() -> None:
    app = App("shelly", version="1.0.0")
    with pytest.raises(RegistrationError, match=r"os\.system\(\) on line 3 .*ctx\.run"):

        @app.command("x", description="x", danger_level="safe", exit_codes=())
        def x(args: NoArgs, ctx: Ctx) -> dict[str, object]:
            os.system("ls")
            return {}


def test_os_popen_and_shell_true_fail_registration() -> None:
    app = App("shelly", version="1.0.0")
    with pytest.raises(RegistrationError, match=r"os\.popen\(\)"):

        @app.command("p", description="x", danger_level="safe", exit_codes=())
        def p(args: NoArgs, ctx: Ctx) -> dict[str, object]:
            os.popen("ls")
            return {}

    with pytest.raises(RegistrationError, match=r"subprocess\.run\(\)"):

        @app.command("s", description="x", danger_level="safe", exit_codes=())
        def s(args: NoArgs, ctx: Ctx) -> dict[str, object]:
            subprocess.run("ls", shell=True, check=False)
            return {}

    @app.command("ok", description="x", danger_level="safe", exit_codes=())
    def ok(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        subprocess.run(["ls"], shell=False, check=False)
        return {}


# Review fixes: secrets, children after the run, grandchildren, SIGPIPE, messages


@dataclass(frozen=True, slots=True)
class SecretArg:
    token: str = Flag(description="A credential the child receives")


def test_a_failed_child_never_echoes_a_secret_to_stdout() -> None:
    app = App("leaky", version="1.0.0")

    @app.command("x", description="x", danger_level="safe", exit_codes=())
    def x(args: SecretArg, ctx: Ctx) -> dict[str, object]:
        ctx.run(["sh", "-c", 'echo "bad token $1" >&2; exit 3', "_", args.token])
        return {}

    out = io.StringIO()
    code = app.run(
        ["x", "--token-from-env", "LEAKY_TOKEN", "--format", "json"],
        stdin=io.StringIO(),
        stdout=out,
        stderr=io.StringIO(),
        env={**BASE_ENV, "LEAKY_TOKEN": "hunter2-secret"},
    )
    assert code == 1 and "hunter2-secret" not in out.getvalue()
    context = error_of(json.loads(out.getvalue()))["context"]
    assert context["argv"][-1] == "[REDACTED]"  # type: ignore[index]
    assert context["stderr"] == "bad token [REDACTED]\n"  # type: ignore[index]


def processes(deadline: float | None = None) -> Processes:
    return Processes(BASE_ENV, deadline=deadline, headless=True, browser_open=False)


def test_no_child_starts_after_terminate(tmp_path: Path) -> None:
    timed_out = processes()
    timed_out.terminate()
    with pytest.raises(CliExit) as caught:
        timed_out.run(["sh", "-c", f'touch "{tmp_path / "ran"}"'])
    assert caught.value.name.value == "TIMEOUT"
    cancelled = processes()
    cancelled.terminate(CancelSignal("SIGTERM", 143))
    with pytest.raises(Cancelled):
        cancelled.run(["sh", "-c", f'touch "{tmp_path / "ran"}"'])
    assert not (tmp_path / "ran").exists()


def test_an_abandoned_handler_starts_no_child(tmp_path: Path) -> None:
    app = App("late", version="1.0.0", default_timeout=0.3)
    refused: list[BaseException] = []
    done = threading.Event()

    @app.command("x", description="x", danger_level="safe", exit_codes=())
    def x(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        time.sleep(1)  # outlive the 0.3 s deadline, so the run abandons this handler
        try:
            ctx.run(["sh", "-c", f'touch "{tmp_path / "ran"}"; exec sleep 30'])
        except CliExit as exc:
            refused.append(exc)
        finally:
            done.set()
        return {}

    code, env = call(["x"], app=app)
    assert code == 10 and error_of(env)["code"] == "TIMEOUT"
    assert done.wait(10)
    assert len(refused) == 1 and not (tmp_path / "ran").exists()


def test_an_explicit_timeout_is_capped_by_the_command_deadline() -> None:
    started = time.monotonic()
    with pytest.raises(CliExit) as caught:
        processes(time.monotonic() + 0.3).run(["sleep", "30"], timeout=Timeout(30))
    assert caught.value.name.value == "TIMEOUT" and time.monotonic() - started < 5


def test_a_background_grandchild_is_stopped_with_its_group(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    script = f'sleep 30 & echo $! > "{pidfile}"; echo hi'
    started = time.monotonic()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(CliExit) as stopped:
            processes().run(["sh", "-c", script], timeout=Timeout(0.5))
        gc.collect()
    assert stopped.value.name.value == "TIMEOUT" and time.monotonic() - started < 5
    assert stopped.value.message.startswith("`sh` ran past")
    assert_gone(int(pidfile.read_text()))
    assert not [w for w in caught if issubclass(w.category, ResourceWarning)]


def test_a_pipeline_names_the_stage_that_hung() -> None:
    with pytest.raises(CliExit) as caught:
        processes().pipeline([["sleep", "30"], ["cat"]], timeout=Timeout(0.3))
    assert caught.value.context["stage"] == 0
    assert caught.value.message.startswith("`sleep` (stage 0) ran past")


def test_sigpipe_of_an_upstream_stage_is_not_a_failure() -> None:
    code, env = call(["pipe", *flags(["yes", "|", "head", "-1"])])
    assert code == 0 and stdout_of(env) == "y\n"
    code, env = call(["pipe", *flags(["yes", "|", "false"])])
    assert code == 1 and error_of(env)["code"] == "SUBPROCESS_FAILED"


def test_a_program_name_keeps_its_case_in_the_message() -> None:
    code, env = run_argv("sh", "-c", "exit 3")
    assert error_of(env)["message"] == "`sh` exited with 3."
