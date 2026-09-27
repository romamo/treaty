"""Additional command declarations: REQ-C-010, REQ-C-011, REQ-C-018, REQ-C-019, REQ-C-024,
REQ-O-031."""

import io
import json
import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import WINDOWS, spec_validator

from treaty import App, Arg, Ctx, Flag, NoArgs, RegistrationError, Subprocess
from treaty._audit import audit

SRC = Path(__file__).resolve().parents[1] / "src"
URL = "https://example.com/device?code=ABC"


@dataclass(frozen=True, slots=True)
class Login:
    status: str
    open_url: str | None = None


def run(
    app: App, argv: list[str], env: dict[str, str] | None = None
) -> tuple[int, dict[str, object]]:
    out = io.StringIO()
    code = app.run(
        [*argv, "--format", "json"],
        stdin=io.StringIO(),
        stdout=out,
        stderr=io.StringIO(),
        env={"PATH": os.environ["PATH"], **(env or {})},
    )
    return code, json.loads(out.getvalue())


# REQ-C-024


def gui_app(behavior: str) -> App:
    app = App("gui", version="1.0.0")

    @app.command(
        "login",
        description="Log in through the browser",
        danger_level="safe",
        exit_codes=(),
        gui_operations=["browser_open"],
        headless_behavior=behavior,
    )
    def login(args: NoArgs, ctx: Ctx) -> Login:
        opened = ctx.open_url(URL)
        return Login(status="opened" if opened else "waiting")

    return app


def test_emit_in_output_returns_data_open_url_in_headless_mode() -> None:
    code, env = run(gui_app("emit_in_output"), ["login"])
    assert code == 0
    assert env["data"] == {"status": "waiting", "open_url": URL}


def test_gui_operations_without_headless_behavior_raise_a_registration_error() -> None:
    app = App("gui", version="1.0.0")
    with pytest.raises(RegistrationError, match='headless_behavior="emit_in_output"'):

        @app.command(
            "login",
            description="x",
            danger_level="safe",
            exit_codes=(),
            gui_operations=["browser_open"],
        )
        def login(args: NoArgs, ctx: Ctx) -> Login:
            return Login(status="x")


def test_headless_behavior_without_gui_operations_fails_registration() -> None:
    app = App("gui", version="1.0.0")
    with pytest.raises(RegistrationError, match="gui_operations"):

        @app.command(
            "x", description="x", danger_level="safe", exit_codes=(), headless_behavior="skip"
        )
        def x(args: NoArgs, ctx: Ctx) -> Login:
            return Login(status="x")


def test_an_unknown_headless_behavior_fails_registration() -> None:
    app = App("gui", version="1.0.0")
    with pytest.raises(RegistrationError, match="emit_in_output, skip, error"):
        app.command(
            "x",
            description="x",
            danger_level="safe",
            exit_codes=(),
            gui_operations=["browser_open"],
            headless_behavior="open",
        )


def test_skip_opens_nothing_and_warns_gui_skipped() -> None:
    code, env = run(gui_app("skip"), ["login"])
    assert code == 0
    assert env["data"] == {"status": "waiting", "open_url": None}
    [warning] = env["warnings"]  # type: ignore[misc]
    assert warning["code"] == "GUI_SKIPPED" and warning["context"] == {"url": URL}


def test_error_exits_4_with_the_url_in_context() -> None:
    code, env = run(gui_app("error"), ["login"])
    assert code == 4
    error = env["error"]
    assert isinstance(error, dict)
    assert error["code"] == "GUI_UNAVAILABLE" and error["context"]["url"] == URL


def test_skip_and_error_need_no_open_url_field() -> None:
    app = App("gui", version="1.0.0")

    @app.command(
        "notify",
        description="x",
        danger_level="safe",
        exit_codes=(),
        gui_operations=["browser_open"],
        headless_behavior="skip",
    )
    def notify(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}


@pytest.mark.parametrize("behavior", ["emit_in_output", "skip", "error"])
def test_the_manifest_carries_the_declared_headless_behavior(behavior: str) -> None:
    manifest = gui_app(behavior).manifest()
    assert manifest["commands"]["login"]["headless_behavior"] == behavior  # type: ignore[index]
    spec_validator("manifest-response").validate(manifest)


def test_meta_headless_is_true_in_all_responses_when_headless_mode_is_active() -> None:
    for behavior in ("emit_in_output", "skip", "error"):
        _, env = run(gui_app(behavior), ["login"])
        assert env["meta"]["headless"] is True  # type: ignore[index]
    _, env = run(gui_app("skip"), ["login", "--no-such-flag"])
    assert env["meta"]["headless"] is True  # type: ignore[index]


