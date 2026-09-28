import importlib
import io
import json
import os
import shlex
import sys
from pathlib import Path

import fixture_audit_app
import pytest
from conftest import SPEC_DIR

from treaty._cli import cli, resolve_spec_dir
from treaty._errors import CliExit
from treaty._profile import SPEC_FALLBACK, build_profile, default_command, has_kit, probes_for


def run_cli(
    argv: list[str], *, cwd: Path | None = None, isatty: bool = False
) -> tuple[int, dict | str]:
    """``cwd`` is the run's --cwd: this process's working directory never changes"""
    argv = argv if cwd is None else [*argv, "--cwd", str(cwd)]
    out = io.StringIO()
    # CI and TREATY_FORMAT force JSON mode, which would override isatty=True
    env = {k: v for k, v in os.environ.items() if k not in ("CI", "TREATY_FORMAT")}
    code = cli.run(argv, stdout=out, stderr=io.StringIO(), env=env, isatty=isatty)
    text = out.getvalue()
    return code, (text if isatty else json.loads(text))


def test_init_dry_run_writes_nothing(tmp_path: Path) -> None:
    code, env = run_cli(cwd=tmp_path, argv=["init", "shop-tool", "--dry-run"])
    assert code == 0 and env["data"]["written"] is False
    assert "src/shop_tool/cli.py" in env["data"]["files"]
    assert not (tmp_path / "shop-tool").exists()


def test_init_scaffolds_a_project_that_passes_audit(tmp_path: Path) -> None:
    code, env = run_cli(cwd=tmp_path, argv=["init", "shop-tool"])
    assert code == 0 and env["data"]["written"] is True
    project = tmp_path / "shop-tool"
    assert (project / "pyproject.toml").is_file()
    assert os.access(project / "conformance" / "shop-tool", os.X_OK)
    sys.path.insert(0, str(project / "src"))
    try:
        module = importlib.import_module("shop_tool.cli")
    finally:
        sys.path.remove(str(project / "src"))
    out = io.StringIO()
    assert (
        module.app.run(["create", "taken"], stdout=out, stderr=io.StringIO(), env={}, isatty=False)
        == 6
    )
    # Equal to what `treaty conformance` writes, so the README's next step leaves it alone
    want = build_profile(module.app, ["./shop-tool"], probes_for(module.app), beside_profile=True)
    assert json.loads((project / "conformance" / "shop-tool.json").read_text()) == want
    code, env = run_cli(cwd=project, argv=["audit", "shop_tool.cli:app", "--all"])
    assert code == 0 and env["data"]["failed"] == 0, env["data"]["next_steps"]


def test_init_refuses_non_empty_directory(tmp_path: Path) -> None:
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "x").write_text("")
    code, env = run_cli(cwd=tmp_path, argv=["init", "taken"])
    assert code == 6 and env["error"]["code"] == "CONFLICT"


def test_init_rejects_bad_name(tmp_path: Path) -> None:
    code, env = run_cli(cwd=tmp_path, argv=["init", "Bad_Name"])
    assert code == 2 and env["error"]["context"]["name"] == "Bad_Name"


def test_probes_derive_from_examples_and_danger_levels() -> None:
    from examples.deployctl import app

    probes = {p.name: p for p in probes_for(app)}
    rollback = probes["deploy rollback"]
    assert rollback.kind == "destructive" and rollback.dry_run_flag == "--dry-run"
    assert rollback.argv == ("deploy", "rollback", "api", "--to", "1.3.9")
    assert probes["version"].kind == "read"
    assert (
        probes["unknown flag"].kind == "invalid"
        and probes["unknown flag"].argv[-1] == "--no-such-flag"
    )
    fixture = {p.name: p for p in probes_for(fixture_audit_app.app)}
    assert "create-item" not in fixture  # mutating without example: skipped
    assert "delete-item" not in fixture  # safe but has a required positional and no example


def test_conformance_writes_profile_without_running(tmp_path: Path) -> None:
    code, env = run_cli(cwd=tmp_path, argv=["conformance", "examples.deployctl:app"])
    assert code == 0 and env["data"]["ran"] is False
    profile = json.loads((tmp_path / "conformance" / "deployctl.json").read_text())
    assert profile["command"] == ["deployctl"] and len(profile["probes"]) == env["data"]["probes"]
    code, text = run_cli(cwd=tmp_path, argv=["conformance", "examples.deployctl:app"], isatty=True)
    assert "add --run" in text


