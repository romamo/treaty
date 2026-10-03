"""The project config file in a project directory the app names (#303):
``App(config_root_flag=, config_root_env=)`` moves ``.<app>.toml`` from the cwd to the
directory a command's ``--project`` argument, else a variable, names"""

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from treaty import App, Arg, Ctx, Flag, NoArgs, RegistrationError


@dataclass(frozen=True, slots=True)
class Settings:
    region: str = "default"
    schemas: Path = Path("schemas")


@dataclass(frozen=True, slots=True)
class ProjectArgs:
    project: Path | None = Flag(default=None, description="The project directory")


@dataclass(frozen=True, slots=True)
class Written:
    effect: str
    path: str


@dataclass(frozen=True, slots=True)
class Seen:
    region: str
    schemas: str
    project: str | None


def make_app(**root: str) -> App:
    app = App("fleet", version="1.0.0", settings=Settings, **root)  # type: ignore[arg-type]

    @app.command("audit", description="Audit a project", danger_level="safe", exit_codes=())
    def audit(args: ProjectArgs, ctx: Ctx, settings: Settings) -> Seen:
        project = None if args.project is None else str(args.project)
        return Seen(settings.region, str(settings.schemas), project)

    @app.command("plain", description="Take no project", danger_level="safe", exit_codes=())
    def plain(args: NoArgs, ctx: Ctx, settings: Settings) -> Seen:
        return Seen(settings.region, str(settings.schemas), None)

    @app.command(
        "config.set",
        description="Set the region",
        danger_level="mutating",
        exit_codes=(),
        config_write_scope="local",
    )
    def config_set(args: ProjectArgs, ctx: Ctx) -> Written:
        return Written("updated", str(ctx.write_config('region = "set"\n')))

    return app


def run(app: App, argv: list[str], env: dict[str, str] | None = None) -> tuple[int, Any]:
    out = io.StringIO()
    code = app.run(argv, stdout=out, stderr=io.StringIO(), env=env or {}, isatty=False)
    return code, json.loads(out.getvalue().splitlines()[0])


def project(where: Path, region: str) -> Path:
    where.mkdir(parents=True, exist_ok=True)
    (where / ".fleet.toml").write_text(f'region = "{region}"\n', encoding="utf-8")
    return where


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path]:
    """A working directory with its own project file, and a project elsewhere"""
    return project(tmp_path / "cwd", "cwd"), project(tmp_path / "fleet", "fleet")


ROOT = {"config_root_flag": "project", "config_root_env": "FLEET_PROJECT"}


def test_without_the_declaration_the_project_file_is_the_cwds(tree: tuple[Path, Path]) -> None:
    cwd, fleet = tree
    code, body = run(make_app(), ["--cwd", str(cwd), "audit", "--project", str(fleet)])
    assert code == 0, body
    assert body["data"]["region"] == "cwd"
    assert body["meta"]["config_sources"] == [str(cwd / ".fleet.toml")]


def test_the_command_flag_names_the_project_file(tree: tuple[Path, Path]) -> None:
    cwd, fleet = tree
    code, body = run(make_app(**ROOT), ["--cwd", str(cwd), "audit", "--project", str(fleet)])
    assert code == 0, body
    assert body["data"]["region"] == "fleet"
    assert body["meta"]["config_sources"] == [str(fleet / ".fleet.toml")]


def test_a_relative_project_resolves_against_the_cwd(tmp_path: Path) -> None:
    fleet = project(tmp_path / "fleet", "fleet")
    code, body = run(make_app(**ROOT), ["--cwd", str(tmp_path), "audit", "--project", "fleet"])
    assert code == 0, body
    assert body["meta"]["config_sources"] == [str(fleet / ".fleet.toml")]


def test_a_relative_path_setting_resolves_against_the_project(tree: tuple[Path, Path]) -> None:
    cwd, fleet = tree
    (fleet / ".fleet.toml").write_text('schemas = "schemas"\n', encoding="utf-8")
    code, body = run(make_app(**ROOT), ["--cwd", str(cwd), "audit", "--project", str(fleet)])
    assert code == 0 and body["data"]["schemas"] == str(fleet / "schemas")
    # A value from the environment still means the run's directory
    env = {"FLEET_SCHEMAS": "env-schemas"}
    argv = ["--cwd", str(cwd), "audit", "--project", str(fleet)]
    assert run(make_app(**ROOT), argv, env)[1]["data"]["schemas"] == str(cwd / "env-schemas")


def test_the_variable_applies_without_the_flag_and_the_flag_wins(
    tree: tuple[Path, Path], tmp_path: Path
) -> None:
    cwd, fleet = tree
    other = project(tmp_path / "other", "other")
    env = {"FLEET_PROJECT": str(other)}
    for command in ("audit", "plain"):
        code, body = run(make_app(**ROOT), ["--cwd", str(cwd), command], env)
        assert code == 0 and body["data"]["region"] == "other", body
    argv = ["--cwd", str(cwd), "audit", "--project", str(fleet)]
    assert run(make_app(**ROOT), argv, env)[1]["data"]["region"] == "fleet"