@pytest.mark.skipif(WINDOWS, reason="the browser stand-in is a POSIX script")
def test_in_non_headless_mode_the_gui_operation_proceeds_normally(tmp_path: Path) -> None:
    opened = tmp_path / "opened.txt"
    browser = tmp_path / "browser.sh"
    browser.write_text(f'#!/bin/sh\nprintf "%s" "$1" > {opened}\n')
    browser.chmod(0o755)
    script = tmp_path / "gui.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import io, os, sys
            sys.path.insert(0, {str(Path(__file__).parent)!r})
            from test_more_declarations import gui_app

            class Terminal(io.StringIO):
                def isatty(self):
                    return True

            out = io.StringIO()
            code = gui_app("error").run(
                ["login", "--format", "json"],
                stdin=Terminal(),
                stdout=out,
                stderr=io.StringIO(),
                env={{"PATH": os.environ["PATH"], "DISPLAY": ":0"}},
                isatty=True,
            )
            print(out.getvalue())
            """
        )
    )
    env = {"PATH": os.environ["PATH"], "BROWSER": str(browser), "PYTHONPATH": str(SRC)}
    done = subprocess.run(
        [sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=30
    )
    envelope = json.loads(done.stdout)
    assert envelope["ok"] is True and envelope["data"]["status"] == "opened"
    assert "headless" not in envelope["meta"]
    assert opened.read_text() == URL


# REQ-C-019


@dataclass(frozen=True, slots=True)
class Echo:
    text: str = Arg(description="Text to echo")
    files: tuple[str, ...] = Flag(default=(), description="More words")


def echo_app(declared: bool) -> App:
    app = App("echoer", version="1.0.0")
    child = Subprocess("echo", user_controlled_args=("text",), hardcoded_args=("-n",))

    @app.command(
        "say",
        description="Echo text",
        danger_level="safe",
        exit_codes=(),
        subprocess=child if declared else None,
    )
    def say(args: Echo, ctx: Ctx) -> dict[str, str]:
        return {"stdout": ctx.run(["echo", "-n", args.text, *args.files]).stdout}

    return app


@pytest.mark.skipif(WINDOWS, reason="echo is a POSIX program")
def test_a_user_supplied_argument_to_the_subprocess_api_is_protected_by_req_f_044(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "pwned"
    code, env = run(echo_app(declared=False), ["say", f"; touch {marker}"])
    assert code == 0
    assert env["data"] == {"stdout": f"; touch {marker}"}
    assert not marker.exists()


def test_the_schema_output_includes_a_subprocess_section() -> None:
    derived = echo_app(declared=False).manifest()["commands"]["say"]  # type: ignore[index]
    assert derived["subprocess"] == {
        "binary": "echo",
        "user_controlled_args": ["text", "files"],
        "hardcoded_args": ["-n"],
    }
    out = io.StringIO()
    echo_app(declared=True).run(
        ["say", "--schema", "--format", "json"], stdout=out, stderr=io.StringIO(), env={}
    )
    schema = json.loads(out.getvalue())["data"]
    assert schema["subprocess"] == {
        "binary": "echo",
        "user_controlled_args": ["text"],
        "hardcoded_args": ["-n"],
    }
    spec_validator("manifest-response").validate(echo_app(declared=True).manifest())


@pytest.mark.parametrize("value", ["a; rm -rf /", "$(id)", "a|b", "`id`", "a&b", "-rf"])
def test_a_shell_metacharacter_in_a_user_derived_subprocess_argument_is_rejected_with_exit_2(
    value: str,
) -> None:
    code, env = run(echo_app(declared=True), ["say", f"--text={value}"])
    assert code == 2
    error = env["error"]
    assert isinstance(error, dict)
    assert error["code"] == "SHELL_METACHARACTER" and error["context"]["field"] == "text"


def test_derived_arguments_are_not_checked_for_metacharacters() -> None:
    """08-D1: only fields the author declares; an argument list needs no check"""
    code, env = run(echo_app(declared=False), ["say", "a|b"])
    assert code == 0 and env["data"] == {"stdout": "a|b"}


def test_subprocess_names_real_fields() -> None:
    app = App("echoer", version="1.0.0")
    with pytest.raises(RegistrationError, match="not fields"):

        @app.command(
            "say",
            description="x",
            danger_level="safe",
            exit_codes=(),
            subprocess=Subprocess("echo", user_controlled_args=("nope",)),
        )
        def say(args: Echo, ctx: Ctx) -> dict[str, str]:
            return {}


def test_audit_flags_a_child_whose_arguments_cannot_be_derived() -> None:
    app = App("echoer", version="1.0.0")

    @app.command("say", description="x", danger_level="safe", exit_codes=())
    def say(args: Echo, ctx: Ctx) -> dict[str, str]:
        argv = ["echo", args.text]
        return {"stdout": ctx.run(argv).stdout}

    rules = {r.id: r for r in audit(app, "echoer", limit=3).rules}
    [finding] = rules["subprocess-declared"].findings
    assert finding.command == "say" and "treaty.Subprocess" in finding.fix
    assert rules["subprocess-declared"].passed is False
    clean = {r.id: r for r in audit(echo_app(declared=False), "echoer", limit=3).rules}
    assert clean["subprocess-declared"].passed