def test_conformance_leaves_an_equal_profile_alone(tmp_path: Path) -> None:
    code, env = run_cli(cwd=tmp_path, argv=["conformance", "examples.deployctl:app"])
    assert code == 0 and env["data"]["effect"] == "created"
    path = tmp_path / "conformance" / "deployctl.json"
    compact = json.dumps(json.loads(path.read_text()))  # same JSON, different formatting
    path.write_text(compact)
    code, env = run_cli(cwd=tmp_path, argv=["conformance", "examples.deployctl:app"])
    assert code == 0 and env["data"]["effect"] == "noop"
    assert path.read_text() == compact


def test_conformance_refreshes_a_profile_that_differs_only_in_command(
    tmp_path: Path,
) -> None:
    """The command is machine-derived (a console script on Windows), not hand-written"""
    run_cli(cwd=tmp_path, argv=["conformance", "examples.deployctl:app"])
    launcher = str(tmp_path / "dctl")  # absolute on every OS, so kept as given
    code, env = run_cli(
        cwd=tmp_path, argv=["conformance", "examples.deployctl:app", "--command", launcher]
    )
    assert code == 0 and env["data"]["effect"] == "updated"
    profile = json.loads((tmp_path / "conformance" / "deployctl.json").read_text())
    assert profile["command"] == [launcher]


def test_conformance_refuses_to_overwrite_a_differing_profile(tmp_path: Path) -> None:
    run_cli(cwd=tmp_path, argv=["conformance", "examples.deployctl:app"])
    path = tmp_path / "conformance" / "deployctl.json"
    profile = json.loads(path.read_text())
    profile["timeout_seconds"] = 5
    profile["probes"].append({"name": "hand written", "argv": ["deploy", "x"], "kind": "invalid"})
    edited = json.dumps(profile, indent=2)
    path.write_text(edited)
    code, env = run_cli(cwd=tmp_path, argv=["conformance", "examples.deployctl:app"])
    assert code == 6 and env["error"]["code"] == "CONFLICT"
    context = env["error"]["context"]
    assert context["changed_keys"] == ["timeout_seconds"]
    assert context["probes_only_in_file"] == ["hand written"]
    assert context["probes_only_generated"] == [] and context["probes_changed"] == []
    assert "--force" in env["error"]["fix_required"]
    assert path.read_text() == edited
    code, env = run_cli(cwd=tmp_path, argv=["conformance", "examples.deployctl:app", "--force"])
    assert code == 0 and env["data"]["effect"] == "updated"
    assert "hand written" not in path.read_text()


def test_conformance_refuses_to_overwrite_invalid_json(tmp_path: Path) -> None:
    (tmp_path / "conformance").mkdir()
    (tmp_path / "conformance" / "deployctl.json").write_text("{not json")
    code, env = run_cli(cwd=tmp_path, argv=["conformance", "examples.deployctl:app"])
    assert code == 6 and env["error"]["context"]["reason"].startswith("not valid JSON")


def test_conformance_runs_kit_against_example(tmp_path: Path) -> None:
    if not (SPEC_DIR / "conformance" / "run.py").is_file():
        pytest.skip("spec checkout not found")
    root = Path(__file__).resolve().parents[1]
    # The interpreter runs the example on every OS; the checked-in launcher is /bin/sh
    code, env = run_cli(
        cwd=tmp_path,
        argv=[
            "conformance",
            "examples.deployctl:app",
            "--run",
            "--command",
            sys.executable,
            "--command",
            str(root / "examples" / "deployctl.py"),
            "--spec-dir",
            str(SPEC_DIR),
        ],
    )
    assert code == 0, env
    assert env["data"]["ran"] is True
    assert env["data"]["levels"] == {"level_1": "pass", "level_2": "pass", "level_3": "pass"}
    assert all(c["status"] == "pass" for c in env["data"]["checks"])


def test_default_command_is_the_launcher_beside_the_profile_on_posix(tmp_path: Path) -> None:
    profile_dir, scripts = tmp_path / "conformance", tmp_path / ".venv" / "bin"
    profile_dir.mkdir()
    assert default_command("shop-tool", profile_dir, scripts, windows=False) == (
        ["shop-tool"],
        False,
    )
    launcher = profile_dir / "shop-tool"
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    assert default_command("shop-tool", profile_dir, scripts, windows=False) == (
        ["./shop-tool"],
        True,
    )


