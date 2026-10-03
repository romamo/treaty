"""Additional command declarations: REQ-C-010, REQ-C-011, REQ-C-018, REQ-C-019, REQ-C-024,
REQ-O-031."""

import io
import json
import os
import shlex
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from conftest import WINDOWS, spec_validator

from treaty import (
    App,
    Arg,
    Background,
    Ctx,
    Dependency,
    Flag,
    NoArgs,
    RegistrationError,
    SideEffect,
    Subprocess,
)
from treaty._audit import audit
from treaty._deps import Version
from treaty._subprocess import _DETACHED
from treaty._values import CommandPath

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


# REQ-O-031


PYTHON = Dependency(
    name="python",
    check_command=(sys.executable, "--version"),
    version_regex=r"Python (\d+\.\d+)",
    min_version="3.0",
    fix_command="uv python install 3.14",
)


def deps_app(*deps: Dependency) -> App:
    return App("depctl", version="1.0.0", dependencies=deps)


def doctor(app: App, path: str | None = None) -> tuple[int, dict[str, object]]:
    return run(app, ["doctor"], env=None if path is None else {"PATH": path})


def entries(envelope: dict[str, object], key: str) -> dict[str, dict[str, object]]:
    data = envelope["data"]
    assert isinstance(data, dict)
    return {e["name"]: e for e in data[key]}


def test_a_dependency_below_min_version_appears_as_a_failed_check_in_doctor() -> None:
    old = replace(PYTHON, name="python-next", min_version="99.0")
    code, env = doctor(deps_app(PYTHON, old))
    assert code == 4
    error = env["error"]
    assert isinstance(error, dict) and error["code"] == "DOCTOR_CHECKS_FAILED"
    assert error["context"]["failed"] == ["python-next"]
    found = entries(env, "dependencies")
    assert found["python"]["ok"] is True
    assert found["python-next"]["ok"] is False
    assert found["python-next"]["found_version"] == found["python"]["found_version"]
    spec_validator("response-envelope").validate(env)


def test_a_missing_dependency_is_a_failed_check_with_no_version() -> None:
    gone = replace(PYTHON, name="gone", check_command=("treaty-no-such-tool", "--version"))
    code, env = doctor(deps_app(gone))
    assert code == 4
    entry = entries(env, "dependencies")["gone"]
    assert entry["ok"] is False and entry["found_version"] is None
    assert "not on PATH" in str(entry["error"])


def test_a_dependency_above_max_version_appears_as_a_compatibility_warning_in_doctor() -> None:
    code, env = doctor(deps_app(replace(PYTHON, max_version="3.1")))
    assert code == 0
    entry = entries(env, "dependencies")["python"]
    assert entry["ok"] is True and entry["compatible"] is False and entry["max_version"] == "3.1"
    [warning] = env["warnings"]  # type: ignore[misc]
    assert warning["code"] == "DEPENDENCY_ABOVE_MAX"
    assert warning["context"]["name"] == "python"


def test_doctor_format_json_includes_a_dependencies_array_with_all_declared_dependencies() -> None:
    other = replace(PYTHON, name="py")
    code, env = doctor(deps_app(PYTHON, other))
    assert code == 0
    data = env["data"]
    assert isinstance(data, dict)
    # Each dependency is a check too (REQ-O-026)
    assert [(c["name"], c["ok"], c["commands"]) for c in data["checks"]] == [
        ("py", True, []),
        ("python", True, []),
    ]
    [first, second] = data["dependencies"]
    assert [first["name"], second["name"]] == ["py", "python"]
    assert first == {
        "name": "py",
        "check_command": shlex.join([sys.executable, "--version"]),
        "version_regex": r"Python (\d+\.\d+)",
        "min_version": "3.0",
        "fix_command": "uv python install 3.14",
        "found_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "ok": True,
    }


def test_the_fix_command_for_each_failed_dependency_is_an_executable_shell_command() -> None:
    gone = replace(PYTHON, name="gone", check_command=("treaty-no-such-tool", "--version"))
    _, env = doctor(deps_app(gone))
    entry = entries(env, "dependencies")["gone"]
    assert shlex.split(str(entry["fix_command"])) == ["uv", "python", "install", "3.14"]


