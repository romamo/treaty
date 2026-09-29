"""The agent contract, checked on every test run: the strict audit, every example, and the
manifest's shape. Copy it into a project's tests and change APP and the import to your app

The testing chapter copies it into a project made with treaty init, next to test_cli.py.
"""

import io
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from todo.cli import app

APP = "todo.cli:app"
ROOT = Path(__file__).resolve().parents[1]


def treaty(*argv: str) -> subprocess.CompletedProcess[str]:
    """The treaty command of this environment, run from the project's root"""
    found = shutil.which("treaty", path=str(Path(sys.executable).parent))
    assert found is not None, "treaty is not installed in this environment"
    return subprocess.run([found, *argv], cwd=ROOT, capture_output=True, text=True)


def test_the_strict_audit_passes() -> None:
    done = treaty("audit", APP, "--strict", "--format", "plain")
    assert done.returncode == 0, done.stdout + done.stderr


def app_examples() -> list[str]:
    """Every example of the app's own commands; built-ins have their own"""
    commands = app.manifest()["commands"]
    assert isinstance(commands, dict)
    builtins = {path.value for path in app.builtins}
    return [
        example["command"]
        for name, command in commands.items()
        if name not in builtins
        for example in command.get("examples", [])
    ]


@pytest.mark.parametrize("example", app_examples())
def test_an_example_parses(example: str) -> None:
    """A renamed flag or a leftover <placeholder> in an example fails here, not in an agent"""
    argv = shlex.split(example)[1:]
    out = io.StringIO()
    code = app.run([*argv, "--validate-only"], stdout=out, stderr=io.StringIO(), env={})
    assert code == 0, out.getvalue()


def test_every_command_declares_its_output() -> None:
    """An output schema with no properties tells an agent nothing about data"""
    commands = app.manifest()["commands"]
    assert isinstance(commands, dict)
    builtins = {path.value for path in app.builtins}
    untyped = [
        name
        for name, command in commands.items()
        if name not in builtins
        and command["output_schema"].get("type") == "object"
        and "properties" not in command["output_schema"]
    ]
    assert untyped == []


def test_the_manifest_is_json() -> None:
    out = io.StringIO()
    code = app.run(["manifest"], stdout=out, stderr=io.StringIO(), env={})
    assert code == 0 and json.loads(out.getvalue())["ok"] is True
