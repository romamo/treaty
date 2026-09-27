"""Interactivity: REQ-F-009, F-047, F-055, C-005, C-023."""

import io
import json
import os
import select
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import pytest
from conftest import WINDOWS, spec_validator
from fixture_prompt_app import app

from treaty import App, Ctx, Flag, NoArgs, RegistrationError

ASKCTL = Path(__file__).resolve().parent / "fixture_prompt_app.py"
BASE_ENV = {"PATH": os.environ["PATH"]}


class Terminal(io.StringIO):
    """Stands in for a terminal on stdin, with the person's typed lines"""

    def isatty(self) -> bool:
        return True


def run(
    argv: list[str],
    *,
    stdin: IO[str] | None = None,
    isatty: bool = False,
    env: dict[str, str] | None = None,
) -> tuple[int, dict[str, object], str]:
    out, err = io.StringIO(), io.StringIO()
    code = app.run(
        [*argv, "--format", "json"],
        stdin=stdin if stdin is not None else io.StringIO(),
        stdout=out,
        stderr=err,
        env={**BASE_ENV, **(env or {})},
        isatty=isatty,
    )
    return code, json.loads(out.getvalue()), err.getvalue()


def error_of(envelope: dict[str, object]) -> dict[str, object]:
    error = envelope["error"]
    assert isinstance(error, dict)
    return error


# F-009, C-005: prompts off a terminal


def test_a_prompt_with_stdin_dev_null_exits_4_at_once() -> None:
    started = time.monotonic()
    with open(os.devnull, encoding="utf-8") as devnull:
        code, env, _ = run(["init"], stdin=devnull)
    assert time.monotonic() - started < 1
    error = error_of(env)
    assert code == 4 and error["code"] == "INPUT_REQUIRED"
    assert error["phase"] == "execution" and error["retryable"] is False
    assert "--name" in str(error["suggestion"])
    assert error["context"] == {"prompt": "Project name", "flag": "name"}


def test_yes_answers_a_confirmation() -> None:
    code, env, _ = run(["init", "--name", "demo", "--yes"])
    assert code == 0 and env["data"] == {"name": "demo", "overwrite": True}


def test_a_confirmation_off_a_terminal_names_yes() -> None:
    code, env, _ = run(["init", "--name", "demo"])
    assert code == 4 and "--yes" in str(error_of(env)["suggestion"])


def test_yes_on_a_command_that_never_prompts_is_a_no_op() -> None:
    code, env, _ = run(["status", "--yes"])
    assert code == 0 and env["data"] == {"status": "ok"}


def test_non_interactive_on_a_terminal_still_exits_4() -> None:
    code, env, _ = run(["init", "--non-interactive"], stdin=Terminal("alice\n"), isatty=True)
    assert code == 4 and error_of(env)["code"] == "INPUT_REQUIRED"


def test_a_terminal_is_asked_and_answers() -> None:
    code, env, err = run(["init"], stdin=Terminal("alice\ny\n"), isatty=True)
    assert code == 0 and env["data"] == {"name": "alice", "overwrite": True}
    assert "Project name: " in err and "Overwrite existing files? [y/N] " in err


def test_exec_and_app_call_answer_with_yes() -> None:
    envelope = app.call("init", {"name": "demo", "yes": True}, env=BASE_ENV)
    assert envelope.exit_code == 0 and envelope.data == {"name": "demo", "overwrite": True}
    refused = app.call("init", {"name": "demo"}, env=BASE_ENV)
    assert refused.exit_code == 4


@pytest.mark.skipif(WINDOWS, reason="pseudo-terminals are POSIX")
def test_a_real_pseudo_terminal_prompts_and_reads_the_answer() -> None:
    import pty

    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, str(ASKCTL), "init"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=BASE_ENV,
    )
    os.close(slave)
    output = b""
    try:
        os.write(master, b"alice\ny\n")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break  # Linux: the slave side closed
                if not chunk:
                    break
                output += chunk
            elif proc.poll() is not None:
                break
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        os.close(master)
    text = output.decode()
    assert proc.returncode == 0, text
    assert "Project name:" in text and "name: alice" in text and "overwrite: true" in text


# F-047: stray input() and empty invocations


