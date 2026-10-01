"""ctx.run(stream="always"): a child's log on stderr as plain text, whatever the format
and verbosity, for a command that declares child_log=True (#173)"""

import io
import json
import os
import re
import sys
import threading
from dataclasses import dataclass

import pytest
from conftest import spec_validator

from treaty import App, Ctx, Flag, RegistrationError
from treaty._command import CHILD_LOG_NOTE
from treaty._subprocess import Processes, Stream

# Each child is a tiny Python script, so these run on every platform
PRINT = "import sys\nfor line in sys.argv[1:]:\n    print(line, flush=True)\n"
BASE_ENV = {"PATH": os.environ["PATH"], "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
BOTH = "import sys\nprint('out', flush=True)\nprint('err', file=sys.stderr, flush=True)\n"


@dataclass(frozen=True, slots=True)
class Child:
    code: str = Flag(description="The Python source the child runs", multiline=True)
    lines: tuple[str, ...] = Flag(default=(), description="Arguments for the child")
    token: str | None = Flag(default=None, description="A credential the child receives")


def make_app() -> App:
    app = App("childlog", version="1.0.0")

    @app.command("deploy", description="Deploy", danger_level="safe", exit_codes=(), child_log=True)
    def deploy(args: Child, ctx: Ctx) -> dict[str, object]:
        extra = [] if args.token is None else [args.token]
        done = ctx.run([sys.executable, "-c", args.code, *args.lines, *extra], stream="always")
        return {"stdout": done.stdout, "stderr": done.stderr}

    @app.command("follow", description="Follow", danger_level="safe", exit_codes=())
    def follow(args: Child, ctx: Ctx) -> dict[str, object]:
        done = ctx.run([sys.executable, "-c", args.code, *args.lines], stream=True)
        return {"stdout": done.stdout, "stderr": done.stderr}

    @app.command("undeclared", description="Undeclared", danger_level="safe", exit_codes=())
    def undeclared(args: Child, ctx: Ctx) -> dict[str, object]:
        ctx.run([sys.executable, "-c", args.code], stream="always")
        return {}

    @app.command(
        "both", description="Two children", danger_level="safe", exit_codes=(), child_log=True
    )
    def both(args: Child, ctx: Ctx) -> dict[str, object]:
        # Two children at once, on two threads of the handler
        def one(tag: str) -> None:
            lines = [f"{tag}{i:04d}-" + "x" * 200 for i in range(500)]
            ctx.run([sys.executable, "-c", PRINT, *lines], stream="always")

        threads = [threading.Thread(target=one, args=(tag,)) for tag in "ab"]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return {}

    return app


APP = make_app()


def run(
    command: str, code: str, *extra: str, isatty: bool = False, **env: str
) -> tuple[int, dict[str, object] | None, str]:
    out, err = io.StringIO(), io.StringIO()
    rc = APP.run(
        [command, "--code", code, *extra],
        stdin=io.StringIO(),
        stdout=out,
        stderr=err,
        env={**BASE_ENV, **env},
        isatty=isatty,
    )
    text = out.getvalue()
    return rc, json.loads(text) if text.startswith("{") else None, err.getvalue()


def unix(data: object) -> object:
    """The child's output as written, with CRLF on Windows made LF"""
    assert isinstance(data, dict)
    return {k: str(v).replace("\r\n", "\n") for k, v in data.items()}


def lines_of(*values: str) -> list[str]:
    return [f"--lines={v}" for v in values]


def test_the_lines_reach_stderr_off_a_terminal_without_verbose() -> None:
    code, envelope, err = run("deploy", BOTH, "--format", "json")
    assert code == 0 and envelope is not None
    assert sorted(err.splitlines()) == ["err", "out"]
    # Completed keeps its tails, as with stream=True
    assert unix(envelope["data"]) == {"stdout": "out\n", "stderr": "err\n"}


def test_stream_true_still_shows_only_where_ctx_log_does() -> None:
    code, _, err = run("follow", BOTH, "--format", "json")
    assert code == 0 and err == ""
    code, _, err = run("follow", BOTH, "--format", "json", "--verbose")
    assert code == 0 and sorted(json.loads(line)["message"] for line in err.splitlines()) == [
        "err",
        "out",
    ]


@pytest.mark.parametrize("fmt", ["json", "plain"])
def test_the_lines_are_plain_text_under_verbose_in_any_format(fmt: str) -> None:
    code, _, err = run("deploy", PRINT, *lines_of("TASK [one]"), "--format", fmt, "--verbose")
    assert code == 0 and err.splitlines() == ["TASK [one]"]


def test_quiet_silences_the_lines() -> None:
    code, envelope, err = run("deploy", BOTH, "--format", "json", "--quiet")
    assert code == 0 and err == "" and envelope is not None and envelope["ok"]


def test_a_secret_in_a_line_is_redacted() -> None:
    code, envelope, err = run(
        "deploy",
        PRINT,
        "--token-from-env",
        "CHILDLOG_TOKEN",
        "--format",
        "json",
        CHILDLOG_TOKEN="hunter2-secret",
    )
    assert code == 0 and envelope is not None
    assert "hunter2-secret" not in err and err.splitlines() == ["[REDACTED]"]


def test_escapes_and_carriage_returns_are_cleaned() -> None:
    script = (
        "import sys\n"
        "sys.stdout.buffer.write(b'\\x1b[31mred\\x1b[0m\\r\\n\\x1b]0;title\\x07a\\rb\\r\\n')\n"
    )
    code, _, err = run("deploy", script, "--format", "json")
    assert code == 0 and err.splitlines() == ["red", "a\\rb"]


def test_a_terminal_keeps_colors_unless_no_color() -> None:
    script = "import sys\nsys.stdout.write('\\x1b[31mred\\x1b[0m\\n')\n"
    code, _, err = run("deploy", script, "--format", "plain", isatty=True)
    assert code == 0 and err == "\x1b[31mred\x1b[0m\n"
    code, _, err = run("deploy", script, "--format", "plain", isatty=True, NO_COLOR="1")
    assert code == 0 and err == "red\n"


def test_concurrent_children_interleave_whole_lines() -> None:
    code, _, err = run("both", PRINT, "--format", "json")
    assert code == 0
    lines = err.splitlines()
    assert len(lines) == 1000
    assert all(re.fullmatch(r"[ab]\d{4}-x{200}", line) for line in lines)
    assert sorted(lines) == sorted(f"{t}{i:04d}-" + "x" * 200 for t in "ab" for i in range(500))


def test_app_call_drops_the_lines(capsys: pytest.CaptureFixture[str]) -> None:
    envelope = APP.call("deploy", {"code": BOTH})
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert envelope.ok and unix(envelope.data) == {"stdout": "out\n", "stderr": "err\n"}


def test_an_undeclared_command_cannot_stream_always() -> None:
    procs = Processes(BASE_ENV, deadline=None, headless=True, browser_open=False)
    with pytest.raises(RegistrationError, match="child_log=True"):
        procs.run([sys.executable, "-c", "pass"], stream=Stream.ALWAYS)
    code, envelope, _ = run("undeclared", "pass", "--format", "json")
    assert code != 0 and envelope is not None and not envelope["ok"]


@pytest.mark.parametrize("value", ["sometimes", "", 1, None])
def test_stream_takes_only_false_true_or_always(value: object) -> None:
    with pytest.raises(TypeError, match="always"):
        Stream.of(value)  # type: ignore[arg-type]


def test_stream_values() -> None:
    assert Stream.of(False) is Stream.OFF
    assert Stream.of(True) is Stream.LOG
    assert Stream.of("always") is Stream.ALWAYS


def test_the_manifest_description_says_stderr_carries_the_child_log() -> None:
    manifest = APP.manifest()
    spec_validator("manifest-response").validate(manifest)
    commands = manifest["commands"]
    assert commands["deploy"]["description"] == f"Deploy. {CHILD_LOG_NOTE}"
    assert commands["follow"]["description"] == "Follow"
