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

from treaty import App, Ctx, NoArgs, RegistrationError

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