def test_input_under_a_pipe_exits_4_even_inside_except_exception() -> None:
    code, env, _ = run(["ask"], stdin=io.StringIO("yes\n"))
    error = error_of(env)
    assert code == 4 and error["code"] == "INTERACTIVE_BLOCKED"
    assert "interactive=True" in str(error["suggestion"])


def test_piped_data_is_still_readable() -> None:
    code, env, _ = run(["slurp"], stdin=io.StringIO("a\nb\n"))
    assert code == 0 and env["data"] == {"lines": ["a", "b"]}


def test_no_arguments_off_a_terminal_prints_help_at_once() -> None:
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(ASKCTL)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=BASE_ENV,
        timeout=10,
        check=False,
    )
    assert time.monotonic() - started < 5
    assert proc.returncode == 0 and json.loads(proc.stdout)["meta"]["help"] is True


# F-055, C-023: editors


def test_an_editor_off_a_terminal_exits_4_with_alternatives() -> None:
    code, env, _ = run(["commit"], env={"EDITOR": "vim"})
    error = error_of(env)
    assert code == 4 and error["code"] == "EDITOR_REQUIRED"
    assert error["alternatives"] == [
        {"flag": "--message", "description": "Supplies the text instead of the editor"}
    ]
    assert "--message" in str(error["suggestion"])


def test_the_alternative_flag_bypasses_the_editor() -> None:
    code, env, _ = run(["commit", "--message", "fix: typo"])
    assert code == 0 and env["data"] == {"message": "fix: typo"}


@pytest.mark.skipif(WINDOWS, reason="the editor is a /bin/sh command")
def test_an_editor_on_a_terminal_edits_the_text() -> None:
    editor = 'sh -c \'printf "edited after: %s" "$(cat "$1")" > "$1"\' _'
    code, env, _ = run(["commit"], stdin=Terminal(), isatty=True, env={"EDITOR": editor})
    assert code == 0
    assert env["data"] == {"message": "edited after: # Describe the change"}


# Declarations


@dataclass(frozen=True, slots=True)
class Named:
    name: str | None = Flag(default=None, description="Name")


def test_prompting_without_interactive_fails_registration() -> None:
    other = App("x", version="1")
    with pytest.raises(RegistrationError, match="interactive=True"):

        @other.command("x", description="x", danger_level="safe", exit_codes=())
        def x(args: NoArgs, ctx: Ctx) -> dict[str, str]:
            return {"name": ctx.prompt("Name", flag="name")}


def test_an_editor_without_alternatives_fails_registration() -> None:
    other = App("x", version="1")
    with pytest.raises(RegistrationError, match="editor_alternatives"):

        @other.command("x", description="x", danger_level="safe", exit_codes=())
        def x(args: NoArgs, ctx: Ctx) -> dict[str, str]:
            return {"text": ctx.edit()}


def test_editor_alternatives_must_be_flags() -> None:
    other = App("x", version="1")
    with pytest.raises(RegistrationError, match="not flags"):

        @other.command(
            "x", description="x", danger_level="safe", exit_codes=(), editor_alternatives=["body"]
        )
        def x(args: Named, ctx: Ctx) -> dict[str, str]:
            return {}


def test_a_yes_field_collides_on_an_interactive_command() -> None:
    @dataclass(frozen=True, slots=True)
    class Yes:
        yes: bool = Flag(default=False, description="Yes")

    other = App("x", version="1")
    with pytest.raises(RegistrationError, match="supplied by the framework"):

        @other.command("x", description="x", danger_level="safe", exit_codes=(), interactive=True)
        def x(args: Yes, ctx: Ctx) -> dict[str, str]:
            return {}


def test_manifest_and_schema_declare_prompts_and_editors() -> None:
    manifest = app.manifest()
    spec_validator("manifest-response").validate(manifest)
    commands = manifest["commands"]
    assert isinstance(commands, dict)
    init = commands["init"]
    assert init["interactive"] is True
    assert {"yes", "non-interactive"} <= set(init["flags"])
    assert "4" in init["exit_codes"]
    assert commands["commit"]["requires_editor"] is True
    assert commands["commit"]["non_interactive_alternatives"] == ["message"]
    assert "interactive" not in commands["ask"]
    code, env, _ = run(["init", "--schema"])
    assert code == 0 and env["data"]["interactive"] is True  # type: ignore[index]
