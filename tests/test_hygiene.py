"""Regressions for the bad-practice notes of the pre-1.0 review: prompts and editors,
nested signal windows, and tasks an async handler leaves running when it fails."""

import asyncio
import io
import json
import os
import sys
from pathlib import Path
from typing import Self

import pytest

from treaty import App, Ctx, NoArgs
from treaty._errors import CliExit
from treaty._prompt import Prompter
from treaty._signals import Cancellation

BASE_ENV = {"PATH": os.environ["PATH"], "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}


def prompter(stdin: str, env: dict[str, str] | None = None) -> Prompter:
    return Prompter(
        command="go",
        declared=True,
        editor_alternatives=("body",),
        flags=frozenset({"name", "body"}),
        interactive=True,
        assume_yes=False,
        stdin=io.StringIO(stdin),
        stderr=io.StringIO(),
        env={**BASE_ENV, **(env or {})},
    )


def test_an_answer_from_a_windows_terminal_loses_its_carriage_return() -> None:
    assert prompter("ada\r\n").prompt("Name", flag="name") == "ada"


def test_a_prompt_must_name_a_flag_of_the_command() -> None:
    from treaty import RegistrationError

    with pytest.raises(RegistrationError, match="names no flag"):
        prompter("ada\n").prompt("Name", flag="nosuch")


@pytest.mark.skipif(sys.platform == "win32", reason="false and a missing program are POSIX")
@pytest.mark.parametrize(("editor", "why"), [("false", "exited with 1"), ("no-such-ed", "found")])
def test_an_editor_that_fails_abandons_the_edit_with_an_error(
    tmp_path: Path, editor: str, why: str
) -> None:
    with pytest.raises(CliExit, match=why) as caught:
        prompter("", {"EDITOR": editor}).edit("draft", lambda: tmp_path / "edit.txt")
    assert caught.value.code == "EDITOR_FAILED"
    assert not (tmp_path / "edit.txt").exists()


def test_signal_windows_refuse_to_nest() -> None:
    cancellation = Cancellation()
    with cancellation.armed(), pytest.raises(RuntimeError, match="do not nest"):
        with cancellation.armed():
            pass


# A failing async handler's own tasks stop before its resources are released

EVENTS: list[str] = []


class Pool:
    @classmethod
    async def acquire(cls, args: object, ctx: Ctx) -> Self:
        return cls()

    async def release(self) -> None:
        EVENTS.append("released")


async def background() -> None:
    try:
        await asyncio.sleep(60)
    finally:
        EVENTS.append("task stopped")


def test_a_failing_async_handlers_tasks_stop_before_its_resources_go() -> None:
    EVENTS.clear()
    app = App("aioctl", version="1.0.0")

    @app.command("go", description="Go", danger_level="safe", exit_codes=())
    async def go(args: NoArgs, ctx: Ctx, pool: Pool) -> dict[str, str]:
        asyncio.get_running_loop().create_task(background())
        await asyncio.sleep(0)  # the task starts
        raise ValueError("the handler failed")

    out = io.StringIO()
    code = app.run(["go", "--format", "json"], stdout=out, stderr=io.StringIO(), env=BASE_ENV)
    assert code == 1 and json.loads(out.getvalue())["error"]["code"] == "HANDLER_CRASHED"
    assert EVENTS == ["task stopped", "released"]