def test_default_command_on_windows_is_the_console_script_not_the_launcher(
    tmp_path: Path,
) -> None:
    profile_dir, scripts = tmp_path / "conformance", tmp_path / ".venv" / "Scripts"
    profile_dir.mkdir()
    scripts.mkdir(parents=True)
    (profile_dir / "shop-tool").write_text("#!/bin/sh\n")  # Windows cannot run it
    assert default_command("shop-tool", profile_dir, scripts, windows=True) == (
        ["shop-tool"],
        False,
    )
    (scripts / "shop-tool.exe").write_bytes(b"MZ")
    assert default_command("shop-tool", profile_dir, scripts, windows=True) == (
        ["../.venv/Scripts/shop-tool.exe"],
        True,
    )
    # Beside the profile it keeps a slash, or the kit would search PATH for it
    assert default_command("shop-tool", scripts, scripts, windows=True) == (
        ["./shop-tool.exe"],
        True,
    )


def test_conformance_without_spec_dir_is_precondition(tmp_path: Path) -> None:
    code, env = run_cli(
        cwd=tmp_path, argv=["conformance", "examples.deployctl:app", "--run", "--spec-dir", "/nope"]
    )
    assert code == 4 and env["error"]["code"] == "PRECONDITION"
    assert env["error"]["context"] == {"source": "--spec-dir", "spec_dir": str(Path("/nope"))}
    assert not (tmp_path / "conformance").exists()  # validated before the profile is written


def test_named_spec_dir_never_falls_back_to_sibling() -> None:
    if not has_kit(SPEC_FALLBACK):
        pytest.skip("sibling spec checkout not found")
    with pytest.raises(CliExit) as flag:
        resolve_spec_dir(Path("/nope"), {})
    assert flag.value.name.value == "PRECONDITION"
    assert flag.value.context == {"source": "--spec-dir", "spec_dir": str(Path("/nope"))}
    with pytest.raises(CliExit) as env_var:
        resolve_spec_dir(None, {"TREATY_SPEC_DIR": "/nope"})
    assert env_var.value.context == {"source": "TREATY_SPEC_DIR", "spec_dir": str(Path("/nope"))}
    assert resolve_spec_dir(None, {}) == SPEC_FALLBACK.resolve()


@pytest.mark.parametrize(
    ("argv", "flag"),
    [
        (["init", "demo", "--directory", "../escape"], "directory"),
        (["conformance", "examples.deployctl:app", "--out", "../../etc/p.json"], "out"),
    ],
)
def test_write_paths_reject_parent_segments(argv: list[str], flag: str, tmp_path: Path) -> None:
    code, env = run_cli([*argv[:-1], str(tmp_path / "sub" / argv[-1])])
    assert code == 2 and env["error"]["code"] == "ARG_ERROR"
    assert env["error"]["phase"] == "validation" and env["error"]["context"]["flag"] == flag
    assert env["error"]["context"]["rejected_pattern"] == "path_traversal"
    prefix = f"pass the absolute path if intended: --{flag} "
    suggestion = env["error"]["suggestion"]
    [path] = shlex.split(suggestion.removeprefix(prefix))
    assert suggestion.startswith(prefix) and Path(path).is_absolute()
    assert not any(tmp_path.rglob("*"))


def test_write_paths_reject_percent_encoding_and_null_bytes(tmp_path: Path) -> None:
    code, env = run_cli(["init", "demo", "--directory", f"{tmp_path}/acme%2Fwidgets"])
    assert code == 2
    assert shlex.split(env["error"]["suggestion"])[-1] == f"{tmp_path}/acme/widgets"
    code, env = run_cli(["init", "demo", "--directory", f"{tmp_path}/a\x00b"])
    assert code == 2 and "null byte" in env["error"]["message"]
    assert not any(tmp_path.rglob("*"))


def test_write_paths_accept_absolute_paths(tmp_path: Path) -> None:
    code, env = run_cli(["init", "demo", "--directory", str(tmp_path / "demo"), "--dry-run"])
    assert code == 0 and env["data"]["directory"] == str(tmp_path / "demo")


def test_scaffold_pins_the_treaty_version_it_was_generated_by() -> None:
    from treaty import __version__
    from treaty._scaffold import ProjectName, render

    pyproject = render(ProjectName("demo"))["pyproject.toml"]
    assert f'dependencies = ["treaty>={__version__}"]' in pyproject