def test_a_command_without_the_argument_reads_the_cwds_file(tree: tuple[Path, Path]) -> None:
    cwd, _ = tree
    code, body = run(make_app(**ROOT), ["--cwd", str(cwd), "plain"])
    assert code == 0 and body["data"]["region"] == "cwd"


def test_a_project_that_does_not_exist_exits_2(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    code, body = run(make_app(**ROOT), ["audit", "--project", str(missing)])
    assert code == 2 and body["error"]["context"]["source"] == "--project"
    assert "does not exist" in body["error"]["message"]
    code, body = run(make_app(**ROOT), ["plain"], {"FLEET_PROJECT": str(missing)})
    assert code == 2 and body["error"]["context"]["source"] == "FLEET_PROJECT"
    afile = tmp_path / "file"
    afile.write_text("", encoding="utf-8")
    code, body = run(make_app(**ROOT), ["audit", "--project", str(afile)])
    assert code == 2 and "is not a directory" in body["error"]["message"]


def test_a_broken_cwd_file_only_fails_the_runs_that_read_it(tree: tuple[Path, Path]) -> None:
    cwd, fleet = tree
    (cwd / ".fleet.toml").write_text("unknown = 1\n", encoding="utf-8")
    argv = ["--cwd", str(cwd), "audit", "--project", str(fleet)]
    code, body = run(make_app(**ROOT), argv)
    assert code == 0 and body["data"]["region"] == "fleet", body
    for argv in (["--cwd", str(cwd), "audit"], ["--cwd", str(cwd), "plain"]):
        code, body = run(make_app(**ROOT), argv)
        assert code == 2 and body["error"]["code"] == "CONFIG_INVALID", body
    envelope = make_app(**ROOT).call("audit", {"project": str(fleet)}, env={})
    assert envelope.ok, envelope


def test_the_arguments_own_variable_names_the_project(tree: tuple[Path, Path]) -> None:
    @dataclass(frozen=True, slots=True)
    class EnvArgs:
        project_dir: Path | None = Flag(
            default=None, env=("FLEET_DIR",), description="The project directory"
        )

    app = App("fleet", version="1.0.0", settings=Settings, config_root_flag="project_dir")

    @app.command("audit", description="Audit", danger_level="safe", exit_codes=())
    def audit(args: EnvArgs, ctx: Ctx, settings: Settings) -> Seen:
        return Seen(settings.region, str(settings.schemas), None)

    cwd, fleet = tree
    code, body = run(app, ["--cwd", str(cwd), "audit"], {"FLEET_DIR": str(fleet)})
    assert code == 0 and body["data"]["region"] == "fleet", body
    code, body = run(app, ["--cwd", str(cwd), "audit", "--project-dir", str(fleet)])
    assert code == 0 and body["data"]["region"] == "fleet", body


def test_config_flag_and_variable_still_win(tree: tuple[Path, Path], tmp_path: Path) -> None:
    cwd, fleet = tree
    chosen = tmp_path / "chosen.toml"
    chosen.write_text('region = "chosen"\n', encoding="utf-8")
    argv = ["--cwd", str(cwd), "audit", "--project", str(fleet)]
    code, body = run(make_app(**ROOT), [*argv, "--config", str(chosen)])
    assert code == 0 and body["data"]["region"] == "chosen"
    code, body = run(make_app(**ROOT), argv, {"FLEET_CONFIG": str(chosen)})
    assert code == 0 and body["data"]["region"] == "chosen"
    assert body["meta"]["config_sources"] == [str(chosen)]


def test_a_context_applies_to_the_relocated_file(tree: tuple[Path, Path]) -> None:
    """The cwd's file has no such context, nor need it: the project's is the one read"""
    cwd, fleet = tree
    (fleet / ".fleet.toml").write_text(
        'region = "fleet"\n[contexts.staging]\nregion = "staging"\n', encoding="utf-8"
    )
    argv = ["--cwd", str(cwd), "audit", "--project", str(fleet)]
    code, body = run(make_app(**ROOT), argv, {"FLEET_CONTEXT": "staging"})
    assert code == 0 and body["data"]["region"] == "staging", body


def test_the_user_file_is_unaffected(tree: tuple[Path, Path], tmp_path: Path) -> None:
    cwd, fleet = tree
    user = tmp_path / "xdg" / "fleet" / "config.toml"
    user.parent.mkdir(parents=True)
    schemas = (tmp_path / "abs").as_posix()
    user.write_text(f'schemas = "{schemas}"\n', encoding="utf-8")
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    argv = ["--cwd", str(cwd), "audit", "--project", str(fleet)]
    code, body = run(make_app(**ROOT), argv, env)
    assert code == 0 and body["data"] == {
        "region": "fleet",
        "schemas": str(tmp_path / "abs"),
        "project": str(fleet),
    }
    assert body["meta"]["config_sources"] == [str(fleet / ".fleet.toml"), str(user)]


def test_show_config_and_status_name_the_variables_file(tree: tuple[Path, Path]) -> None:
    cwd, fleet = tree
    env = {"FLEET_PROJECT": str(fleet)}
    code, body = run(make_app(**ROOT), ["--cwd", str(cwd), "--show-config"], env)
    assert code == 0 and body["data"]["sources"]["region"] == f"file:{fleet / '.fleet.toml'}"
    assert str(fleet / ".fleet.toml") in body["data"]["precedence_order"]
    code, body = run(make_app(**ROOT), ["--cwd", str(cwd), "status", "--show-state-files"], env)
    assert code == 0, body
    [entry] = [f for f in body["data"]["state_files"] if f["purpose"] == "project config"]
    assert entry["path"] == str((fleet / ".fleet.toml").resolve()) and entry["exists"]


def test_a_config_write_goes_to_the_project_file(tree: tuple[Path, Path]) -> None:
    cwd, fleet = tree
    argv = ["--cwd", str(cwd), "config", "set", "--project", str(fleet)]
    code, body = run(make_app(**ROOT), argv)
    assert code == 0 and body["data"]["path"] == str(fleet / ".fleet.toml"), body
    assert (fleet / ".fleet.toml").read_text(encoding="utf-8") == 'region = "set"\n'
    assert (cwd / ".fleet.toml").read_text(encoding="utf-8") == 'region = "cwd"\n'


def test_app_call_and_a_bound_value_read_the_project_file(tree: tuple[Path, Path]) -> None:
    _, fleet = tree
    app = make_app(**ROOT)
    envelope = app.call("audit", {"project": str(fleet)}, env={})
    assert envelope.ok and envelope.data is not None
    assert envelope.data["region"] == "fleet" and envelope.data["project"] == str(fleet)
    # The value McpServe(bind=) fixes for the run of mcp serve
    bound = app._call_bound("audit", {}, {"project": str(fleet)}, env={})
    assert bound.ok and bound.extra_meta["config_sources"] == [str(fleet / ".fleet.toml")]


def test_each_exec_line_reads_its_own_project(tree: tuple[Path, Path], tmp_path: Path) -> None:
    cwd, fleet = tree
    other = project(tmp_path / "other", "other")
    lines = [
        {"_cmd": "audit", "project": str(fleet)},
        {"_cmd": "plain"},
        {"_cmd": "audit", "project": str(other)},
        {"_cmd": "audit"},
        {"_cmd": "audit", "project": str(tmp_path / "missing")},
    ]
    stdin = io.StringIO("".join(json.dumps(line) + "\n" for line in lines))
    out = io.StringIO()
    app = make_app(**ROOT)
    app.run(["--cwd", str(cwd), "exec"], stdin=stdin, stdout=out, stderr=io.StringIO(), env={})
    answers = [json.loads(line) for line in out.getvalue().splitlines()]
    regions = [a["data"]["region"] if a["ok"] else a["meta"]["exit_code"] for a in answers]
    assert regions == ["fleet", "cwd", "other", "cwd", 2]


def test_the_variable_is_listed_with_the_apps_environment() -> None:
    app = make_app(**ROOT)
    assert "FLEET_PROJECT" in dict(app.environment())
    names = [e["name"] for e in app.manifest()["env_vars"]]  # type: ignore[union-attr]
    assert "FLEET_PROJECT" in names


@pytest.mark.parametrize(
    ("root", "match"),
    [
        ({"config_root_flag": "--project"}, "a command's Path argument"),
        ({"config_root_flag": "config"}, "treaty's own --config"),
        ({"config_root_env": "fleet-project"}, "environment variable name"),
        ({"config_root_env": "FLEET_CONFIG"}, "a variable treaty reads itself"),
    ],
)
def test_a_malformed_declaration_is_refused(root: dict[str, str], match: str) -> None:
    with pytest.raises(RegistrationError, match=match):
        App("fleet", version="1.0.0", **root)  # type: ignore[arg-type]


def test_the_argument_must_be_a_path_and_the_variable_no_settings_name() -> None:
    @dataclass(frozen=True, slots=True)
    class Named:
        project: str = Arg(description="A project name")

    app = App("fleet", version="1.0.0", config_root_flag="project")

    @app.command("audit", description="Audit", danger_level="safe", exit_codes=())
    def audit(args: Named, ctx: Ctx) -> NoArgs:
        return NoArgs()

    with pytest.raises(RegistrationError, match="so it is a Path"):
        app.call("audit", {"project": "x"}, env={})
    clash = App("fleet", version="1.0.0", settings=Settings, config_root_env="FLEET_REGION")
    with pytest.raises(RegistrationError, match="is read for setting region"):
        clash.call("version", {}, env={})
