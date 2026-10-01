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
from treaty._subprocess import LINE_BYTES, Processes, Stream
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
        headless_behavior="emit_in_output",
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
            headless_behavior="emit_in_output",
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
    assert error_of(env)["message"] == "`sh` exited with 3"


# ctx.run(stream=True): a long child's lines as they arrive (#61)


@dataclass(frozen=True, slots=True)
class Streamed:
    argv: tuple[str, ...] = Flag(default=(), description="The program and its arguments")
    stdin: str | None = Flag(default=None, description="Text for the child's stdin", multiline=True)
    token: str | None = Flag(default=None, description="A credential the child receives")


def stream_app() -> App:
    app = App("streams", version="1.0.0")

    @app.command("follow", description="Stream argv", danger_level="safe", exit_codes=())
    def follow(args: Streamed, ctx: Ctx) -> dict[str, object]:
        done = ctx.run(list(args.argv), input=args.stdin, stream=True)
        return {"stdout": done.stdout, "stderr": done.stderr}

    return app


STREAMS = stream_app()


class Watched(io.StringIO):
    """The run's stderr: calls ``on_write`` with each write as it happens"""

    def __init__(self, on_write: object = None) -> None:
        super().__init__()
        self.on_write = on_write

    def write(self, text: str) -> int:
        if callable(self.on_write):
            self.on_write(text)
        return super().write(text)


def stream(
    argv: list[str], *extra: str, err: io.StringIO | None = None, isatty: bool = False, **env: str
) -> tuple[int, dict[str, object], str]:
    out, err = io.StringIO(), err if err is not None else io.StringIO()
    code = STREAMS.run(
        ["follow", *flags(argv), *extra, "--format", "json"],
        stdin=io.StringIO(),
        stdout=out,
        stderr=err,
        env={**BASE_ENV, **env},
        isatty=isatty,
    )
    return code, json.loads(out.getvalue()), err.getvalue()


def logged(err: str) -> list[str]:
    return [json.loads(line)["message"] for line in err.splitlines()]


def test_a_streamed_line_reaches_stderr_before_the_child_exits(tmp_path: Path) -> None:
    go = tmp_path / "go"
    # The child waits for the file the first line's echo creates: without streaming it
    # would never see it, and gives up after 10 seconds
    script = (
        'echo first; i=0; while [ ! -e "$1" ]; do sleep 0.01; i=$((i+1)); '
        "[ $i -gt 1000 ] && { echo late; exit 1; }; done; echo second >&2"
    )
    err = Watched(lambda text: "first" in text and go.touch())
    code, envelope, text = stream(["sh", "-c", script, "_", str(go)], "--verbose", err=err)
    assert code == 0, envelope
    assert logged(text) == ["first", "second"]
    assert envelope["data"] == {"stdout": "first\n", "stderr": "second\n"}


def test_streamed_lines_show_where_ctx_log_does() -> None:
    script = "echo out; echo err >&2"
    code, envelope, text = stream(["sh", "-c", script])
    assert code == 0 and text == ""  # off a terminal: errors only, as ctx.log
    code, _, text = stream(["sh", "-c", script], isatty=True)
    assert code == 0 and sorted(logged(text)) == ["err", "out"]


def test_a_streamed_child_keeps_only_the_tail_of_a_large_output() -> None:
    lines: list[str] = []
    done = Processes(
        BASE_ENV, deadline=None, headless=True, browser_open=False, echo=lines.append
    ).run(["sh", "-c", "seq 1 30000; seq 1 20000 >&2"], stream=Stream.LOG)
    # Every line is echoed, the two pipes interleaved
    assert len(lines) == 50000 and {"30000", "20000"} <= set(lines)
    assert len(done.stdout) == 4096 and done.stdout.endswith("\n29999\n30000\n")
    assert len(done.stderr) == 4096 and done.stderr.endswith("\n19999\n20000\n")


def test_a_failed_streamed_child_puts_its_stderr_tail_in_the_error() -> None:
    code, envelope, _ = stream(["sh", "-c", "seq 1 20000 >&2; exit 3"])
    assert code == 1 and error_of(envelope)["code"] == "SUBPROCESS_FAILED"
    tail = error_of(envelope)["context"]["stderr"]  # type: ignore[index]
    assert len(tail) == 4096 and tail.endswith("\n20000\n")


def test_a_secret_in_a_streamed_line_is_redacted() -> None:
    script = 'echo "using token $1"'
    out, err = io.StringIO(), io.StringIO()
    app = App("leaky", version="1.0.0")

    @app.command("x", description="x", danger_level="safe", exit_codes=())
    def x(args: SecretArg, ctx: Ctx) -> dict[str, object]:
        ctx.run(["sh", "-c", script, "_", args.token], stream=True)
        return {}

    code = app.run(
        ["x", "--token-from-env", "LEAKY_TOKEN", "--verbose", "--format", "json"],
        stdin=io.StringIO(),
        stdout=out,
        stderr=err,
        env={**BASE_ENV, "LEAKY_TOKEN": "hunter2-secret"},
    )
    assert code == 0 and "hunter2-secret" not in err.getvalue() + out.getvalue()
    assert logged(err.getvalue()) == ["using token [REDACTED]"]


