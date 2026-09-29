"""Config layer and env namespace: REQ-F-073, O-042, F-028, O-015, O-016, O-024, O-036, F-076"""

import hashlib
import io
import json
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fixture_config_app
import pytest
from conftest import WINDOWS

from treaty import App, Ctx, Init, NoArgs, RegistrationError
from treaty._audit import audit
from treaty._idempotency import state_dir
from treaty._values import InstanceId

CONFIGCTL = Path(__file__).resolve().parent / "fixture_config_app.py"


@dataclass(frozen=True, slots=True)
class Row:
    id: str
    name: str


def make_app() -> App:
    app = App("my-tool", version="1.0.0", description="Rows")

    @app.command("list", description="List rows", danger_level="safe", exit_codes=())
    def list_(args: NoArgs, ctx: Ctx) -> list[Row]:
        return [Row("1", "alice")]

    return app


def run(
    argv: list[str], env: dict[str, str] | None = None, *, app: App | None = None
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = (app or make_app()).run(argv, stdout=out, stderr=err, env=env or {}, isatty=False)
    return code, out.getvalue(), err.getvalue()


def process_env(**extra: str) -> dict[str, str]:
    """``env -i`` plus what a Python process needs to start"""
    keep = ("SYSTEMROOT", "PATH") if WINDOWS else ()
    return {**{k: os.environ[k] for k in keep if k in os.environ}, **extra}


# REQ-F-073


def test_all_tool_specific_env_vars_are_documented_under_the_toolname_prefix() -> None:
    code, _, err = run(["--help"])
    assert code == 0
    section = err.split("Environment\n", 1)[1].splitlines()
    names = [line.split()[0] for line in section if line.startswith("  ")]
    assert "MY_TOOL_FORMAT" in names and all(n.startswith("MY_TOOL_") for n in names)


def test_setting_an_unprefixed_variable_does_not_affect_the_tool() -> None:
    unprefixed = {"FORMAT": "plain", "TREATY_FORMAT": "plain", "MAX_OUTPUT_BYTES": "x"}
    stable = ["list", "--stable-output"]
    assert run(stable, unprefixed) == run(stable)


def test_the_framework_warns_on_a_handler_that_reads_an_unprefixed_variable() -> None:
    app = make_app()

    @app.command("dbg", description="Debug", danger_level="safe", exit_codes=())
    def dbg(args: NoArgs, ctx: Ctx) -> NoArgs:
        ctx.env.get("DEBUG")
        ctx.env.get("MY_TOOL_DEBUG")
        os.environ.get("HOME")
        return args

    report = audit(app, "my-tool", limit=3)
    [rule] = [r for r in report.rules if r.id == "env-prefix"]
    assert [(f.command, f.fix) for f in rule.findings] == [
        ("dbg", "read MY_TOOL_DEBUG instead of DEBUG")
    ]


def test_tool_manifest_names_the_prefixed_env_var_of_each_global_flag() -> None:
    """Partial: the spec's manifest has no ``environment`` key yet (02-D1)"""
    flags = make_app().manifest()["flags"]
    assert "$MY_TOOL_FORMAT" in flags["format"]["description"]  # type: ignore[index]
    assert "$MY_TOOL_MAX_OUTPUT_BYTES" in flags["max-output"]["description"]  # type: ignore[index]


def test_env_i_prefixed_variable_applies_and_the_unprefixed_one_does_not() -> None:
    def version(env: dict[str, str]) -> str:
        argv = [sys.executable, str(CONFIGCTL), "--version"]
        done = subprocess.run(argv, env=process_env(**env), capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        return done.stdout

    assert json.loads(version({"FORMAT": "plain"}))["data"]["name"] == "configctl"
    assert version({"CONFIGCTL_FORMAT": "plain"}) == "1.0.0\n"


# REQ-O-042


def test_tool_format_env_produces_the_same_output_as_the_format_flag() -> None:
    assert run(["list"], {"MY_TOOL_FORMAT": "tsv"}) == run(["list", "--format", "tsv"])


def test_explicit_format_flag_wins_over_the_tool_format_env() -> None:
    code, out, _ = run(["list", "--format", "json"], {"MY_TOOL_FORMAT": "tsv"})
    assert code == 0 and json.loads(out)["data"] == [{"id": "1", "name": "alice"}]


def test_underscore_format_has_no_effect_on_format_selection() -> None:
    stable = ["list", "--stable-output"]
    assert run(stable, {"_FORMAT": "tsv", "FORMAT": "tsv"}) == run(stable)


def test_bogus_tool_format_fails_like_a_bogus_format_flag() -> None:
    by_env = run(["list"], {"MY_TOOL_FORMAT": "bogus"})
    by_flag = run(["list", "--format", "bogus"])
    assert by_env[0] == by_flag[0] == 2
    env_error, flag_error = (json.loads(r[1])["error"] for r in (by_env, by_flag))
    assert env_error["context"].pop("source") == "MY_TOOL_FORMAT"
    same = ("code", "message", "phase", "context")
    assert {k: env_error[k] for k in same} == {k: flag_error[k] for k in same}


def test_help_names_the_exact_env_var_the_tool_honors() -> None:
    _, _, err = run(["list", "--help"])
    assert "$MY_TOOL_FORMAT" in err


# Settings: one fixture app, fresh files per test


def configctl(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    out = io.StringIO()
    code = fixture_config_app.app.run(
        argv, stdout=out, stderr=io.StringIO(), env=env or {}, isatty=False
    )
    return code, json.loads(out.getvalue())


def spawn(argv: list[str], cwd: Path, **env: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(CONFIGCTL), *argv],
        cwd=cwd,
        env=process_env(**env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def configctl_in(cwd: Path, argv: list[str], **env: str) -> tuple[int, dict[str, Any]]:
    """A real process in ``cwd``, which decides the project file"""
    proc = spawn(argv, cwd, **env)
    out, err = proc.communicate(timeout=60)
    assert out, err
    return proc.returncode, json.loads(out)


def user_file(home: Path, text: str) -> Path:
    path = home / "configctl" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# REQ-F-028


def test_every_response_includes_config_sources_as_an_array_of_absolute_paths(
    tmp_path: Path,
) -> None:
    path = user_file(tmp_path, 'region = "eu-west-1"\n')
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    for argv in (["show"], ["nope"], ["show", "--help"], ["--schema"], ["version"]):
        _, envelope = configctl(argv, env)
        assert envelope["meta"]["config_sources"] == [str(path)], argv
        assert Path(envelope["meta"]["config_sources"][0]).is_absolute()
    assert configctl(["show"], env)[1]["data"]["region"] == "eu-west-1"


def test_effective_config_hash_changes_when_any_config_file_is_modified(tmp_path: Path) -> None:
    path = user_file(tmp_path, 'region = "eu-west-1"\n')
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    before = configctl(["show"], env)[1]["meta"]["effective_config_hash"]
    path.write_text('region = "eu-west-2"\n')
    after = configctl(["show"], env)[1]["meta"]["effective_config_hash"]
    assert before != after and len(after) == 12


def test_effective_config_hash_is_stable_when_no_config_has_changed(tmp_path: Path) -> None:
    user_file(tmp_path, "retries = 5\n")
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    hashes = {configctl(["show"], env)[1]["meta"]["effective_config_hash"] for _ in range(3)}
    assert len(hashes) == 1


def test_config_sources_is_empty_not_absent_when_no_config_file_was_loaded() -> None:
    _, envelope = configctl(["show"])
    assert envelope["meta"]["config_sources"] == []
    # Without App(settings=) no file is read: the hash is that of {}
    code, out, _ = run(["list"])
    meta = json.loads(out)["meta"]
    empty = hashlib.sha256(b"{}").hexdigest()[:12]
    assert code == 0 and meta["config_sources"] == [] and meta["effective_config_hash"] == empty


def test_env_beats_the_project_file_which_beats_the_user_file(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".configctl.toml").write_text('region = "project"\n')
    user = user_file(tmp_path, 'region = "user"\nretries = 9\n')
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    code, envelope = configctl_in(project, ["show"], **env)
    assert code == 0 and envelope["data"] == {"region": "project", "retries": 9, "tags": []}
    local = project.resolve() / ".configctl.toml"
    assert envelope["meta"]["config_sources"] == [str(local), str(user)]
    _, envelope = configctl_in(project, ["show"], CONFIGCTL_REGION="env", **env)
    assert envelope["data"]["region"] == "env"


@pytest.mark.parametrize(
    ("text", "key"),
    [("colour = 1\n", "colour"), ('retries = "many"\n', "retries"), ("region = [", None)],
)
def test_an_invalid_config_file_exits_2_with_config_invalid(
    tmp_path: Path, text: str, key: str | None
) -> None:
    path = user_file(tmp_path, text)
    code, envelope = configctl(["show"], {"XDG_CONFIG_HOME": str(tmp_path)})
    error = envelope["error"]
    assert code == 2 and error["code"] == "CONFIG_INVALID" and error["phase"] == "validation"
    assert error["context"]["path"] == str(path) and error["context"].get("key") == key


def test_an_invalid_setting_in_plain_mode_names_the_setting() -> None:
    """key is the setting's name, not a credential: stderr shows it"""
    err = io.StringIO()
    code = fixture_config_app.app.run(
        ["show", "--format", "plain"],
        stdout=io.StringIO(),
        stderr=err,
        env={"CONFIGCTL_RETRIES": "many"},
    )
    assert code == 2 and "  key: retries" in err.getvalue()


def test_an_invalid_env_setting_exits_2_naming_the_variable() -> None:
    code, envelope = configctl(["show"], {"CONFIGCTL_RETRIES": "many"})
    error = envelope["error"]
    assert code == 2 and error["code"] == "CONFIG_INVALID"
    assert error["context"] == {"source": "CONFIGCTL_RETRIES", "key": "retries"}


def test_settings_must_be_a_frozen_dataclass_with_defaults_and_no_framework_names() -> None:
    @dataclass
    class Loose:
        region: str = "x"

    @dataclass(frozen=True)
    class NoDefault:
        region: str

    @dataclass(frozen=True)
    class Framework:
        format: str = "json"

    for bad in (Loose, NoDefault, Framework, dict):
        with pytest.raises(RegistrationError):
            App("x", version="1.0.0", settings=bad)


def test_audit_flags_a_handler_that_parses_a_config_file_by_hand() -> None:
    app = make_app()

    @app.command("region", description="Region", danger_level="safe", exit_codes=())
    def region(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return tomllib.loads(Path(".my-tool.toml").read_text())

    report = audit(app, "my-tool", limit=3)
    [rule] = [r for r in report.rules if r.id == "settings-declared"]
    assert [f.command for f in rule.findings] == ["region"]
    assert "App(settings=Settings)" in rule.findings[0].fix


# REQ-O-015


def test_show_config_format_json_parses_as_json(tmp_path: Path) -> None:
    proc = spawn(["--show-config", "--format", "json"], tmp_path)
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0 and json.loads(out)["ok"] is True


def test_each_key_in_sources_maps_to_the_file_or_env_var_that_provided_it(tmp_path: Path) -> None:
    path = user_file(tmp_path, 'retries = 5\napi_token = "s3cret"\n')
    env = {"XDG_CONFIG_HOME": str(tmp_path), "CONFIGCTL_TAGS": "a,b"}
    code, envelope = configctl(["--show-config"], env)
    data = envelope["data"]
    assert code == 0 and data["sources"] == {
        "region": "default",
        "retries": f"file:{path}",
        "api_token": f"file:{path}",
        "tags": "env:CONFIGCTL_TAGS",
    }
    assert data["effective_config"] == {
        "region": "us-east-1",
        "retries": 5,
        "api_token": "[REDACTED]",
        "tags": ["a", "b"],
    }


def test_precedence_order_is_present_and_lists_all_config_layers_in_order(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    _, envelope = configctl_in(project, ["--show-config"], **env)
    local = project.resolve() / ".configctl.toml"
    user = tmp_path / "configctl" / "config.toml"
    assert envelope["data"]["precedence_order"] == ["env-vars", str(local), str(user), "defaults"]


def test_show_config_reflects_the_actual_resolved_state_including_env_overrides(
    tmp_path: Path,
) -> None:
    user_file(tmp_path, 'region = "file"\n')
    env = {"XDG_CONFIG_HOME": str(tmp_path), "CONFIGCTL_REGION": "env"}
    _, shown = configctl(["--show-config"], env)
    _, ran = configctl(["show"], env)
    assert shown["data"]["effective_config"]["region"] == ran["data"]["region"] == "env"
    assert shown["data"]["sources"]["region"] == "env:CONFIGCTL_REGION"
    assert shown["meta"]["effective_config_hash"] == ran["meta"]["effective_config_hash"]


# REQ-O-016


def test_no_config_causes_no_config_file_to_be_read_regardless_of_what_exists(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".configctl.toml").write_text("not toml [\n")
    user_file(tmp_path, 'region = "user"\n')
    code, envelope = configctl_in(project, ["show", "--no-config"], XDG_CONFIG_HOME=str(tmp_path))
    assert code == 0 and envelope["data"]["region"] == "us-east-1"


def test_environment_variables_still_take_effect_with_no_config() -> None:
    code, envelope = configctl(["show", "--no-config"], {"CONFIGCTL_RETRIES": "7"})
    assert code == 0 and envelope["data"]["retries"] == 7


def test_meta_config_sources_is_an_empty_array_when_no_config_is_passed(tmp_path: Path) -> None:
    user_file(tmp_path, 'region = "user"\n')
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    _, envelope = configctl(["show", "--no-config"], env)
    assert envelope["meta"]["config_sources"] == []
    _, shown = configctl(["--show-config", "--no-config"], env)
    assert shown["data"]["precedence_order"] == ["env-vars", "defaults"]


def test_no_config_is_present_in_every_commands_help() -> None:
    app = fixture_config_app.app
    for path in app.commands:
        err = io.StringIO()
        app.run([*path.parts, "--help"], stdout=io.StringIO(), stderr=err, env={}, isatty=False)
        assert "--no-config" in err.getvalue(), path


# REQ-O-024


def test_config_path_causes_the_command_to_load_config_only_from_that_file(
    tmp_path: Path,
) -> None:
    user_file(tmp_path, 'region = "user"\nretries = 9\n')
    isolated = tmp_path / "isolated.json"
    isolated.write_text('{"region": "isolated"}')
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    code, envelope = configctl(["show", "--config", str(isolated)], env)
    assert code == 0 and envelope["data"] == {"region": "isolated", "retries": 3, "tags": []}
    code, envelope = configctl(["show"], {**env, "CONFIGCTL_CONFIG": str(isolated)})
    assert code == 0 and envelope["data"]["region"] == "isolated"


def test_a_config_path_not_yet_written_reads_as_empty(tmp_path: Path) -> None:
    fresh = tmp_path / "session" / "config.toml"
    code, envelope = configctl(["show", "--config", str(fresh)])
    assert code == 0 and envelope["meta"]["config_sources"] == []
    code, envelope = configctl(["show", "--config", str(tmp_path)])  # a directory
    assert code == 2 and envelope["error"]["code"] == "CONFIG_INVALID"


def test_context_staging_uses_the_staging_context_from_the_loaded_config(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        'region = "base"\ncurrent_context = "prod"\n'
        '[contexts.prod]\nregion = "prod-1"\n[contexts.staging]\nregion = "staging-1"\n'
    )
    base = ["show", "--config", str(config)]
    _, envelope = configctl([*base, "--context", "staging"])
    assert envelope["data"]["region"] == "staging-1" and envelope["meta"]["context"] == "staging"
    _, envelope = configctl(base)
    assert envelope["data"]["region"] == "prod-1" and envelope["meta"]["context"] == "prod"
    _, envelope = configctl(base, {"CONFIGCTL_CONTEXT": "staging"})
    assert envelope["data"]["region"] == "staging-1"
    code, envelope = configctl([*base, "--context", "qa"])
    assert code == 2 and envelope["error"]["code"] == "CONTEXT_UNKNOWN"
    assert envelope["error"]["context"]["available"] == ["prod", "staging"]


def test_two_concurrent_invocations_with_different_config_paths_share_no_mutable_state(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    sessions = [tmp_path / f"session-{n}" for n in (1, 2)]
    for session in sessions:
        session.mkdir()
    procs = [
        # The audit log is the one file every run shares by design (REQ-F-026)
        spawn(
            ["config", "set", f"r{n}", "--config", str(s / "config.toml")],
            s,
            HOME=str(home),
            CONFIGCTL_AUDIT_LOG="off",
        )
        for n, s in enumerate(sessions)
    ]
    assert [p.wait(timeout=60) for p in procs] == [0, 0]
    for n, session in enumerate(sessions):
        code, envelope = configctl(["show", "--config", str(session / "config.toml")])
        assert envelope["data"]["region"] == f"r{n}"
    assert not home.exists()  # nothing shared was written


def test_meta_config_sources_reflects_the_config_path_when_passed(tmp_path: Path) -> None:
    config = tmp_path / "agent.toml"
    config.write_text("")
    _, envelope = configctl(["show", "--config", str(config)])
    assert envelope["meta"]["config_sources"] == [str(config)]


# REQ-O-036


def set_region(region: str, home: Path, *flags: str, **env: str) -> dict[str, Any]:
    code, envelope = configctl(
        ["config", "set", region, "--global", *flags], {"XDG_CONFIG_HOME": str(home), **env}
    )
    assert code == 0, envelope
    return envelope


def test_instance_id_agent_1_config_set_writes_to_its_instance_path(tmp_path: Path) -> None:
    envelope = set_region("us-east-1", tmp_path, "--instance-id", "agent-1")
    expected = tmp_path / "configctl" / "instances" / "agent-1" / "config.toml"
    assert (
        envelope["data"]["path"] == str(expected) and envelope["meta"]["instance_id"] == "agent-1"
    )
    assert expected.read_text() == 'region = "us-east-1"\n'


def test_instance_id_agent_2_writes_a_different_path_and_does_not_affect_agent_1(
    tmp_path: Path,
) -> None:
    set_region("us-east-1", tmp_path, "--instance-id", "agent-1")
    set_region("eu-west-1", tmp_path, "--instance-id", "agent-2")
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    for agent, region in (("agent-1", "us-east-1"), ("agent-2", "eu-west-1")):
        _, envelope = configctl(["show", "--instance-id", agent], env)
        assert envelope["data"]["region"] == region


def test_without_instance_id_concurrent_config_writes_use_file_locking_and_succeed(
    tmp_path: Path,
) -> None:
    procs = [
        spawn(["config", "set", f"r{n}", "--global"], tmp_path, XDG_CONFIG_HOME=str(tmp_path))
        for n in range(6)
    ]
    assert [p.wait(timeout=60) for p in procs] == [0] * 6
    written = (tmp_path / "configctl" / "config.toml").read_text()
    assert written in {f'region = "r{n}"\n' for n in range(6)}


def test_tool_instance_id_env_is_equivalent_to_the_instance_id_flag(tmp_path: Path) -> None:
    by_env = set_region("x", tmp_path, CONFIGCTL_INSTANCE_ID="agent-3")
    by_flag = set_region("x", tmp_path, "--instance-id", "agent-3")
    assert by_env["data"] == by_flag["data"]
    assert by_env["meta"]["instance_id"] == by_flag["meta"]["instance_id"] == "agent-3"


def test_instance_id_namespaces_the_state_dir(tmp_path: Path) -> None:
    base = state_dir("configctl", tmp_path, {})
    assert state_dir("configctl", tmp_path, {}, InstanceId("a.1")) == tmp_path / "instances" / "a.1"
    assert base == tmp_path


def test_a_malformed_instance_id_exits_2() -> None:
    code, envelope = configctl(["show", "--instance-id", "../up"])
    assert code == 2 and envelope["error"]["context"]["flag"] == "instance-id"


# REQ-F-076


class MarkerInit:
    """Setup that writes one marker file; ``fail`` makes ``run`` raise it instead"""

    def __init__(self, marker: Path, fail: OSError | None = None) -> None:
        self.marker = marker
        self.fail = fail
        self.runs = 0

    def initialized(self, ctx: Ctx) -> bool:
        return self.marker.exists()

    def run(self, ctx: Ctx) -> None:
        self.runs += 1
        if self.fail is not None:
            raise self.fail
        self.marker.write_text("ok")


def init_app(setup: Init) -> App:
    app = App("keyctl", version="1.0.0", init=setup)

    @app.command("sign", description="Sign", danger_level="safe", exit_codes=())
    def sign(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"signed": "yes"}

    return app


def keyctl(app: App, argv: list[str]) -> tuple[int, dict[str, Any]]:
    out = io.StringIO()
    code = app.run([*argv, "--stable-output"], stdout=out, stderr=io.StringIO(), env={})
    return code, json.loads(out.getvalue())


def test_first_invocation_of_a_non_init_command_is_identical_to_the_hundredth(
    tmp_path: Path,
) -> None:
    home = {
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "CONFIGCTL_AUDIT_LOG": "off",  # the audit log is written on every run by design
    }
    runs = [configctl(["show", "--stable-output"], home) for _ in range(3)]
    assert runs[0] == runs[1] == runs[2] and runs[0][0] == 0
    assert list(tmp_path.iterdir()) == []  # reading config created nothing
    app = init_app(MarkerInit(tmp_path / "marker"))
    assert keyctl(app, ["sign"]) == keyctl(app, ["sign"])


def test_initialization_that_requires_network_access_is_gated_behind_init(
    tmp_path: Path,
) -> None:
    setup = MarkerInit(tmp_path / "marker")
    app = init_app(setup)
    keyctl(app, ["sign"])
    assert setup.runs == 0  # only the init command runs setup
    assert keyctl(app, ["init"])[0] == 0 and setup.runs == 1


def test_tool_init_is_idempotent_and_exits_0_with_already_initialized(tmp_path: Path) -> None:
    setup = MarkerInit(tmp_path / "marker")
    app = init_app(setup)
    code, first = keyctl(app, ["init"])
    assert code == 0 and first["data"]["already_initialized"] is False
    code, again = keyctl(app, ["init"])
    assert code == 0 and again["data"] == {
        "effect": "noop",
        "initialized": True,
        "already_initialized": True,
    }
    assert setup.runs == 1 and keyctl(app, ["sign"])[0] == 0


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (PermissionError(13, "Permission denied"), "permissions"),
        (ConnectionError("refused"), "network"),
        (OSError(28, "No space left on device"), "disk"),
    ],
)
def test_a_failed_tool_init_exits_non_zero_with_the_specific_failure(
    tmp_path: Path, error: OSError, reason: str
) -> None:
    code, envelope = keyctl(init_app(MarkerInit(tmp_path / "marker", error)), ["init"])
    assert code == 1 and envelope["error"]["code"] == "INIT_FAILED"
    assert envelope["error"]["context"]["reason"] == reason


def test_first_invocation_in_a_clean_environment_exits_init_required_pointing_to_init(
    tmp_path: Path,
) -> None:
    app = init_app(MarkerInit(tmp_path / "marker"))
    code, envelope = keyctl(app, ["sign"])
    error = envelope["error"]
    assert code == 4 and error["code"] == "INIT_REQUIRED"
    assert error["fix_command"] == "keyctl init"
    assert keyctl(app, ["version"])[0] == 0  # built-ins never need init


def test_audit_flags_first_run_setup_inside_a_command(tmp_path: Path) -> None:
    app = make_app()

    @app.command("store", description="Store", danger_level="mutating", exit_codes=())
    def store(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        home = Path("~/.my-tool").expanduser()
        if not home.exists():
            home.mkdir()
        return {"effect": "noop"}

    report = audit(app, "my-tool", limit=3)
    [rule] = [r for r in report.rules if r.id == "init-isolated"]
    assert [f.command for f in rule.findings] == ["store"] and "App(init=" in rule.findings[0].fix


# REQ-F-068 with the config layer


def test_help_on_a_command_that_would_otherwise_fail_must_succeed_with_exit_0(
    tmp_path: Path,
) -> None:
    user_file(tmp_path, "region = [\n")
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    app = fixture_config_app.app
    for argv in (["show", "--help"], ["--help"], []):
        err = io.StringIO()
        code = app.run(argv, stdout=io.StringIO(), stderr=err, env=env, isatty=False)
        assert code == 0 and err.getvalue(), argv
    for argv in (["--version"], ["version"], ["manifest"], ["--schema"], ["show", "--schema"]):
        code, envelope = configctl(argv, env)
        assert code == 0 and envelope["ok"] is True, argv
    assert app.call("version", {}, env=env).ok and app.call("manifest", {}, env=env).ok
    # What reads the settings still fails, and says why
    for argv in (["show"], ["--show-config"]):
        code, envelope = configctl(argv, env)
        assert code == 2 and envelope["error"]["code"] == "CONFIG_INVALID", argv
    assert app.call("show", {}, env=env).error.code == "CONFIG_INVALID"


@dataclass(frozen=True, slots=True)
class Checked:
    port: int = 80

    def __post_init__(self) -> None:
        if self.port < 0:
            raise ValueError("port must be 0 or more")


def test_a_settings_post_init_that_refuses_a_value_exits_2_and_spares_help() -> None:
    app = App("my-tool", version="1.0.0", description="Rows", settings=Checked)

    @app.command("port", description="Show the port", danger_level="safe", exit_codes=())
    def port(args: NoArgs, ctx: Ctx, settings: Checked) -> dict[str, int]:
        return {"port": settings.port}

    env = {"MY_TOOL_PORT": "-1"}
    for argv in (["--help"], ["--version"], []):
        assert run(argv, env, app=app)[0] == 0, argv
    code, out, _ = run(["port"], env, app=app)
    error = json.loads(out)["error"]
    assert code == 2 and error["code"] == "CONFIG_INVALID"
    assert "port must be 0 or more" in error["message"]
    assert app.call("port", {}, env=env).exit_code == 2


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("deep.json", '{"region": ' + "[" * 100_000 + "]" * 100_000 + "}"),
        ("deep.toml", "region = " + "[" * 100_000 + "]" * 100_000 + "\n"),
        ("big.json", '{"retries": ' + "9" * 5000 + "}"),
    ],
    # Short ids: pytest puts the id in PYTEST_CURRENT_TEST, capped at 32767 chars on Windows
    ids=["deep.json", "deep.toml", "big.json"],
)
def test_a_config_file_too_deep_or_too_big_to_decode_is_config_invalid(
    tmp_path: Path, name: str, text: str
) -> None:
    path = tmp_path / name
    path.write_text(text)
    app = fixture_config_app.app
    code, out, _ = run(["show", "--config", str(path)], app=app)
    assert code == 2 and json.loads(out)["error"]["code"] == "CONFIG_INVALID"
    assert run(["--help", "--config", str(path)], app=app)[0] == 0


@dataclass(frozen=True, slots=True)
class Keyed:
    api_token: str = ""
    api_keys: tuple[str, ...] = ()


def test_a_secret_setting_is_redacted_from_logs_errors_and_tracebacks() -> None:
    app = App("my-tool", version="1.0.0", description="Rows", settings=Keyed)

    @app.command("call", description="Call", danger_level="safe", exit_codes=())
    def call(args: NoArgs, ctx: Ctx, settings: Keyed) -> dict[str, int]:
        ctx.log(f"calling with {settings.api_token} and {settings.api_keys[1]}")
        raise RuntimeError(f"auth failed for {settings.api_token}")

    env = {"MY_TOOL_API_TOKEN": "sk-live-abcdef", "MY_TOOL_API_KEYS": "kx-one-1234,kx-two-5678"}
    code, out, err = run(["call"], env, app=app)
    assert code == 1
    for secret in ("sk-live-abcdef", "kx-two-5678"):
        assert secret not in out and secret not in err
    assert "[REDACTED]" in err


def test_a_setting_declared_secret_is_redacted_and_a_path_setting_follows_cwd(
    tmp_path: Path,
) -> None:
    """Flag(secret=True) on a settings field marks a DSN that no secret word names"""
    from dataclasses import dataclass

    from treaty import App, Flag

    @dataclass(frozen=True)
    class Settings:
        database: str = Flag(default="", description="DSN", secret=True)
        migrations: Path = Path("migrations")

    app = App("dbx", version="1.0.0", settings=Settings)

    @app.command("where", description="Where", danger_level="safe", exit_codes=())
    def where(args: NoArgs, ctx: Ctx, settings: Settings) -> dict[str, str]:
        return {"migrations": str(settings.migrations)}

    env = {"DBX_DATABASE": "sqlite://admin:s3cret@db", "DBX_AUDIT_LOG": "off"}
    out = io.StringIO()
    app.run(
        ["where", "--show-config", "--format", "json"], stdout=out, stderr=io.StringIO(), env=env
    )
    assert "s3cret" not in out.getvalue()
    project = tmp_path / "proj"
    project.mkdir()
    out = io.StringIO()
    app.run(
        ["where", "--cwd", str(project), "--format", "json"],
        stdout=out,
        stderr=io.StringIO(),
        env=env,
    )
    assert json.loads(out.getvalue())["data"] == {"migrations": str(project / "migrations")}


def test_settings_resolve_path_tuples_and_refuse_flag_options_they_ignore(tmp_path: Path) -> None:
    from dataclasses import dataclass

    from treaty import App, Flag, RegistrationError

    @dataclass(frozen=True)
    class Paths:
        extra: tuple[Path, ...] = ()

    app = App("tpx", version="1.0.0", settings=Paths)

    @app.command("where", description="Where", danger_level="safe", exit_codes=())
    def where(args: NoArgs, ctx: Ctx, settings: Paths) -> dict[str, list[str]]:
        return {"extra": [str(p) for p in settings.extra]}

    out = io.StringIO()
    env = {"TPX_EXTRA": "e1,e2", "TPX_AUDIT_LOG": "off"}
    app.run(["where", "--cwd", str(tmp_path)], stdout=out, stderr=io.StringIO(), env=env)
    assert json.loads(out.getvalue())["data"] == {
        "extra": [str(tmp_path / "e1"), str(tmp_path / "e2")]
    }

    @dataclass(frozen=True)
    class Checked:
        region: str = Flag(default="eu", description="Region", pattern=r"[a-z]{2}")

    with pytest.raises(RegistrationError, match="not pattern"):
        App("chk", version="1.0.0", settings=Checked)

    @dataclass(frozen=True)
    class Flagged:
        verbose: bool = Flag(default=False, description="Verbose", secret=True)

    # A field's type is inspected once the app is in use, after app.scalar
    flagged = App("flg", version="1.0.0", settings=Flagged)
    with pytest.raises(RegistrationError, match="boolean cannot hold a secret"):
        flagged.manifest()


# A settings field of a class app.scalar registers after App(settings=) (#29)


@dataclass(frozen=True, slots=True)
class QueryId:
    value: str


@dataclass(frozen=True, slots=True)
class QuerySettings:
    query_id: QueryId | None = None
    fallbacks: tuple[QueryId, ...] = ()


def query_app() -> App:
    app = App("qctl", version="1.0.0", settings=QuerySettings)
    app.scalar(QueryId, parse=QueryId, pattern=r"\d+")

    @app.command("show", description="Show the query", danger_level="safe", exit_codes=())
    def show(args: NoArgs, ctx: Ctx, settings: QuerySettings) -> dict[str, object]:
        assert settings.query_id is None or isinstance(settings.query_id, QueryId)
        return {
            "query_id": None if settings.query_id is None else settings.query_id.value,
            "fallbacks": [q.value for q in settings.fallbacks],
        }

    return app


def run_query(argv: list[str], **env: str) -> tuple[int, dict[str, Any]]:
    out = io.StringIO()
    code = query_app().run(
        argv, stdout=out, stderr=io.StringIO(), env={"QCTL_AUDIT_LOG": "off", **env}
    )
    return code, json.loads(out.getvalue())


def test_a_scalar_setting_is_parsed_from_the_environment() -> None:
    code, envelope = run_query(["show", "--no-config"], QCTL_QUERY_ID="42", QCTL_FALLBACKS="7,8")
    assert code == 0, envelope
    assert envelope["data"] == {"query_id": "42", "fallbacks": ["7", "8"]}


def test_a_scalar_setting_is_parsed_from_a_config_file(tmp_path: Path) -> None:
    config = tmp_path / "qctl.toml"
    config.write_text('query_id = "42"\nfallbacks = ["7"]\n', encoding="utf-8")
    code, envelope = run_query(["show", "--config", str(config)])
    assert code == 0, envelope
    assert envelope["data"] == {"query_id": "42", "fallbacks": ["7"]}
    code, envelope = run_query(["show", "--config", str(config), "--show-config"])
    assert envelope["data"]["effective_config"] == {"query_id": "42", "fallbacks": ["7"]}
    assert envelope["data"]["sources"]["query_id"] == f"file:{config}"


def test_a_bad_scalar_setting_is_config_invalid_naming_the_key_and_source(
    tmp_path: Path,
) -> None:
    code, envelope = run_query(["show", "--no-config"], QCTL_QUERY_ID="abc")
    error = envelope["error"]
    assert code == 2 and error["code"] == "CONFIG_INVALID", error
    assert error["context"] == {"source": "QCTL_QUERY_ID", "key": "query_id"}
    assert "does not match pattern" in error["message"]
    config = tmp_path / "qctl.toml"
    config.write_text('fallbacks = ["7", "x"]\n', encoding="utf-8")
    code, envelope = run_query(["show", "--config", str(config)])
    error = envelope["error"]
    assert code == 2 and error["code"] == "CONFIG_INVALID", error
    assert error["context"] == {"path": str(config), "key": "fallbacks"}
    assert "does not match pattern" in error["message"]


def test_settings_field_types_are_inspected_on_first_use_after_app_scalar() -> None:
    # Construction checks the shape only: the scalar is not registered yet
    app = App("qctl", version="1.0.0", settings=QuerySettings)
    # Used before app.scalar, the app names the field and the fix
    with pytest.raises(RegistrationError, match="field 'query_id'.*app.scalar"):
        app.manifest()
    with pytest.raises(RegistrationError, match="field 'query_id'"):
        app.run(["show"], stdout=io.StringIO(), stderr=io.StringIO(), env={})
    # Registered later, the settings are inspected again on next use
    app.scalar(QueryId, parse=QueryId, pattern=r"\d+")
    assert app.settings is not None
    assert [s.name for s in app.settings.fields] == ["query_id", "fallbacks"]
    app.manifest()


def test_an_audit_reports_a_settings_field_of_an_unregistered_class(tmp_path: Path) -> None:
    (tmp_path / "unregctl.py").write_text(
        "from dataclasses import dataclass\n"
        "from treaty import App\n"
        "class QueryId(str): ...\n"
        "@dataclass(frozen=True)\n"
        "class Settings:\n"
        "    query_id: QueryId | None = None\n"
        "app = App('unregctl', version='1.0.0', settings=Settings)\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, "-c", "from treaty._cli import main; main()", "audit", "unregctl:app"],
        cwd=tmp_path,
        env=process_env(TREATY_AUDIT_LOG="off"),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 4, proc.stdout + proc.stderr
    error = json.loads(proc.stdout)["error"]
    assert error["code"] == "APP_IMPORT_FAILED"
    assert error["context"]["exception"] == "RegistrationError"
    assert "query_id" in error["message"]
