"""Did-you-mean suggestions on UNKNOWN_COMMAND and an unroutable argv path"""

import io
import json

import pytest

from examples.authctl import app as authctl
from examples.deployctl import app as deployctl
from treaty import App
from treaty._suggest import closest, distance, hint


def run(app: App, argv: list[str], env: dict[str, str] | None = None) -> dict[str, object]:
    out = io.StringIO()
    app.run(
        [*argv, "--format", "json"],
        stdin=io.StringIO(),
        stdout=out,
        stderr=io.StringIO(),
        env={"PATH": "/usr/bin", **(env or {})},
    )
    error = json.loads(out.getvalue())["error"]
    assert isinstance(error, dict)
    return error


NAMES = [
    ("status", "app status"),
    ("start", "app deploy start"),
    ("status", "app job status"),
    ("rollback", "app deploy rollback"),
    ("deploy.rollback", "app deploy rollback"),
    ("deploy.start", "app deploy start"),
    ("generate-skills", "app generate-skills"),
]


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("rollbak", ["app deploy rollback"]),
        ("stauts", ["app status", "app job status"]),
        ("ROLLBACK", ["app deploy rollback"]),
        ("deploy.rolback", ["app deploy rollback"]),
        ("gen", ["app generate-skills"]),
        ("stat", ["app status", "app job status", "app deploy start"]),
        ("st", []),
        ("explode", []),
    ],
)
def test_closest(typed: str, expected: list[str]) -> None:
    assert closest(typed, NAMES) == expected


def test_distance_counts_a_swap_of_neighbors_as_one_edit() -> None:
    assert distance("status", "status") == 0
    assert distance("stauts", "status") == 1
    assert distance("rollbak", "rollback") == 1
    assert distance("explode", "deploy") > 2
    assert distance("", "abc") == 3


def test_hint_names_the_best_match_first() -> None:
    assert hint([]) is None
    assert hint(["app status"]) == "did you mean app status?"
    assert hint(["a", "b", "c"]) == "did you mean a (or b, c)?"


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["rollbak"], "deployctl deploy rollback"),
        (["deploy.rollback"], "deployctl deploy rollback"),
        (["delpoy"], "deployctl deploy"),
        (["deploy", "rolback"], "deployctl deploy rollback"),
        (["job", "stats"], "deployctl job status"),
    ],
)
def test_an_unroutable_path_suggests_what_to_type(argv: list[str], expected: str) -> None:
    error = run(deployctl, argv)
    assert error["code"] == "ARG_ERROR"
    context = error["context"]
    assert isinstance(context, dict)
    assert context["did_you_mean"] == [expected]
    assert error["suggestion"] == f"did you mean {expected}?"


def test_a_path_like_nothing_keeps_the_list_and_no_guess() -> None:
    error = run(deployctl, ["explode"])
    context = error["context"]
    assert isinstance(context, dict)
    assert "did_you_mean" not in context
    assert "deployctl deploy rollback" in context["available"]  # type: ignore[operator]


def test_app_call_and_exec_suggest_the_dot_path() -> None:
    envelope = deployctl.call("deploy.rolback", {})
    assert envelope.error is not None and envelope.error.code == "UNKNOWN_COMMAND"
    assert envelope.error.context["did_you_mean"] == ["deploy.rollback"]
    assert envelope.error.suggestion == "did you mean deploy.rollback?"
    envelope = deployctl.call("rollback", {})
    assert envelope.error is not None
    assert envelope.error.context["did_you_mean"] == ["deploy.rollback"]
    envelope = deployctl.call("exec", {})
    assert envelope.error is not None and "did_you_mean" not in envelope.error.context


def test_check_permissions_for_a_mistyped_command_names_the_closest() -> None:
    error = run(authctl, ["check-permissions", "--for", "repos lst"], {"AUTHCTL_TOKEN": "t"})
    assert error["code"] == "UNKNOWN_COMMAND"
    context = error["context"]
    assert isinstance(context, dict)
    assert context["did_you_mean"] == ["repos.list"]
    assert error["fix_required"] == "pass --for repos.list, the closest command"
