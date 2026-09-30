"""Session and process hygiene (workstream 09): REQ-F-029, F-030, F-032, F-041, F-043,
F-050, F-060, F-066, O-017, O-018, O-020."""

import io
import json
import locale
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import WINDOWS, needs_posix_signals
from fixture_session_app import app as session_app

from treaty import App, CachePolicy, Ctx, NoArgs
from treaty._app import _StrayStdout
from treaty._atomic import retry_sharing_violation
from treaty._audit import audit
from treaty._mode import child_ctype, locale_available, normalize_locale

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
    assert status == 0
    if not WINDOWS:  # Windows children take the locale from the user profile, not LC_ALL
        assert data_of(envelope)["stdout"] == "1234.56\n"
    if WINDOWS:  # kept as before: native children ignore the variables
        assert child_env(session_app, ("LC_ALL", "LC_NUMERIC"), env=GERMAN) == {
            "LC_ALL": "C",
            "LC_NUMERIC": "C",
        }


LOCALE_NAMES = ("LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "LC_NUMERIC", "LC_TIME")
SH_LOCALE = " ".join(f'{name}="${{{name}-unset}}"' for name in LOCALE_NAMES)


@pytest.mark.skipif(WINDOWS, reason="Windows children get LC_ALL=C, as before")
def test_a_child_gets_the_c_locale_with_utf8_text_and_none_of_the_users_overrides() -> None:
    app = App("shctl", version="1.0.0")

    @app.command("locale", description="Echo the locale", danger_level="safe", exit_codes=())
    def show(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"stdout": ctx.run(["sh", "-c", f"echo {SH_LOCALE}"]).stdout}

    user = {**GERMAN, "LC_CTYPE": "de_DE.UTF-8", "LC_TIME": "de_DE.UTF-8", "LC_MESSAGES": "de"}
    status, envelope, _ = run(app, ["locale"], env=user)
    assert status == 0, envelope
    seen = dict(pair.split("=", 1) for pair in str(data_of(envelope)["stdout"]).split())
    assert seen == {
        "LANG": "C",
        "LC_ALL": "unset",
        "LC_CTYPE": child_ctype(),
        "LC_MESSAGES": "C",
        "LC_NUMERIC": "C",
        "LC_TIME": "unset",
    }


@pytest.mark.skipif(WINDOWS, reason="Windows children get LC_ALL=C, as before")
def test_a_tool_that_requires_a_utf8_locale_starts_under_the_c_locale() -> None:
    # Ansible's check: under LC_ALL=C the interpreter keeps the ASCII C locale
    ansible = "import locale; locale.setlocale(locale.LC_ALL, ''); print(locale.getlocale()[1])"
    status, envelope, _ = run(session_app, ["child", "--code", ansible], env=GERMAN)
    assert status == 0
    assert data_of(envelope)["stdout"] == "UTF-8\n"


def test_c_utf8_is_used_where_the_platform_has_it_and_c_where_it_does_not() -> None:
    env = {"LANG": "de_DE.UTF-8", "LC_ALL": "de_DE.UTF-8", "LC_TERMINAL": "iTerm2"}
    missing = dict(env)
    normalize_locale(missing, ctype=child_ctype(lambda name: False))
    present = dict(env)
    normalize_locale(present, ctype=child_ctype(lambda name: name == "C.UTF-8"))
    if WINDOWS:
        assert missing == present == {**env, "LC_ALL": "C", "LC_NUMERIC": "C"}
        return
    c = {"LANG": "C", "LC_MESSAGES": "C", "LC_NUMERIC": "C", "LC_TERMINAL": "iTerm2"}
    assert missing == {**c, "LC_CTYPE": "C"}
    assert present == {**c, "LC_CTYPE": "C.UTF-8"}


@pytest.mark.skipif(WINDOWS, reason="Windows has no newlocale; children get LC_ALL=C")
def test_probing_for_c_utf8_leaves_the_process_locale_alone() -> None:
    before = locale.setlocale(locale.LC_ALL)
    assert locale_available("C")
    assert not locale_available("xx_YY.no-such-locale")
    if sys.platform == "darwin":
        assert locale_available("C.UTF-8")
    assert locale.setlocale(locale.LC_ALL) == before


@pytest.mark.skipif(WINDOWS, reason="Windows has no newlocale; children get LC_ALL=C")
def test_a_c_library_that_cannot_be_opened_falls_back_to_c(tmp_path: Path) -> None:
    # A static Python's dlopen fails: every command probes, so this must not raise
    assert not locale_available("C.UTF-8", str(tmp_path / "no-such-libc.so"))


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
    names = ("LC_ALL", "LANG", "LC_CTYPE", "LC_MESSAGES")
    code = f"import json, os; print(json.dumps([os.environ.get(n) for n in {names!r}]))"
    env = {**GERMAN, "LC_MESSAGES": "de_DE.UTF-8"}
    status, envelope, _ = run(session_app, ["child-localized", "--code", code], env=env)
    assert status == 0
    seen = json.loads(str(data_of(envelope)["stdout"]))
    assert seen == ["de_DE.UTF-8", "de_DE.UTF-8", None, "de_DE.UTF-8"]


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


def cached_latest(state: Path) -> object:
    """The cache's latest version; on Windows a read while the update check replaces the
    file is retried, as treaty's own read is"""
    text = retry_sharing_violation(lambda: (state / "update.json").read_text())
    return json.loads(text)["latest"]


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


def test_a_pep_440_release_from_the_checker_is_cached_in_its_semver_spelling(
    tmp_path: Path,
) -> None:
    """A checker reading PyPI gets 2.0.0rc1; meta.update_available is semver like tool_version"""
    checker = Checker("2.0.0rc1")
    app = updating_app(checker, tmp_path)
    run(app, ["hello"], terminal=True)
    assert checker.done.wait(5)
    deadline = time.monotonic() + 5
    while not (tmp_path / "update.json").exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    while cached_latest(tmp_path) != "2.0.0-rc.1":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    _, envelope, _ = run(app, ["hello"], terminal=True)
    assert meta_of(envelope)["update_available"] == "2.0.0-rc.1"


def test_a_stale_cache_is_refreshed_in_the_background_for_the_next_run(tmp_path: Path) -> None:
    checker = Checker("3.0.0")
    cached(tmp_path, "2.0.0", age=2 * 86_400)
    app = updating_app(checker, tmp_path)
    _, envelope, _ = run(app, ["hello"], terminal=True)
    assert meta_of(envelope)["update_available"] == "2.0.0"
    assert checker.done.wait(5)
    deadline = time.monotonic() + 5
    while cached_latest(tmp_path) != "3.0.0":
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


# F-032, F-043, F-030: the session temp directory


def scratch(tmp_path: Path, *argv: str) -> dict[str, object]:
    status, envelope, _ = run(session_app, ["scratch", *argv], env={"TMPDIR": str(tmp_path)})
    assert status == 0, envelope
    return envelope


def test_the_temp_directory_path_is_exposed_as_a_framework_api_and_an_env_var(
    tmp_path: Path,
) -> None:
    envelope = scratch(tmp_path)
    data = data_of(envelope)
    assert data["tmp_dir"] == meta_of(envelope)["session_tmp_dir"]
    assert data["child_tmpdir"] == data["child_gettempdir"] == data["tmp_dir"]
    assert str(data["temp_file"]).startswith(str(data["tmp_dir"]))
    request_id = meta_of(envelope)["request_id"]
    assert Path(str(data["tmp_dir"])).name == request_id  # the run identifier


def test_two_parallel_invocations_never_write_to_the_same_temp_file_path(tmp_path: Path) -> None:
    env = {"TMPDIR": str(tmp_path)}
    procs = [
        subprocess.Popen(
            [sys.executable, str(SESSIONCTL), "scratch"],
            env={**BASE_ENV, **env},
            stdout=subprocess.PIPE,
            text=True,
        )
        for _ in range(3)
    ]
    outputs = [json.loads(p.communicate(timeout=30)[0]) for p in procs]
    dirs = {data_of(o)["tmp_dir"] for o in outputs}
    files = {data_of(o)["temp_file"] for o in outputs}
    assert len(dirs) == len(files) == 3


def test_after_a_command_exits_normally_its_session_temp_directory_is_removed(
    tmp_path: Path,
) -> None:
    envelope = scratch(tmp_path)
    assert not Path(str(meta_of(envelope)["session_tmp_dir"])).exists()
    proc = tool(["scratch"], env={"TMPDIR": str(tmp_path)})
    assert not Path(str(meta_of(json.loads(proc.stdout))["session_tmp_dir"])).exists()


def test_a_run_that_uses_no_temp_directory_makes_none(tmp_path: Path) -> None:
    _, envelope, _ = run(session_app, ["environ"], env={"TMPDIR": str(tmp_path)})
    assert "session_tmp_dir" not in meta_of(envelope)
    assert list(tmp_path.iterdir()) == []


@needs_posix_signals
def test_after_a_command_exits_via_signal_its_session_temp_directory_is_removed(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "child.pid"
    proc = subprocess.Popen(
        [sys.executable, str(SESSIONCTL), "hold", "--pid-file", str(marker)],
        env={**BASE_ENV, "TMPDIR": str(tmp_path)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while not marker.exists() or not marker.read_text():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    child = int(marker.read_text())
    session = next(p for p in (tmp_path / f"sessionctl-{os.getuid()}").iterdir())
    assert (session / "children.pids").read_text().split() == [str(child)]
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 143
    assert meta_of(json.loads(out))["session_tmp_dir"] == str(session)
    assert not session.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)  # SIGTERM reached the tracked child: no orphan


def test_the_session_tracking_file_exists_and_is_readable_while_children_are_running(
    tmp_path: Path,
) -> None:
    status, envelope, _ = run(session_app, ["kids"], env={"TMPDIR": str(tmp_path)})
    assert status == 0, envelope
    listed, own = str(data_of(envelope)["stdout"]).splitlines()[-2:]
    assert listed.split() == [own]


def test_no_orphaned_child_remains_and_meta_session_pid_file_is_absent_once_they_exited(
    tmp_path: Path,
) -> None:
    status, envelope, _ = run(session_app, ["kids"], env={"TMPDIR": str(tmp_path)})
    pid = int(str(data_of(envelope)["stdout"]).split()[-1])
    assert "session_pid_file" not in meta_of(envelope)
    if not WINDOWS:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_the_session_temp_directory_is_namespaced_by_instance_id(tmp_path: Path) -> None:
    envelope = scratch(tmp_path, "--instance-id", "agent-7")
    where = Path(str(meta_of(envelope)["session_tmp_dir"]))
    assert where.parent.parent.name == "instances" and where.parent.name == "agent-7"


def report(tmp_path: Path, *argv: str) -> dict[str, object]:
    status, envelope, _ = run(session_app, ["report", *argv], env={"TMPDIR": str(tmp_path)})
    assert status == 0, envelope
    return data_of(envelope)


def test_a_response_that_includes_a_caller_facing_output_file_includes_a_cleanup_object(
    tmp_path: Path,
) -> None:
    data = report(tmp_path)
    assert Path(str(data["output_file"])).read_text() == "{}"
    cleanup = data["cleanup"]
    assert isinstance(cleanup, dict)
    assert set(cleanup) == {"command", "auto_cleanup_after_seconds"}
    assert cleanup["auto_cleanup_after_seconds"] == 300


def test_cleanup_command_is_a_valid_directly_executable_shell_command(tmp_path: Path) -> None:
    data = report(tmp_path)
    cleanup = data["cleanup"]
    assert isinstance(cleanup, dict)
    subprocess.run(str(cleanup["command"]), shell=True, check=True)  # noqa: S602 - the point
    assert not Path(str(data["output_file"])).exists()


def test_files_older_than_auto_cleanup_after_seconds_are_pruned_when_any_command_next_runs(
    tmp_path: Path,
) -> None:
    kept = Path(str(report(tmp_path, "--keep", "300")["output_file"]))
    expired = Path(str(report(tmp_path, "--keep", "1")["output_file"]))
    assert expired.exists() and kept.exists()
    time.sleep(2.1)  # the expiry rounds up to a whole second
    run(session_app, ["environ"], env={"TMPDIR": str(tmp_path)})
    assert not expired.exists() and not expired.parent.exists()
    assert kept.exists()


def test_a_session_directory_left_by_a_killed_run_is_pruned_after_a_day(tmp_path: Path) -> None:
    left = scratch(tmp_path)
    stale = Path(str(meta_of(left)["session_tmp_dir"]))
    stale.mkdir(mode=0o700)
    day_ago = time.time() - 2 * 86_400
    os.utime(stale, (day_ago, day_ago))
    fresh = stale.with_name("fresh")
    fresh.mkdir(mode=0o700)
    run(session_app, ["environ"], env={"TMPDIR": str(tmp_path)})
    assert not stale.exists() and fresh.exists()


@pytest.mark.skipif(WINDOWS, reason="POSIX permission bits")
def test_all_temp_files_are_created_with_mode_0600(tmp_path: Path) -> None:
    assert data_of(scratch(tmp_path))["file_mode"] == 0o600
    assert report(tmp_path)["file_mode"] == 0o600


@pytest.mark.skipif(WINDOWS, reason="POSIX permission bits")
def test_all_temp_directories_are_created_with_mode_0700(tmp_path: Path) -> None:
    assert data_of(scratch(tmp_path))["dir_mode"] == 0o700
    assert report(tmp_path)["dir_mode"] == 0o700
    root = tmp_path / f"sessionctl-{os.getuid()}"
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / "out").stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(WINDOWS, reason="POSIX permission bits")
def test_umask_does_not_widen_permissions(tmp_path: Path) -> None:
    old = os.umask(0)
    try:
        data = data_of(scratch(tmp_path))
        written = report(tmp_path)
    finally:
        os.umask(old)
    assert (data["dir_mode"], data["file_mode"]) == (0o700, 0o600)
    assert (written["dir_mode"], written["file_mode"]) == (0o700, 0o600)


@pytest.mark.skipif(WINDOWS, reason="symlinks need privileges on Windows")
def test_a_planted_symlink_root_is_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / f"sessionctl-{os.getuid()}").symlink_to(elsewhere)
    status, envelope, _ = run(session_app, ["scratch"], env={"TMPDIR": str(tmp_path)})
    error = envelope["error"]
    assert isinstance(error, dict)
    assert status == 4 and error["code"] == "TEMP_DIR_UNSAFE"
    assert list(elsewhere.iterdir()) == []


def test_the_cleanup_built_in_removes_output_files(tmp_path: Path) -> None:
    path = Path(str(report(tmp_path)["output_file"]))
    status, envelope, _ = run(
        session_app, ["cleanup", "--confirm-destructive"], env={"TMPDIR": str(tmp_path)}
    )
    assert status == 0, envelope
    cleaned = data_of(envelope)["cleaned"]
    assert isinstance(cleaned, list) and str(path.parent) in [c["path"] for c in cleaned]
    assert not path.exists()


# F-060: third-party stdout

NOISYCTL = Path(__file__).resolve().parent / "fixture_noisy_app.py"


def noisy(command: str) -> tuple[dict[str, object], str]:
    proc = subprocess.run(
        [sys.executable, str(NOISYCTL), command],
        env=BASE_ENV,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout), proc.stderr  # one JSON document: nothing ahead of it


def stray(envelope: dict[str, object]) -> list[object]:
    warnings = envelope["warnings"]
    assert isinstance(warnings, list)
    return [w for w in warnings if w["code"] == "THIRD_PARTY_STDOUT"]


def test_a_library_that_calls_print_on_import_does_not_contaminate_the_json_stdout() -> None:
    envelope, err = noisy("quiet")
    assert data_of(envelope) == {"status": "ok"}
    assert "initialized\n" in err


def test_the_intercepted_string_appears_as_a_warning_with_its_text() -> None:
    envelope, _ = noisy("quiet")
    assert stray(envelope) == [
        {
            "code": "THIRD_PARTY_STDOUT",
            "message": "Third-party code wrote to stdout; the text is in this warning instead",
            # bytes counts what reached descriptor 1: print ends lines with \r\n on Windows
            "context": {"text": "initialized", "bytes": len("initialized" + os.linesep)},
        }
    ]


def test_json_loads_of_stdout_succeeds_when_native_code_or_a_child_writes_to_it() -> None:
    envelope, err = noisy("native")
    [warning] = stray(envelope)
    assert warning["context"]["text"] == "initialized\nfrom C code\nfrom a child"  # type: ignore[index]
    assert "from C code\n" in err and "from a child\n" in err


def test_json_shaped_writes_go_to_stderr_without_a_warning() -> None:
    envelope, err = noisy("json")
    assert [w["context"]["text"] for w in stray(envelope)] == ["initialized"]  # type: ignore[index]
    assert '{"status": "ok"}\n' in err


CAPSYS_SUITE = """
import pytest

from treaty import App, Ctx, NoArgs

app = App("demo", version="1.0.0")


@app.command("go", description="Go", danger_level="safe", exit_codes=())
def go(args: NoArgs, ctx: Ctx) -> dict[str, object]:
    print("stray")
    return {"ok": True}


@pytest.mark.parametrize("n", range(5))
def test_go(n: int, capsys: pytest.CaptureFixture[str]) -> None:
    assert app.run(["go"]) == 0
    assert '"THIRD_PARTY_STDOUT"' in capsys.readouterr().out
"""


def test_an_in_process_run_under_capsys_leaves_no_unraisable_flush(tmp_path: Path) -> None:
    # Issue #65: the stdout stand-in's finalizer flushed pytest's closed capture stream
    (tmp_path / "test_suite.py").write_text(CAPSYS_SUITE)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-W",
            "error::pytest.PytestUnraisableExceptionWarning",
            "test_suite.py",
        ],
        cwd=tmp_path,
        env={**BASE_ENV, "NO_COLOR": "1"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "5 passed" in proc.stdout
    assert "Unraisable" not in proc.stdout + proc.stderr


class FlushCounter(io.TextIOWrapper):
    """A stderr that counts its flushes and, once closed, refuses them"""

    def __init__(self) -> None:
        super().__init__(io.BytesIO(), encoding="utf-8")
        self.flushes = 0

    def flush(self) -> None:
        super().flush()
        self.flushes += 1


def test_a_stdout_stand_in_that_outlives_its_run_still_works() -> None:
    # Overlapping runs can restore a finished run's stand-in as sys.stdout: it stays open
    app = App("keepctl", version="1.0.0")
    kept: list[object] = []

    @app.command("go", description="Go", danger_level="safe", exit_codes=())
    def go(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        kept.append(sys.stdout)
        return {"ok": True}

    # StringIO's flush tolerates a closed stream; a real stream's raises ValueError
    first, second = FlushCounter(), FlushCounter()
    for err in (first, second):
        assert app.run(["go"], stdout=io.StringIO(), stderr=err, env={}, isatty=False) == 0
    old = kept[0]
    assert isinstance(old, _StrayStdout) and old is not kept[1]
    assert old.isatty() is False
    assert old.write("late\n") == len("late\n")
    old.writelines(["later\n"])
    flushed = first.flushes
    old.flush()
    assert first.flushes == flushed + 1  # an open stderr is still flushed
    assert old.take() == ("late\nlater\n", len("late\nlater\n"))  # still counted
    first.close()
    old.flush()  # its stderr is gone: nothing to flush into, and no error
    old.close()


# O-018: cache flags


def caching_app() -> App:
    app = App("cachectl", version="1.0.0")
    fetches: list[str] = []

    @app.command(
        "price",
        description="Fetch a price, cached for an hour",
        danger_level="safe",
        exit_codes=(),
        cache=CachePolicy(ttl_seconds=3600),
    )
    def price(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        hit = ctx.cache.get("btc")
        if hit is not None:
            return {"price": hit.decode(), "fetched": False}
        fetches.append("btc")
        value = f"{100 + len(fetches)}"
        ctx.cache.put("btc", value.encode())
        return {"price": value, "fetched": True}

    @app.command("plain", description="No cache", danger_level="safe", exit_codes=())
    def plain(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    return app


def priced(app: App, tmp_path: Path, *flags: str) -> tuple[dict[str, object], dict[str, object]]:
    status, envelope, _ = run(app, ["price", *flags], env={"XDG_CACHE_HOME": str(tmp_path)})
    assert status == 0, envelope
    return data_of(envelope), meta_of(envelope)


def entry_of(tmp_path: Path) -> Path:
    [entry] = (tmp_path / "cachectl" / "price").iterdir()
    return entry


def test_a_second_run_reads_the_cache_and_says_so_in_meta_cache_used(tmp_path: Path) -> None:
    app = caching_app()
    first, first_meta = priced(app, tmp_path)
    second, second_meta = priced(app, tmp_path)
    assert first == {"price": "101", "fetched": True} and first_meta["cache_used"] is False
    assert second == {"price": "101", "fetched": False} and second_meta["cache_used"] is True


def test_no_cache_causes_the_command_to_bypass_all_declared_cache_files(tmp_path: Path) -> None:
    app = caching_app()
    priced(app, tmp_path)
    data, meta = priced(app, tmp_path, "--no-cache")
    assert data == {"price": "102", "fetched": True} and meta["cache_used"] is False
    assert entry_of(tmp_path).read_bytes() == b"101"  # not written either


def test_cache_ttl_0_is_equivalent_to_no_cache(tmp_path: Path) -> None:
    app = caching_app()
    priced(app, tmp_path)
    data, meta = priced(app, tmp_path, "--cache-ttl", "0")
    assert data == {"price": "102", "fetched": True} and meta["cache_used"] is False
    assert entry_of(tmp_path).read_bytes() == b"101"


def test_cache_files_older_than_cache_ttl_seconds_are_treated_as_missing(tmp_path: Path) -> None:
    app = caching_app()
    priced(app, tmp_path)
    minute_ago = time.time() - 60
    os.utime(entry_of(tmp_path), (minute_ago, minute_ago))
    assert priced(app, tmp_path, "--cache-ttl", "120")[0]["fetched"] is False
    assert priced(app, tmp_path, "--cache-ttl", "30")[0] == {"price": "102", "fetched": True}


def test_the_cache_flags_are_absent_on_commands_that_declare_no_cache_side_effects(
    tmp_path: Path,
) -> None:
    app = caching_app()
    flags = {p: set(c["flags"]) for p, c in app.manifest()["commands"].items()}  # type: ignore[attr-defined]
    assert {"no-cache", "cache-ttl"} <= flags["price"]
    assert not {"no-cache", "cache-ttl"} & flags["plain"]
    status, envelope, _ = run(app, ["plain", "--no-cache"])
    assert status == 2


def test_a_cache_command_declares_its_cache_side_effect(tmp_path: Path) -> None:
    entry = caching_app().manifest()["commands"]["price"]  # type: ignore[index]
    assert entry["filesystem_side_effects"] == [
        {"path": "~/.cache/cachectl/price/", "type": "cache", "ttl_seconds": 3600}
    ]


def test_the_cleanup_built_in_removes_the_cache(tmp_path: Path) -> None:
    app = caching_app()
    priced(app, tmp_path)
    status, envelope, _ = run(
        app, ["cleanup", "--confirm-destructive"], env={"XDG_CACHE_HOME": str(tmp_path)}
    )
    cleaned = data_of(envelope)["cleaned"]
    assert status == 0 and isinstance(cleaned, list)
    assert [c["path"] for c in cleaned] == [str(tmp_path / "cachectl" / "price")]
    assert priced(app, tmp_path)[0]["fetched"] is True


@pytest.mark.skipif(WINDOWS, reason="POSIX permission bits")
def test_cache_files_are_private(tmp_path: Path) -> None:
    priced(caching_app(), tmp_path)
    assert entry_of(tmp_path).stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "cachectl").stat().st_mode & 0o777 == 0o700


def test_the_cache_declared_audit_rule_flags_a_hand_written_cache() -> None:
    app = App("handctl", version="1.0.0")

    @app.command("fetch", description="Fetch", danger_level="safe", exit_codes=())
    def fetch(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        where = Path(ctx.env["HOME"]) / ".cache" / "handctl"
        where.mkdir(parents=True, exist_ok=True)
        return {}

    rule = next(r for r in audit(app, "handctl", limit=5).rules if r.id == "cache-declared")
    [finding] = rule.findings
    assert finding.command == "fetch" and "CachePolicy" in finding.fix
