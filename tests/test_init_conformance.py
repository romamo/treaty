import importlib
import io
import json
import os
import sys
from pathlib import Path

import fixture_audit_app
import pytest
from conftest import SPEC_DIR

from treaty._cli import cli
from treaty._profile import probes_for


def run_cli(argv: list[str], *, isatty: bool = False) -> tuple[int, dict | str]:
    out = io.StringIO()
    code = cli.run(argv, stdout=out, stderr=io.StringIO(), env=dict(os.environ), isatty=isatty)
    text = out.getvalue()
    return code, (text if isatty else json.loads(text))


def test_init_dry_run_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    code, env = run_cli(["init", "shop-tool", "--dry-run"])
    assert code == 0 and env["data"]["written"] is False
    assert "src/shop_tool/cli.py" in env["data"]["files"]
    assert not (tmp_path / "shop-tool").exists()


def test_init_scaffolds_a_project_that_passes_audit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    code, env = run_cli(["init", "shop-tool"])
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
        == 79
    )
    monkeypatch.chdir(project)
    code, env = run_cli(["audit", "shop_tool.cli:app", "--all"])
    assert code == 0 and env["data"]["failed"] == 0, env["data"]["next_steps"]


def test_init_refuses_non_empty_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "x").write_text("")
    code, env = run_cli(["init", "taken"])
    assert code == 6 and env["error"]["code"] == "CONFLICT"


def test_init_rejects_bad_name(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    code, env = run_cli(["init", "Bad_Name"])
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


def test_conformance_writes_profile_without_running(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    code, env = run_cli(["conformance", "examples.deployctl:app"])
    assert code == 0 and env["data"]["ran"] is False
    profile = json.loads((tmp_path / "conformance" / "deployctl.json").read_text())
    assert profile["command"] == ["deployctl"] and len(profile["probes"]) == env["data"]["probes"]
    code, text = run_cli(["conformance", "examples.deployctl:app"], isatty=True)
    assert "add --run" in text


def test_conformance_runs_kit_against_example(tmp_path: Path, monkeypatch) -> None:
    if not (SPEC_DIR / "conformance" / "run.py").is_file():
        pytest.skip("spec checkout not found")
    root = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(root))
    launcher = str(root / "conformance" / "deployctl")
    code, env = run_cli(
        [
            "conformance",
            "examples.deployctl:app",
            "--run",
            "--command",
            launcher,
            "--spec-dir",
            str(SPEC_DIR),
        ]
    )
    assert code == 0, env
    assert env["data"]["ran"] is True
    assert env["data"]["levels"] == {"level_1": "pass", "level_2": "pass", "level_3": "pass"}
    assert all(c["status"] == "pass" for c in env["data"]["checks"])


def test_conformance_without_spec_dir_is_precondition(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    monkeypatch.delenv("TREATY_SPEC_DIR", raising=False)
    code, env = run_cli(["conformance", "examples.deployctl:app", "--run", "--spec-dir", "/nope"])
    assert code == 4 and env["error"]["code"] == "PRECONDITION"
    assert env["data"]["ran"] is False  # profile was still written
