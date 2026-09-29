"""The new CLI chapter's tests/test_cli.py: todo's commands through the envelope

It replaces the scaffold's tests once cli.py holds todo; copy it into the project to run it.
"""

import io
from pathlib import Path

import pytest
from todo.cli import app

# The whole environment of each call: the audit log off, so a test run never lands in yours
QUIET = {"TODO_AUDIT_LOG": "off"}


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "todo.json")


def test_add_creates_an_item(db: str) -> None:
    env = app.call("add", {"text": "Buy milk", "priority": "high", "db": db}, env=QUIET)
    assert env.exit_code == 0
    assert env.data == {
        "effect": "created",
        "item": {"id": 1, "text": "Buy milk", "priority": "high", "done": False},
    }


def test_done_on_a_missing_item_is_not_found(db: str) -> None:
    env = app.call("done", {"id": 9, "db": db}, env=QUIET)
    assert env.exit_code == 5 and env.error is not None
    assert env.error.code == "NOT_FOUND"


def test_purge_previews_until_confirmed(db: str) -> None:
    app.call("add", {"text": "Walk dog", "db": db}, env=QUIET)
    app.call("done", {"id": 1, "db": db}, env=QUIET)
    preview = app.call("purge", {"db": db}, env=QUIET)
    assert preview.exit_code == 2 and preview.error is not None
    assert preview.error.code == "CONFIRMATION_REQUIRED"
    applied = app.call("purge", {"db": db, "confirm_destructive": True}, env=QUIET)
    assert applied.exit_code == 0
    assert app.call("list", {"all": True, "db": db}, env=QUIET).data == []


def test_list_prints_the_old_plain_lines(db: str) -> None:
    app.call("add", {"text": "Buy milk", "priority": "high", "db": db}, env=QUIET)
    out = io.StringIO()
    argv = ["list", "--db", db, "--format", "plain"]
    code = app.run(argv, stdout=out, stderr=io.StringIO(), env=QUIET, isatty=False)
    assert code == 0 and out.getvalue() == "[ ] #1 Buy milk (high)\n"