def test_the_manifest_lists_declared_dependencies_at_the_root() -> None:
    manifest = deps_app(PYTHON).manifest()
    assert manifest["dependencies"] == [
        {
            "name": "python",
            "check_command": shlex.join([sys.executable, "--version"]),
            "version_regex": r"Python (\d+\.\d+)",
            "min_version": "3.0",
            "fix_command": "uv python install 3.14",
        }
    ]
    spec_validator("manifest-response").validate(manifest)
    assert "dependencies" not in deps_app().manifest()
    assert deps_app().manifest()["etag"] != manifest["etag"]


def test_a_dependency_is_declared_with_an_argument_list_and_versions() -> None:
    with pytest.raises(RegistrationError, match="argument list"):
        replace(PYTHON, check_command="python --version")  # type: ignore[arg-type]
    with pytest.raises(RegistrationError, match="dotted numbers"):
        replace(PYTHON, min_version=">=3.0")
    with pytest.raises(RegistrationError, match="twice"):
        deps_app(PYTHON, PYTHON)


def test_versions_compare_as_numbers() -> None:
    assert Version("1.10") > Version("1.9")
    assert not Version("1.19") < Version("1.19.0")
    assert Version("2") > Version("1.99.99")


# REQ-C-018


def tools_app(platform: tuple[str, ...] = ()) -> App:
    app = App("pkg", version="1.0.0")

    @app.command(
        "package",
        description="Build a package",
        danger_level="safe",
        exit_codes=(),
        platform=platform,
        required_tools={"fakectl": "1.2", "oldctl": "2.0", "gonectl": "1.0"},
    )
    def package(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    return app


def fake_tools(tmp_path: Path) -> str:
    for name, version in (("fakectl", "1.2.3"), ("oldctl", "1.9.0")):
        tool = tmp_path / name
        tool.write_text(f"#!/bin/sh\necho '{name} version {version}'\n")
        tool.chmod(0o755)
    return str(tmp_path)


@pytest.mark.skipif(WINDOWS, reason="the stand-in tools are POSIX scripts")
def test_doctor_reports_a_check_failure_for_each_missing_or_outdated_required_tool(
    tmp_path: Path,
) -> None:
    code, env = doctor(tools_app(), fake_tools(tmp_path))
    assert code == 4
    checks = entries(env, "checks")
    assert checks["fakectl"] == {
        "name": "fakectl",
        "ok": True,
        "version": "1.2.3",
        "required": "1.2",
        "commands": ["package"],
    }
    assert checks["oldctl"]["ok"] is False and checks["oldctl"]["version"] == "1.9.0"
    assert checks["gonectl"]["ok"] is False and checks["gonectl"]["version"] is None
    assert checks["gonectl"]["fix"] == "install gonectl 1.0 or newer"
    error = env["error"]
    assert isinstance(error, dict) and error["context"]["failed"] == ["gonectl", "oldctl"]


@pytest.mark.skipif(WINDOWS, reason="the stand-in tools are POSIX scripts")
def test_a_required_tool_uses_the_fix_of_a_dependency_of_the_same_name(tmp_path: Path) -> None:
    dep = Dependency("gonectl", ("gonectl", "--version"), "1.0", "brew install gonectl")
    app = App("pkg", version="1.0.0", dependencies=[dep])

    @app.command(
        "package",
        description="x",
        danger_level="safe",
        exit_codes=(),
        required_tools={"gonectl": "1.0"},
    )
    def package(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    _, env = doctor(app, fake_tools(tmp_path))
    assert entries(env, "checks")["gonectl"]["fix"] == "brew install gonectl"


# #296: a program without --version is needed at any version, on PATH


def any_version_app(tools: object) -> App:
    app = App("ledger", version="1.0.0")

    @app.command(
        "fmt",
        description="Format the ledger",
        danger_level="safe",
        exit_codes=(),
        required_tools=tools,  # type: ignore[arg-type]  # the bad shapes are the point
    )
    def fmt(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    return app


def no_version_tool(tmp_path: Path) -> Path:
    """A program that fails ``--version`` and leaves a mark when run at all"""
    tool = tmp_path / "bean-format"
    tool.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(tmp_path / 'ran'))}\nexit 1\n")
    tool.chmod(0o755)
    return tmp_path / "ran"


@pytest.mark.skipif(WINDOWS, reason="the stand-in tool is a POSIX script")
@pytest.mark.parametrize("tools", [{"bean-format": None}, ("bean-format",), ["bean-format"]])
def test_doctor_checks_a_tool_needed_at_any_version_is_on_path_without_running_it(
    tmp_path: Path, tools: object
) -> None:
    ran = no_version_tool(tmp_path)
    code, env = doctor(any_version_app(tools), str(tmp_path))
    assert code == 0
    assert entries(env, "checks")["bean-format"] == {
        "name": "bean-format",
        "ok": True,
        "version": None,
        "required": None,
        "commands": ["fmt"],
    }
    assert not ran.exists()


def test_doctor_fails_a_tool_needed_at_any_version_that_is_not_on_path(tmp_path: Path) -> None:
    code, env = doctor(any_version_app({"bean-format": None}), str(tmp_path))
    assert code == 4
    check = entries(env, "checks")["bean-format"]
    assert check["ok"] is False and check["required"] is None
    assert check["fix"] == "install bean-format"
    assert check["error"] == "bean-format is not on PATH"


@pytest.mark.skipif(WINDOWS, reason="the stand-in tools are POSIX scripts")
def test_a_floor_from_another_command_still_runs_the_version_check(tmp_path: Path) -> None:
    app = any_version_app({"fakectl": None})

    @app.command(
        "pack", description="x", danger_level="safe", exit_codes=(), required_tools={"fakectl": "2"}
    )
    def pack(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    _, env = doctor(app, fake_tools(tmp_path))
    check = entries(env, "checks")["fakectl"]
    assert check["ok"] is False and check["version"] == "1.2.3" and check["required"] == "2"
    assert check["commands"] == ["fmt", "pack"]


def test_the_manifest_lists_a_tool_needed_at_any_version_as_a_star() -> None:
    app = any_version_app({"bean-format": None, "git": "2.30"})
    manifest = app.manifest()
    assert manifest["commands"]["fmt"]["required_tools"] == {"bean-format": "*", "git": "2.30"}  # type: ignore[index]
    spec_validator("manifest-response").validate(manifest)
    listed = any_version_app(("bean-format",)).manifest()
    assert listed["commands"]["fmt"]["required_tools"] == {"bean-format": "*"}  # type: ignore[index]


@pytest.mark.parametrize(
    ("tools", "message"),
    [
        ({"": None}, "is not a program name"),
        ({3: None}, "is not a program name"),
        (("",), "is not a program name"),
        ((3,), "is not a program name"),
        (("git", "git"), "names 'git' twice"),
        ("git", "or to None for any version"),
        ({"git": "latest"}, "not dotted numbers"),
    ],
)
def test_required_tools_refuses_a_malformed_declaration(tools: object, message: str) -> None:
    with pytest.raises(RegistrationError, match=message):
        any_version_app(tools)


def test_invoking_a_linux_only_command_on_macos_emits_a_compatibility_warning() -> None:
    elsewhere = "linux" if sys.platform != "linux" else "darwin"
    code, env = run(tools_app((elsewhere,)), ["package"])
    assert code == 0
    [warning] = env["warnings"]  # type: ignore[misc]
    assert warning["code"] == "UNSUPPORTED_PLATFORM"
    assert warning["context"] == {"platform": sys.platform, "supported": [elsewhere]}
    here = "win32" if sys.platform == "win32" else sys.platform.rstrip("0123456789")
    _, env = run(tools_app((here,)), ["package"])
    assert env["warnings"] == []


def test_the_schema_output_for_each_command_includes_platform_and_required_tools() -> None:
    out = io.StringIO()
    tools_app(("linux",)).run(
        ["package", "--schema", "--format", "json"], stdout=out, stderr=io.StringIO(), env={}
    )
    schema = json.loads(out.getvalue())["data"]
    assert schema["platform"] == ["linux"]
    assert schema["required_tools"] == {"fakectl": "1.2", "gonectl": "1.0", "oldctl": "2.0"}
    spec_validator("manifest-response").validate(tools_app(("linux",)).manifest())


def test_platform_takes_sys_platform_values() -> None:
    with pytest.raises(RegistrationError, match="sys.platform"):
        tools_app(("macos",))


def test_audit_flags_a_program_missing_from_required_tools() -> None:
    rules = {r.id: r for r in audit(echo_app(declared=False), "echoer", limit=3).rules}
    [finding] = rules["required-tools"].findings
    assert finding.fix == (
        'required_tools={"echo": "<minimum version>"}, or {"echo": None} when any version '
        "will do or it has no --version"
    )


# 13-D1: built-ins added by 08 yield to an app command of the same name


def test_an_app_command_named_doctor_replaces_the_built_in() -> None:
    app = App("mine", version="1.0.0")
    assert CommandPath("doctor") in app.builtins

    @app.command("doctor", description="The app's own", danger_level="safe", exit_codes=())
    def own(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"doctor": "mine"}

    assert CommandPath("doctor") not in app.builtins
    _, env = run(app, ["doctor"])
    assert env["data"] == {"doctor": "mine"}
    rules = {r.id: r for r in audit(app, "mine", limit=3).rules}
    [finding] = rules["builtin-shadowed"].findings
    assert finding.command == "doctor" and "mine-doctor" in finding.fix


def test_an_app_group_named_doctor_replaces_the_built_in() -> None:
    app = App("mine", version="1.0.0")
    group = app.group("doctor", description="Checks")

    @group.command("run", description="x", danger_level="safe", exit_codes=())
    def run_(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    assert set(app.commands) >= {CommandPath("doctor.run")}
    assert CommandPath("doctor") not in app.commands


def test_manifest_version_and_exec_stay_reserved() -> None:
    def handler(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    app = App("mine", version="1.0.0")
    for name in ("manifest", "version", "exec"):
        with pytest.raises(RegistrationError, match="already registered"):
            app.command(name, description="x", danger_level="safe", exit_codes=())(handler)


# REQ-C-011


def side_effects_app(root: Path, clearable_with: str | None = None) -> App:
    app = App("fetcher", version="1.0.0")

    @app.command(
        "fetch-schema",
        description="Fetch and cache a schema",
        danger_level="safe",
        exit_codes=(),
        filesystem_side_effects=[
            SideEffect(
                f"{root}/cache/schemas/", "cache", ttl_seconds=3600, clearable_with=clearable_with
            ),
            SideEffect(f"{root}/tmp/fetch-{{session_id}}/", "temp"),
            SideEffect(f"{root}/logs/{{date}}.log", "log"),
            SideEffect(f"{root}/credentials.json", "credential"),
        ],
    )
    def fetch_schema(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        cache = root / "cache" / "schemas"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "a.json").write_text("{}")
        for session in ("1", "2"):
            (root / "tmp" / f"fetch-{session}").mkdir(parents=True, exist_ok=True)
        (root / "logs").mkdir(exist_ok=True)
        (root / "logs" / "2026-09-27.log").write_text("log")
        (root / "credentials.json").write_text("{}")
        return {}

    return app


def test_a_command_that_writes_to_a_cache_directory_declares_that_path_in_filesystem_side_effects(
    tmp_path: Path,
) -> None:
    app = side_effects_app(tmp_path, clearable_with="fetcher cleanup")
    manifest = app.manifest()
    effects = manifest["commands"]["fetch-schema"]["filesystem_side_effects"]  # type: ignore[index]
    assert effects[0] == {
        "path": f"{tmp_path}/cache/schemas/",
        "type": "cache",
        "ttl_seconds": 3600,
        "clearable_with": "fetcher cleanup",
    }
    assert [e["type"] for e in effects] == ["cache", "temp", "log", "credential"]
    spec_validator("manifest-response").validate(manifest)
    out = io.StringIO()
    app.run(
        ["fetch-schema", "--schema", "--format", "json"], stdout=out, stderr=io.StringIO(), env={}
    )
    assert json.loads(out.getvalue())["data"]["filesystem_side_effects"] == effects


@pytest.mark.skipif(WINDOWS, reason="expected paths use / separators")
def test_tool_cleanup_removes_all_paths_declared_as_temp_or_cache(tmp_path: Path) -> None:
    app = side_effects_app(tmp_path)
    assert run(app, ["fetch-schema"])[0] == 0
    code, env = run(app, ["cleanup", "--dry-run"])
    assert code == 0
    data = env["data"]
    assert isinstance(data, dict) and data["effect"] == "would_delete"
    expected = [
        f"{tmp_path}/cache/schemas",
        f"{tmp_path}/logs/2026-09-27.log",
        f"{tmp_path}/tmp/fetch-1",
        f"{tmp_path}/tmp/fetch-2",
    ]
    assert data["would_affect"]["resources"] == expected
    assert (tmp_path / "cache" / "schemas").exists()
    code, env = run(app, ["cleanup", "--confirm-destructive"])
    assert code == 0
    data = env["data"]
    assert isinstance(data, dict) and data["effect"] == "deleted"
    assert [c["path"] for c in data["cleaned"]] == expected
    assert [c["type"] for c in data["cleaned"]] == ["cache", "log", "temp", "temp"]
    assert not (tmp_path / "cache" / "schemas").exists()
    assert not (tmp_path / "tmp" / "fetch-1").exists()
    assert (tmp_path / "credentials.json").exists()
    _, env = run(app, ["cleanup", "--confirm-destructive"])
    assert env["data"] == {
        "effect": "noop",
        "cleaned": [],
        "total_bytes_freed": 0,
        "skipped": [],
        "failed": [],
        "would_affect": None,
    }


@pytest.mark.skipif(WINDOWS, reason="expected paths use / separators")
def test_cleanup_expands_a_home_path_from_the_run_environment(tmp_path: Path) -> None:
    app = App("homey", version="1.0.0")

    @app.command(
        "x",
        description="x",
        danger_level="safe",
        exit_codes=(),
        filesystem_side_effects=[SideEffect("~/.cache/homey/", "cache")],
    )
    def x(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    (tmp_path / ".cache" / "homey").mkdir(parents=True)
    _, env = run(app, ["cleanup", "--confirm-destructive"], env={"HOME": str(tmp_path)})
    cleaned = env["data"]["cleaned"]  # type: ignore[index]
    assert [c["path"] for c in cleaned] == [f"{tmp_path}/.cache/homey"]


def test_clearable_with_must_name_a_command_when_the_manifest_is_built(tmp_path: Path) -> None:
    app = side_effects_app(tmp_path, clearable_with="fetcher cache clear")
    rules = {r.id: r for r in audit(app, "fetcher", limit=3).rules}
    [finding] = rules["declared-commands"].findings
    assert "names no command" in finding.message
    with pytest.raises(RegistrationError, match="clearable_with"):
        app.manifest()


def test_a_side_effect_is_an_absolute_path_of_a_known_type() -> None:
    with pytest.raises(RegistrationError, match="absolute"):
        SideEffect("cache/", "cache")
    with pytest.raises(RegistrationError, match="cache, log, temp, credential, config"):
        SideEffect("/tmp/x", "scratch")


def test_audit_flags_an_undeclared_disk_write(tmp_path: Path) -> None:
    app = App("writer", version="1.0.0")

    @app.command("x", description="x", danger_level="safe", exit_codes=())
    def x(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        (tmp_path / "cache.json").write_text("{}")
        return {}

    rules = {r.id: r for r in audit(app, "writer", limit=3).rules}
    [finding] = rules["fs-side-effects"].findings
    assert "write_text" in finding.message and "treaty.SideEffect" in finding.fix
    clean = {r.id: r for r in audit(side_effects_app(tmp_path), "fetcher", limit=3).rules}
    assert clean["fs-side-effects"].passed


# REQ-C-010

SLEEPER = [sys.executable, "-c", "import time; print('watching', flush=True); time.sleep(60)"]


@dataclass(frozen=True, slots=True)
class Watching:
    background_pid: int
    cleanup_command: str
    pid_file: str


def watcher_app(state: Path, lifetime: int = 3600, cleanup: str = "watch stop-watcher") -> App:
    app = App("watch", version="1.0.0", state_dir=state)

    @app.command(
        "start-watcher",
        description="Start a watcher",
        danger_level="safe",
        exit_codes=(),
        background=Background(cleanup, max_lifetime_seconds=lifetime),
    )
    def start(args: NoArgs, ctx: Ctx) -> Watching:
        spawned = ctx.spawn(SLEEPER)
        return Watching(
            spawned.pid, f"watch stop-watcher --pid {spawned.pid}", str(spawned.pid_file)
        )

    @app.command("stop-watcher", description="Stop the watcher", danger_level="safe", exit_codes=())
    def stop(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    return app


def detached(pid: int) -> subprocess.Popen[bytes]:
    return next(p for p in _DETACHED if p.pid == pid)


def stop(pid: int) -> None:
    proc = detached(pid)
    proc.kill()
    proc.wait(timeout=10)


def test_the_schema_output_for_background_commands_includes_spawns_background_process_true(
    tmp_path: Path,
) -> None:
    app = watcher_app(tmp_path)
    out = io.StringIO()
    app.run(
        ["start-watcher", "--schema", "--format", "json"], stdout=out, stderr=io.StringIO(), env={}
    )
    schema = json.loads(out.getvalue())["data"]
    assert schema["spawns_background_process"] is True
    assert schema["cleanup_command"] == "watch stop-watcher"
    assert schema["max_lifetime_seconds"] == 3600
    spec_validator("manifest-response").validate(app.manifest())


def test_the_response_data_includes_background_pid_and_cleanup_command(tmp_path: Path) -> None:
    code, env = run(watcher_app(tmp_path), ["start-watcher"])
    assert code == 0
    data = env["data"]
    assert isinstance(data, dict)
    pid = data["background_pid"]
    assert isinstance(pid, int) and data["cleanup_command"] == f"watch stop-watcher --pid {pid}"
    try:
        assert detached(pid).poll() is None  # it outlived the run
        pid_file = Path(data["pid_file"])
        assert pid_file == tmp_path / "background" / "start-watcher.pids"
        assert pid_file.read_text().split()[0] == str(pid)
        logs = list((tmp_path / "background").glob("start-watcher.*.log"))
        deadline = time.monotonic() + 10
        while "watching" not in logs[0].read_text() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert logs[0].read_text() == "watching\n"
    finally:
        stop(pid)


def test_the_framework_refuses_to_register_a_background_command_without_its_metadata() -> None:
    app = App("watch", version="1.0.0")
    with pytest.raises(RegistrationError, match="background=treaty.Background"):

        @app.command("start", description="x", danger_level="safe", exit_codes=())
        def start(args: NoArgs, ctx: Ctx) -> Watching:
            spawned = ctx.spawn(SLEEPER)
            return Watching(spawned.pid, "watch stop", "")

    with pytest.raises(RegistrationError, match="'background_pid' and 'cleanup_command'"):

        @app.command(
            "begin",
            description="x",
            danger_level="safe",
            exit_codes=(),
            background=Background("watch stop", max_lifetime_seconds=60),
        )
        def begin(args: NoArgs, ctx: Ctx) -> Login:
            return Login("x")

    with pytest.raises(RegistrationError, match="max_lifetime_seconds"):
        Background("watch stop", max_lifetime_seconds=0)


def test_cleanup_command_must_name_a_command_when_the_manifest_is_built(tmp_path: Path) -> None:
    app = watcher_app(tmp_path, cleanup="watch halt")
    with pytest.raises(RegistrationError, match="cleanup_command"):
        app.manifest()


def test_a_later_spawn_stops_processes_past_their_max_lifetime(tmp_path: Path) -> None:
    app = watcher_app(tmp_path, lifetime=1)
    _, first = run(app, ["start-watcher"])
    old = first["data"]["background_pid"]  # type: ignore[index]
    [entry] = (tmp_path / "background" / "start-watcher.pids").read_text().splitlines()
    expires = int(entry.split()[1])
    assert expires <= time.time() + 2  # one second's lifetime, rounded to a whole second
    wait_until = time.monotonic() + 10
    while time.time() < expires:  # the recorded deadline, not a guess at it
        assert time.monotonic() < wait_until
        time.sleep(0.05)
    _, second = run(app, ["start-watcher"])
    new = second["data"]["background_pid"]  # type: ignore[index]
    try:
        assert detached(old).wait(timeout=10) != 0
        lines = (tmp_path / "background" / "start-watcher.pids").read_text().splitlines()
        entries = {int(line.split()[0]): line.split()[2:] for line in lines}
        assert len(entries[new]) == 1  # its start time, nothing to stop yet
        # SIGTERMed at its deadline, the old one waits for the next spawn's SIGKILL
        assert old not in entries or entries[old][1:] == ["stopping"]
    finally:
        stop(new)


def test_audit_flags_an_undeclared_background_process() -> None:
    app = App("watch", version="1.0.0")

    @app.command("start", description="x", danger_level="safe", exit_codes=())
    def start(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        subprocess.Popen(["sleep", "60"], start_new_session=True)
        return {}

    rules = {r.id: r for r in audit(app, "watch", limit=3).rules}
    [finding] = rules["background-declared"].findings
    assert "subprocess.Popen" in finding.message and "ctx.spawn" in finding.fix
