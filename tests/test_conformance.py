import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import SPEC_DIR

KIT = SPEC_DIR / "conformance" / "run.py"
PROFILE = Path(__file__).resolve().parents[1] / "conformance" / "deployctl.json"
VENV_PYTHON = Path(sys.executable)


def test_example_cli_passes_conformance_kit() -> None:
    if not KIT.is_file():
        pytest.skip(f"conformance kit not found at {KIT}; set TREATY_SPEC_DIR")
    if not VENV_PYTHON.is_file():
        pytest.skip("launcher needs the project venv")
    result = subprocess.run(
        ["uv", "run", "--project", str(SPEC_DIR), str(KIT), str(PROFILE)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)["data"]
    assert report["levels"] == {"level_1": "pass", "level_2": "pass", "level_3": "pass"}
    assert report["summary"]["failed"] == 0 and report["summary"]["skipped"] == 0
