"""Output hygiene: REQ-F-005, F-006, F-007, F-008, F-010, F-016, F-051, C-013."""

import io
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from conftest import WINDOWS, spec_validator
from fixture_hygiene_app import app

from treaty import App, Ctx, Exit, NoArgs, ParseError

HYGIENECTL = Path(__file__).resolve().parent / "fixture_hygiene_app.py"
needs_sh = pytest.mark.skipif(WINDOWS, reason="the child is a /bin/sh command")


def run(
    argv: list[str], *, env: dict[str, str] | None = None, isatty: bool = False
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = app.run(argv, stdout=out, stderr=err, env=env or {}, isatty=isatty)
    return code, out.getvalue(), err.getvalue()


def envelope(argv: list[str], **kw: object) -> tuple[int, dict[str, object], str]:
    code, out, err = run(argv, **kw)  # type: ignore[arg-type]
    return code, json.loads(out), err


def tool(
    argv: list[str],
    env: dict[str, str],
    *,
    stdout: int = subprocess.PIPE,
    stderr: int = subprocess.PIPE,
) -> subprocess.CompletedProcess[str]:
    """The fixture app as a real process, through App.main(), with pipes for stdout"""
    base = {"PATH": os.environ["PATH"], "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    return subprocess.run(
        [sys.executable, str(HYGIENECTL), *argv],
        env={**base, **env},
        stdout=stdout,
        stderr=stderr,
        text=True,
        timeout=30,
        check=False,
    )


def data_of(env: dict[str, object]) -> dict[str, object]:
    data = env["data"]
    assert isinstance(data, dict)
    return data


def error_of(env: dict[str, object]) -> dict[str, object]:
    error = env["error"]
    assert isinstance(error, dict)
    return error


# REQ-F-006 / REQ-F-060: stdout carries only the envelope


def test_print_in_a_handler_goes_to_stderr_with_a_warning() -> None:
    code, out, err = run(["chatty"])
    env = json.loads(out)  # exactly one JSON document: nothing printed ahead of it
    spec_validator("response-envelope").validate(env)
    assert code == 0 and env["data"] == {"status": "ok"}
    assert "initialized\n" in err
    assert env["warnings"] == [
        {
            "code": "THIRD_PARTY_STDOUT",
            "message": "Third-party code wrote to stdout; the text went to stderr",
            "context": {"bytes": len("initialized\n")},
        }
    ]


def test_sys_stdout_is_restored_after_the_run() -> None:
    before = sys.stdout
    run(["chatty"])
    assert sys.stdout is before


def test_stderr_discarded_still_yields_the_envelope() -> None:
    """2>/dev/null"""
    proc = tool(["chatty"], {}, stderr=subprocess.DEVNULL)
    assert proc.returncode == 0 and json.loads(proc.stdout)["data"] == {"status": "ok"}


def test_stdout_discarded_loses_no_log_line() -> None:
    """1>/dev/null"""
    proc = tool(["chatty"], {}, stdout=subprocess.DEVNULL)
    lines = proc.stderr.splitlines()
    assert proc.returncode == 0 and "initialized" in lines
    logged = json.loads(lines[-1])
    assert logged == {
        "level": "info",
        "message": "connecting",
        "fields": {"host": "db.example.com"},
    }


def test_log_is_plain_text_in_plain_mode() -> None:
    code, _, err = run(["chatty", "--format", "plain"])
    assert code == 0 and "connecting host=db.example.com\n" in err


# REQ-F-051: ctx.log redacts secrets


def test_log_redacts_declared_secrets_and_credential_names() -> None:
    code, env, err = envelope(
        ["login", "--api-token-from-env", "TOKEN"], env={"TOKEN": "sk-live-123456"}
    )
    assert code == 0 and data_of(env) == {"logged_in": True}
    assert "sk-live-123456" not in err and "abc" not in err
    assert "wJalr" not in err and "hunter2" not in err and "sid=1" not in err
    logged = json.loads(err.strip())
    assert logged["message"] == "sending [REDACTED]"
    assert logged["fields"] == {
        "token": "[REDACTED]",
        "env": {
            "AWS_SECRET_ACCESS_KEY": "[REDACTED]",
            "DB_PASS": "[REDACTED]",
            "HOME": "/home/me",
        },
        "headers": {"Authorization": "[REDACTED]", "Cookie": "[REDACTED]", "Accept": "*/*"},
    }


# REQ-F-007 / REQ-F-016: strings in JSON are plain, valid UTF-8 text


def test_escape_sequences_are_stripped_in_json() -> None:
    code, out, _ = run(["colored"])
    assert code == 0 and "\\u001b" not in out and "\x1b" not in out
    data = data_of(json.loads(out))
    # A carriage return is data, not a terminal escape
    assert data["text"] == "red" and data["progress"] == "50%\r100%"


def test_null_bytes_and_lone_surrogates_become_replacement_characters() -> None:
    _, out, _ = run(["colored"])
    assert data_of(json.loads(out))["raw"] == "a�b�"
    out.encode("utf-8")  # strict: no surrogate is left to fail here


def test_plain_mode_keeps_the_text_as_returned() -> None:
    code, out, _ = run(["colored", "--format", "plain"])
    assert code == 0 and "text: \x1b[31mred\x1b[0m\n" in out


def test_escape_sequences_are_stripped_from_error_messages() -> None:
    code, env, _ = envelope(["busy"], env={"DOWN": "1"})
    assert code == 80 and error_of(env)["message"] == "Upstream is down."


# REQ-F-008: color detection


@pytest.mark.parametrize(
    "env",
    [
        {"NO_COLOR": ""},
        {"TERM": "dumb"},
        {"CI": "1"},
        {"GITHUB_ACTIONS": "true"},
        {"JENKINS_URL": "x"},
    ],
)
def test_any_no_color_signal_turns_color_off(env: dict[str, str]) -> None:
    code, out, _ = run(["color", "--format", "json"], env=env, isatty=True)
    assert code == 0 and data_of(json.loads(out)) == {"color": False}
    code, out, _ = run(["color", "--format", "plain"], env=env, isatty=True)
    assert code == 0 and out == "color: false\n"


def test_color_needs_a_terminal_and_a_text_format() -> None:
    assert run(["color"], isatty=True)[1] == "color: true\n"
    assert data_of(json.loads(run(["color", "--format", "json"], isatty=True)[1])) == {
        "color": False
    }
    assert run(["color", "--format", "plain"], isatty=False)[1] == "color: false\n"


# REQ-F-008 / REQ-F-010: what child processes inherit


@needs_sh
def test_a_child_process_sees_no_pager_and_no_color() -> None:
    proc = tool(["children"], {"PAGER": "less", "GIT_PAGER": "less"})
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["data"] == {"child": "cat cat 1"}


@needs_sh
def test_an_empty_no_color_reaches_children_as_1() -> None:
    proc = tool(["children"], {"NO_COLOR": ""})
    assert json.loads(proc.stdout)["data"] == {"child": "cat cat 1"}


def test_run_leaves_the_process_environment_alone() -> None:
    before = dict(os.environ)
    run(["color"])
    assert dict(os.environ) == before


# REQ-F-005: locale-invariant serialization


def test_dates_numbers_and_booleans_are_invariant() -> None:
    code, out, _ = run(["report"])
    assert code == 0
    assert '"created_at":"2026-09-27T10:00:00Z"' in out
    assert '"day":"2026-09-27"' in out and '"at":"10:00:00"' in out
    assert '"price":1234.56' in out and '"count":1000000' in out
    assert '"ratio":"0.075"' in out and '"active":true' in out
    assert re.search(r'"created_at":"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', out)


def test_a_zone_offset_is_kept() -> None:
    _, out, _ = run(["offset"])
    assert data_of(json.loads(out)) == {"at": "2026-09-27T12:00:00+02:00"}


def test_a_naive_datetime_is_invalid_output() -> None:
    code, env, _ = envelope(["naive"])
    error = error_of(env)
    assert code == 1 and error["code"] == "INVALID_OUTPUT"
    assert "tzinfo" in str(error["message"])


def test_output_schema_names_the_string_formats() -> None:
    properties = app.commands[next(p for p in app.commands if p.value == "report")].output_schema[
        "properties"
    ]
    assert properties["created_at"] == {"type": "string", "format": "date-time"}
    assert properties["day"] == {"type": "string", "format": "date"}
    assert properties["at"] == {"type": "string", "format": "time"}
    assert properties["ratio"]["type"] == "string" and "pattern" in properties["ratio"]


def test_output_is_byte_identical_across_locales() -> None:
    def stripped(locale: str) -> dict[str, object]:
        proc = tool(["report"], {"LC_ALL": locale, "LANG": locale})
        assert proc.returncode == 0, proc.stderr
        env = json.loads(proc.stdout)
        meta = env["meta"]
        del meta["request_id"], meta["duration_ms"]
        return env

    german, c = stripped("de_DE.UTF-8"), stripped("C")
    assert json.dumps(german, sort_keys=True) == json.dumps(c, sort_keys=True)


# REQ-C-013: code, message as a sentence, suggestion when recoverable

_SENTENCE = re.compile(r"^[^a-z].*[.!?]$", re.DOTALL)
_CODE = re.compile(r"^[A-Z][A-Z0-9_]+$")


def sentence_app() -> App:
    demo = App("democtl", version="1.0.0", default_timeout=0.2)

    @demo.command("crash", description="Raise a bug", danger_level="safe", exit_codes=())
    def crash(args: NoArgs, ctx: Ctx) -> None:
        raise ValueError("boom")

    @demo.command("slow", description="Outlive the deadline", danger_level="safe", exit_codes=())
    def slow(args: NoArgs, ctx: Ctx) -> None:
        import time

        time.sleep(1)

    @demo.command("reject", description="Reject its own input", danger_level="safe", exit_codes=())
    def reject(args: NoArgs, ctx: Ctx) -> None:
        raise ParseError("region xx is unknown")

    @demo.command(
        "missing", description="Fail with a lowercase message", danger_level="safe", exit_codes=()
    )
    def missing(args: NoArgs, ctx: Ctx) -> None:
        raise Exit.NOT_FOUND("no such release", context={"tag": "9.9"})

    return demo


@pytest.mark.parametrize(
    "argv",
    [
        ["crash"],
        ["slow"],
        ["reject"],
        ["missing"],
        ["nope"],
        ["crash", "--bogus", "1"],
        ["--format", "xml", "crash"],
        ["--format", "csv", "crash"],
        ["--bogus", "crash"],
    ],
)
def test_every_framework_error_message_is_a_sentence(argv: list[str]) -> None:
    out = io.StringIO()
    code = sentence_app().run(argv, stdout=out, stderr=io.StringIO(), env={}, isatty=False)
    env = json.loads(out.getvalue())
    error = env["error"]
    assert code != 0 and isinstance(error, dict)
    assert _CODE.match(error["code"])
    assert _SENTENCE.match(error["message"]), error["message"]
    for item in error.get("errors", []):
        assert _SENTENCE.match(item["message"]), item["message"]
    if error["retryable"] or "fix_required" in error:
        assert error["suggestion"]
    assert "Traceback" not in out.getvalue()


def test_a_declared_suggestion_fills_the_error() -> None:
    code, env, _ = envelope(["busy"])
    error = error_of(env)
    assert code == 79 and error["retryable"] is True
    assert error["message"] == "Upstream is busy."
    assert error["suggestion"] == "wait a minute, then retry"


def test_a_retryable_error_without_a_declared_suggestion_gets_a_retry_step() -> None:
    _, env, _ = envelope(["busy"], env={"DOWN": "1"})
    assert error_of(env)["suggestion"] == "retry the same command; it had no side effects"


def test_keys_are_never_rewritten_so_none_collide() -> None:
    app = App("keys", version="1")

    @app.command("x", description="x", danger_level="safe", exit_codes=())
    def x(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        return {"\x1b[1mk\x1b[0m": 1, "k": 2}

    out = io.StringIO()
    assert app.run(["x", "--format", "json"], stdout=out, stderr=io.StringIO(), env={}) == 0
    assert json.loads(out.getvalue())["data"] == {"\x1b[1mk\x1b[0m": 1, "k": 2}


# The stdout swap across runs


def quiet_run(app: App, argv: list[str]) -> int:
    return app.run([*argv, "--format", "json"], stdout=io.StringIO(), stderr=io.StringIO(), env={})


def test_overlapping_runs_on_threads_restore_the_original_streams() -> None:
    first_in, second_in = threading.Event(), threading.Event()
    release_first, release_second = threading.Event(), threading.Event()
    app = App("overlap", version="1")

    @app.command("a", description="a", danger_level="safe", exit_codes=())
    def a(args: NoArgs, ctx: Ctx) -> dict[str, bool]:
        first_in.set()
        return {"released": release_first.wait(10)}

    @app.command("b", description="b", danger_level="safe", exit_codes=())
    def b(args: NoArgs, ctx: Ctx) -> dict[str, bool]:
        second_in.set()
        return {"released": release_second.wait(10)}

    before = (sys.stdout, sys.stdin)
    first = threading.Thread(target=quiet_run, args=(app, ["a"]))
    second = threading.Thread(target=quiet_run, args=(app, ["b"]))
    first.start()
    assert first_in.wait(10)
    second.start()
    assert second_in.wait(10)
    release_first.set()  # the first run ends while the second still holds the swap
    first.join(10)
    release_second.set()
    second.join(10)
    assert (sys.stdout, sys.stdin) == before


def test_a_run_nested_in_a_handler_gives_back_the_outer_swap() -> None:
    app = App("nested", version="1")

    @app.command("inner", description="inner", danger_level="safe", exit_codes=())
    def inner(args: NoArgs, ctx: Ctx) -> dict[str, bool]:
        return {}

    @app.command("outer", description="outer", danger_level="safe", exit_codes=())
    def outer(args: NoArgs, ctx: Ctx) -> dict[str, bool]:
        swapped = sys.stdout
        quiet_run(app, ["inner"])
        return {"kept": sys.stdout is swapped}

    before = sys.stdout
    out = io.StringIO()
    app.run(["outer", "--format", "json"], stdout=out, stderr=io.StringIO(), env={})
    assert json.loads(out.getvalue())["data"] == {"kept": True} and sys.stdout is before