def test_a_streamed_child_reads_its_input() -> None:
    code, envelope, text = stream(["cat"], "--stdin", "a\nb\n", "--verbose")
    assert code == 0 and logged(text) == ["a", "b"]
    assert envelope["data"] == {"stdout": "a\nb\n", "stderr": ""}


def test_a_streamed_child_that_runs_past_its_timeout_is_stopped() -> None:
    lines: list[str] = []
    procs = Processes(BASE_ENV, deadline=None, headless=True, browser_open=False, echo=lines.append)
    started = time.monotonic()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(CliExit) as stopped:
            procs.run(
                ["sh", "-c", "echo hi; exec sleep 30"], timeout=Timeout(0.5), stream=Stream.LOG
            )
        gc.collect()
    assert stopped.value.name.value == "TIMEOUT" and time.monotonic() - started < 5
    assert stopped.value.message.startswith("`sh` ran past")
    assert lines == ["hi"] and not procs.tracked
    assert not [w for w in caught if issubclass(w.category, ResourceWarning)]


def test_a_streamed_child_under_app_call_logs_at_info_and_keeps_stdout_clean(
    capsys: pytest.CaptureFixture[str],
) -> None:
    envelope = STREAMS.call("follow", {"argv": ["sh", "-c", "echo out; echo err >&2"]})
    captured = capsys.readouterr()
    # App.call runs off a terminal: ctx.log's INFO lines, these too, are not written
    assert captured.out == "" and captured.err == ""
    assert envelope.ok and envelope.data == {"stdout": "out\n", "stderr": "err\n"}


def test_the_command_deadline_stops_a_streamed_child(tmp_path: Path) -> None:
    app = App("deadline", version="1.0.0", default_timeout=0.5)

    @app.command("x", description="x", danger_level="safe", exit_codes=())
    def x(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        script = f'echo $$ > "{tmp_path / "pid"}"; echo hi; exec sleep 30'
        ctx.run(["sh", "-c", script], stream=True)
        return {}

    started = time.monotonic()
    code, env = call(["x"], app=app)
    assert code == 10 and error_of(env)["code"] == "TIMEOUT"
    assert time.monotonic() - started < 5
    assert_gone(int((tmp_path / "pid").read_text()))


PEM = "-----BEGIN KEY-----\nMIIEsecret-line-one\nMIIEsecret-line-two\n-----END KEY-----"


def test_each_line_of_a_multiline_secret_is_redacted_in_a_streamed_child() -> None:
    printer = [sys.executable, "-c", "import os; print(os.environ['PEM'])"]
    code, _, text = stream(printer, "--token-from-env", "PEM", "--verbose", PEM=PEM)
    assert code == 0
    assert not [line for line in PEM.splitlines() if line in text]
    assert logged(text) == ["[REDACTED]"] * 4


def test_a_secret_cut_by_the_line_split_is_redacted() -> None:
    secret = "s3cret-value-xyz"
    # The 64 KiB split falls inside the secret: its halves arrive as two chunks
    write = f"import sys; sys.stdout.write('a' * {LINE_BYTES - 5} + {secret!r} + '\\n')"
    code, _, text = stream(
        [sys.executable, "-c", write], "--token-from-env", "TOK", "--verbose", TOK=secret
    )
    assert code == 0
    assert secret[:5] not in text and secret[5:] not in text
    assert "".join(logged(text)) == "a" * (LINE_BYTES - 5) + "[REDACTED]"


@pytest.mark.skipif(WINDOWS, reason="needs setsid; Windows stops only the child")
def test_a_detached_grandchild_is_not_blocked_by_a_timed_out_stream(tmp_path: Path) -> None:
    done = tmp_path / "done"
    # The grandchild leaves the process group, so the timeout does not stop it; after
    # the run gives up it writes more lines than the queue and the pipe hold
    grandchild = (
        "import os, sys, time; os.setsid(); time.sleep(3); "
        "[print('x' * 100) for _ in range(5000)]; sys.stdout.flush(); "
        f"open({str(done)!r}, 'w').close()"
    )
    lines: list[str] = []
    procs = Processes(BASE_ENV, deadline=None, headless=True, browser_open=False, echo=lines.append)
    with pytest.raises(CliExit):
        procs.run(
            ["sh", "-c", '"$1" -c "$2" & echo hi; exec sleep 30', "_", sys.executable, grandchild],
            timeout=Timeout(0.5),
            stream=Stream.LOG,
        )
    deadline = time.monotonic() + 15
    while not done.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert done.exists()
