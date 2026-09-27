"""Session and process hygiene (workstream 09): REQ-F-029, F-030, F-032, F-041, F-043,
F-050, F-060, F-066, O-017, O-018, O-020."""

import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fixture_session_app import app as session_app

from treaty import App, Ctx, NoArgs
from treaty._audit import audit

SESSIONCTL = Path(__file__).resolve().parent / "fixture_session_app.py"
BASE_ENV = {"PATH": os.environ["PATH"], "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}


class TerminalInput(io.StringIO):
    """Stands in for a terminal on stdin"""

    def isatty(self) -> bool:
        return True


def run(
    app: App,
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    terminal: bool = False,
) -> tuple[int, dict[str, object], str]:
    """One in-process run in JSON; ``terminal`` puts a person at stdin and stdout"""
    out, err = io.StringIO(), io.StringIO()
    code = app.run(
        [*argv, "--format", "json"],
        stdin=TerminalInput() if terminal else io.StringIO(),
        stdout=out,
        stderr=err,
        env={**BASE_ENV, **(env or {})},
        isatty=terminal,
    )
    return code, json.loads(out.getvalue()), err.getvalue()


def tool(
    argv: list[str], env: dict[str, str] | None = None, *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """The fixture app as a real process, through App.main(), stdout a pipe"""
    return subprocess.run(
        [sys.executable, str(SESSIONCTL), *argv],
        env={**BASE_ENV, **(env or {})},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=cwd,
    )


def data_of(envelope: dict[str, object]) -> dict[str, object]:
    data = envelope["data"]
    assert isinstance(data, dict)
    return data


def meta_of(envelope: dict[str, object]) -> dict[str, object]:
    meta = envelope["meta"]
    assert isinstance(meta, dict)
    return meta


def child_env(app: App, names: tuple[str, ...], **kw: object) -> dict[str, str | None]:
    """The variables a ``ctx.run`` child of ``app`` sees"""
    code = f"import json, os; print(json.dumps({{n: os.environ.get(n) for n in {names!r}}}))"
    status, envelope, _ = run(app, ["child", "--code", code], **kw)  # type: ignore[arg-type]
    assert status == 0, envelope
    return json.loads(str(data_of(envelope)["stdout"]))


# F-050: update notifiers


NOTIFIERS = ("CI", "NO_UPDATE_NOTIFIER", "HOMEBREW_NO_AUTO_UPDATE", "PIP_DISABLE_PIP_VERSION_CHECK")


def test_ci_1_is_set_in_the_subprocess_environment_for_all_child_processes() -> None:
    seen = child_env(session_app, (*NOTIFIERS, "MYLIB_NO_UPDATE"))
    assert seen == {
        "CI": "1",
        "NO_UPDATE_NOTIFIER": "1",
        "HOMEBREW_NO_AUTO_UPDATE": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "MYLIB_NO_UPDATE": "1",  # the app's suppress_update_notifier hook
    }


def test_the_suppression_environment_variables_are_set_before_any_command_execution() -> None:
    proc = tool(["environ"])
    assert proc.returncode == 0, proc.stderr
    seen = data_of(json.loads(proc.stdout))
    assert seen["NO_UPDATE_NOTIFIER"] == "1" and seen["MYLIB_NO_UPDATE"] == "1"
    assert seen["CI"] == "1"  # for libraries in the process, not for treaty's own mode


NOTIFYING_CHILD = (
    "import os, sys\n"
    "if not (os.environ.get('NO_UPDATE_NOTIFIER') or os.environ.get('CI')):\n"
    "    print('Update available: 1.2.3 -> 1.3.0', file=sys.stderr)\n"
    "    print('Update available: 1.2.3 -> 1.3.0')\n"
)


def test_a_library_update_notifier_prints_nothing_on_stdout_or_stderr_off_a_terminal() -> None:
    proc = tool(["child", "--code", NOTIFYING_CHILD])
    assert proc.returncode == 0
    assert "Update available" not in proc.stdout + proc.stderr


def test_in_tty_mode_update_notifications_are_unaffected() -> None:
    seen = child_env(session_app, (*NOTIFIERS, "MYLIB_NO_UPDATE"), terminal=True)
    assert set(seen.values()) == {None}
    status, envelope, _ = run(session_app, ["child", "--code", NOTIFYING_CHILD], terminal=True)
    assert "Update available" in str(data_of(envelope)["stderr"])


def test_ci_on_a_terminal_still_silences_update_notices() -> None:
    seen = child_env(session_app, ("NO_UPDATE_NOTIFIER",), terminal=True, env={"CI": "true"})
    assert seen == {"NO_UPDATE_NOTIFIER": "1"}


def test_ci_set_for_libraries_does_not_turn_a_terminal_run_into_json() -> None:
    # main() sets CI=1 in os.environ off a terminal only; on one, nothing changes
    seen = child_env(session_app, ("CI",), terminal=True)
    assert seen == {"CI": None}


# F-066: child locale

GERMAN = {"LANG": "de_DE.UTF-8", "LC_ALL": "de_DE.UTF-8"}
FORMATTED = (
    "import locale\n"
    "try:\n"
    "    locale.setlocale(locale.LC_ALL, '')\n"
    "except locale.Error:\n"
    "    pass\n"
    "print(locale.format_string('%.2f', 1234.56, grouping=True))\n"
)


def test_a_subprocess_that_would_emit_a_german_number_emits_dot_decimals() -> None:
    status, envelope, _ = run(session_app, ["child", "--code", FORMATTED], env=GERMAN)
    assert status == 0 and data_of(envelope)["stdout"] == "1234.56\n"
    assert child_env(session_app, ("LC_ALL", "LC_NUMERIC"), env=GERMAN) == {
        "LC_ALL": "C",
        "LC_NUMERIC": "C",
    }


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_git_error_messages_are_in_english_regardless_of_the_system_locale(
    tmp_path: Path,
) -> None:
    app = App("gitctl", version="1.0.0")

    @app.command("status", description="git status", danger_level="safe", exit_codes=())
    def status(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"stderr": ctx.run(["git", "status"], cwd=tmp_path, check=False).stderr}

    _, envelope, _ = run(app, ["status"], env={**GERMAN, "LANGUAGE": "de"})
    assert "not a git repository" in str(data_of(envelope)["stderr"])


def test_a_command_with_preserve_locale_does_not_have_lc_all_c_injected() -> None:
    code = "import json, os; print(json.dumps([os.environ.get('LC_ALL'), os.environ.get('LANG')]))"
    status, envelope, _ = run(session_app, ["child-localized", "--code", code], env=GERMAN)
    assert status == 0
    assert json.loads(str(data_of(envelope)["stdout"])) == ["de_DE.UTF-8", "de_DE.UTF-8"]


def test_the_parent_process_locale_is_unaffected_only_subprocess_environments_are() -> None:
    proc = tool(["environ"], env={"LC_ALL": "en_US.UTF-8"})
    seen = data_of(json.loads(proc.stdout))
    assert seen["LC_ALL"] == "en_US.UTF-8" and seen["LC_NUMERIC"] is None


def test_the_preserve_locale_audit_rule_lists_opted_out_commands() -> None:
    report = audit(session_app, "sessionctl", limit=50)
    rule = next(r for r in report.rules if r.id == "preserve-locale")
    assert [f.command for f in rule.findings] == ["child-localized"]
    assert "preserve_locale" in rule.findings[0].fix


# F-029, O-020: update checks


class Checker:
    """An update check that records each call; ``delay`` stands in for the network"""

    def __init__(self, latest: str | None = "9.0.0", delay: float = 0.0) -> None:
        self.latest_version = latest
        self.delay = delay
        self.calls = 0
        self.done = threading.Event()

    def latest(self, current: str, timeout: float) -> str | None:
        self.calls += 1
        time.sleep(self.delay)
        self.done.set()
        return self.latest_version


def updating_app(checker: Checker, state: Path) -> App:
    app = App("updctl", version="1.0.0", state_dir=state, update_check=checker)

    @app.command("hello", description="Say hello", danger_level="safe", exit_codes=())
    def hello(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"hello": "world"}

    return app


def cached(state: Path, latest: str, age: float = 0.0) -> None:
    state.mkdir(parents=True, exist_ok=True)
    body = {"checked_at": time.time() - age, "latest": latest}
    (state / "update.json").write_text(json.dumps(body))


def test_meta_update_available_names_a_newer_cached_release_for_a_person(tmp_path: Path) -> None:
    checker = Checker()
    cached(tmp_path, "2.1.0")
    _, envelope, _ = run(updating_app(checker, tmp_path), ["hello"], terminal=True)
    assert meta_of(envelope)["update_available"] == "2.1.0"
    assert checker.calls == 0  # the cache is fresh


def test_an_older_or_equal_cached_release_is_not_an_update(tmp_path: Path) -> None:
    for latest in ("1.0.0", "0.9.0", "1.0.0-rc.1"):
        cached(tmp_path, latest)
        _, envelope, _ = run(updating_app(Checker(), tmp_path), ["hello"], terminal=True)
        assert "update_available" not in meta_of(envelope), latest


def test_a_stale_cache_is_refreshed_in_the_background_for_the_next_run(tmp_path: Path) -> None:
    checker = Checker("3.0.0")
    cached(tmp_path, "2.0.0", age=2 * 86_400)
    app = updating_app(checker, tmp_path)
    _, envelope, _ = run(app, ["hello"], terminal=True)
    assert meta_of(envelope)["update_available"] == "2.0.0"
    assert checker.done.wait(5)
    deadline = time.monotonic() + 5
    while json.loads((tmp_path / "update.json").read_text())["latest"] != "3.0.0":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    _, envelope, _ = run(app, ["hello"], terminal=True)
    assert meta_of(envelope)["update_available"] == "3.0.0"


def test_no_update_check_occurs_when_ci_true(tmp_path: Path) -> None:
    checker = Checker()
    _, envelope, _ = run(
        updating_app(checker, tmp_path), ["hello"], terminal=True, env={"CI": "true"}
    )
    assert checker.calls == 0 and not (tmp_path / "update.json").exists()
    assert "update_available" not in meta_of(envelope)


def test_no_update_check_occurs_when_stdout_is_not_a_tty(tmp_path: Path) -> None:
    checker = Checker()
    cached(tmp_path, "2.0.0", age=2 * 86_400)
    _, envelope, _ = run(updating_app(checker, tmp_path), ["hello"])
    time.sleep(0.05)
    assert checker.calls == 0 and "update_available" not in meta_of(envelope)


def test_tool_no_update_1_disables_all_update_behavior_including_background_checks(
    tmp_path: Path,
) -> None:
    checker = Checker()
    cached(tmp_path, "2.0.0", age=2 * 86_400)
    app = updating_app(checker, tmp_path)
    _, envelope, _ = run(app, ["hello"], terminal=True, env={"UPDCTL_NO_UPDATE": "1"})
    time.sleep(0.05)
    assert checker.calls == 0 and "update_available" not in meta_of(envelope)


def test_no_update_check_and_tool_no_update_1_are_equivalent_in_effect(tmp_path: Path) -> None:
    checker = Checker()
    cached(tmp_path, "2.0.0", age=2 * 86_400)
    app = updating_app(checker, tmp_path)
    by_flag = run(app, ["hello", "--no-update-check"], terminal=True)
    by_env = run(app, ["hello"], terminal=True, env={"UPDCTL_NO_UPDATE": "1"})
    time.sleep(0.05)
    assert checker.calls == 0
    for _, envelope, _ in (by_flag, by_env):
        assert "update_available" not in meta_of(envelope)


def test_meta_update_available_is_absent_when_no_update_check_is_passed(tmp_path: Path) -> None:
    cached(tmp_path, "2.0.0")
    app = updating_app(Checker(), tmp_path)
    _, envelope, _ = run(app, ["--no-update-check", "hello"], terminal=True)
    assert "update_available" not in meta_of(envelope)


def test_no_update_check_prevents_any_network_call_for_update_checking(tmp_path: Path) -> None:
    checker = Checker()
    run(updating_app(checker, tmp_path), ["hello", "--no-update-check"], terminal=True)
    time.sleep(0.05)
    assert checker.calls == 0 and not (tmp_path / "update.json").exists()


def test_no_measurable_latency_is_added_to_any_command_by_update_checking(tmp_path: Path) -> None:
    checker = Checker(delay=3.0)
    started = time.monotonic()
    run(updating_app(checker, tmp_path), ["hello"], terminal=True)
    assert time.monotonic() - started < 1.0  # the check runs on a daemon thread


def test_the_no_update_check_flag_is_present_in_every_commands_help_output() -> None:
    for path in ("child", "environ", "manifest", "doctor", "cleanup", "version"):
        out = io.StringIO()
        session_app.run([path, "--help"], stdout=out, stderr=io.StringIO(), env={}, isatty=True)
        assert "--no-update-check" in out.getvalue(), path


def test_a_plain_run_tells_the_person_about_an_update_on_stderr(tmp_path: Path) -> None:
    cached(tmp_path, "2.0.0")
    out, err = io.StringIO(), io.StringIO()
    app = updating_app(Checker(), tmp_path)
    app.run(["hello"], stdin=TerminalInput(), stdout=out, stderr=err, env=BASE_ENV, isatty=True)
    assert err.getvalue() == "updctl 2.0.0 is available; this is 1.0.0\n"
    assert "2.0.0" not in out.getvalue()


# O-017, F-041: the working directory


def test_cwd_causes_all_relative_path_resolution_to_be_based_on_it(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hello")
    status, envelope, _ = run(session_app, ["read", "notes.txt", "--cwd", str(tmp_path)])
    assert status == 0, envelope
    data = data_of(envelope)
    assert data["text"] == "hello"
    assert data["file"] == str(tmp_path / "notes.txt") and data["cwd"] == str(tmp_path)


def test_meta_cwd_reflects_the_value_of_cwd_when_passed(tmp_path: Path) -> None:
    _, envelope, _ = run(session_app, ["--cwd", str(tmp_path), "environ"])
    assert meta_of(envelope)["cwd"] == str(tmp_path)
    (tmp_path / "sub").mkdir()
    proc = tool(["environ", "--cwd", "sub"], cwd=tmp_path)
    assert Path(meta_of(json.loads(proc.stdout))["cwd"]).samefile(tmp_path / "sub")


def test_a_nonexistent_cwd_path_causes_exit_2_before_any_side_effects(tmp_path: Path) -> None:
    for where in (tmp_path / "missing", tmp_path / "file.txt"):
        (tmp_path / "file.txt").write_text("")
        status, envelope, _ = run(session_app, ["chdir", str(tmp_path), "--cwd", str(where)])
        assert status == 2
        error = envelope["error"]
        assert isinstance(error, dict)
        assert error["code"] == "ARG_ERROR" and error["phase"] == "validation"
        assert error["context"]["flag"] == "cwd"
    assert Path.cwd() != tmp_path


def test_cwd_does_not_change_the_processs_actual_working_directory(tmp_path: Path) -> None:
    before = os.getcwd()
    status, envelope, _ = run(session_app, ["pwd", "--cwd", str(tmp_path)])
    assert status == 0 and os.getcwd() == before
    data = data_of(envelope)
    assert data["process"] == before
    assert Path(str(data["child"])).samefile(tmp_path)  # children start under --cwd


def test_the_process_cwd_after_any_command_invocation_is_identical_to_before(
    tmp_path: Path,
) -> None:
    before = os.getcwd()
    status, envelope, _ = run(session_app, ["chdir", str(tmp_path)])
    assert status == 0 and os.getcwd() == before
    warning = next(w for w in envelope["warnings"] if w["code"] == "CWD_CHANGED")  # type: ignore[union-attr]
    assert warning["context"] == {"from": before, "to": str(data_of(envelope)["now"])}


def test_a_stream_that_changes_directory_is_changed_back(tmp_path: Path) -> None:
    app = App("streamctl", version="1.0.0")

    @app.command("tail", description="Stream", danger_level="safe", exit_codes=(), streaming=True)
    def tail(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, int]]:
        os.chdir(tmp_path)
        yield {"n": 1}

    before = os.getcwd()
    out = io.StringIO()
    app.run(["tail"], stdout=out, stderr=io.StringIO(), env=BASE_ENV)
    last = json.loads(out.getvalue().splitlines()[-1])
    assert os.getcwd() == before
    assert [w["code"] for w in last["warnings"]] == ["CWD_CHANGED"]


def test_a_command_that_calls_os_chdir_is_flagged_by_the_linter() -> None:
    report = audit(session_app, "sessionctl", limit=50)
    rule = next(r for r in report.rules if r.id == "no-chdir")
    assert [f.command for f in rule.findings] == ["chdir"]
    assert "ctx.cwd" in rule.findings[0].fix


def test_operations_in_other_directories_work_with_absolute_paths_from_cwd(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.txt").write_text("from a")
    proc = tool(["read", "a.txt", "--cwd", str(tmp_path)])
    envelope = json.loads(proc.stdout)
    assert proc.returncode == 0 and data_of(envelope)["text"] == "from a"
