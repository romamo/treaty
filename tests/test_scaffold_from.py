"""treaty scaffold-from (#3): a treaty module from a typer, click, or argparse CLI, which
registers, runs, and passes ruff and mypy --strict"""

import argparse
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from treaty import App
from treaty._audit import audit
from treaty._cli import cli
from treaty._scaffold_from import check_module, render_module, scaffold

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
SAMPLES = [
    ("typer", "fixture_scaffold_typer:app"),
    ("click", "fixture_scaffold_click:cli"),
    ("argparse", "fixture_scaffold_argparse:build_parser"),
]


def run(*argv: str, fmt: str = "json") -> tuple[int, dict[str, object] | str]:
    out = io.StringIO()
    code = cli.run(
        ["--cwd", str(TESTS), "--format", fmt, *argv],
        stdout=out,
        stderr=io.StringIO(),
        env={"TREATY_AUDIT_LOG": "0"},
        isatty=False,
    )
    text = out.getvalue()
    return code, (json.loads(text) if fmt == "json" else text)


def data_of(envelope: dict[str, object] | str) -> dict[str, object]:
    assert isinstance(envelope, dict)
    data = envelope["data"]
    assert isinstance(data, dict)
    return data


def load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    where = tmp_path_factory.mktemp("scaffolded")
    paths = {}
    for kind, target in SAMPLES:
        path = where / f"scaffolded_{kind}.py"
        code, envelope = run("scaffold-from", kind, target, "--out", str(path))
        assert code == 0, envelope
        assert data_of(envelope)["effect"] == "created"
        paths[kind] = path
    return paths


def test_generated_modules_pass_ruff_and_mypy(generated: dict[str, Path]) -> None:
    files = [str(p) for p in generated.values()]
    config = str(ROOT / "pyproject.toml")
    ruff = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-cache", "--config", config, *files],
        capture_output=True,
        text=True,
    )
    assert ruff.returncode == 0, ruff.stdout + ruff.stderr
    mypy = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", "--no-incremental", *files],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert mypy.returncode == 0, mypy.stdout + mypy.stderr


def test_typer_tree(generated: dict[str, Path]) -> None:
    app = load(generated["typer"]).app
    assert isinstance(app, App)
    manifest = app.manifest()["commands"]
    assert isinstance(manifest, dict)
    assert {"check", "transaction.list", "transaction.add"} <= set(manifest)
    flags = manifest["transaction.list"]["flags"]
    assert {"ledger", "limit-to", "kind", "tag", "output-format"} <= set(flags)
    assert "format" not in [f for f in flags if flags[f].get("description") == "Output style"]
    envelope = app.call("transaction.list", {"account": "Assets", "kind": "card", "tag": ["a"]})
    assert envelope.ok and envelope.data == {
        "effect": "noop",
        "args": {
            "ledger": str(Path("main.beancount").absolute()),
            "limit_to": 10,
            "kind": "card",
            "tag": ["a"],
            "output_format": "table",
            "account": "Assets",
        },
    }


def test_click_tree(generated: dict[str, Path]) -> None:
    module = load(generated["click"])
    app = module.app
    manifest = app.manifest()["commands"]
    assert {"release.rollback", "status"} <= set(manifest)
    envelope = app.call(
        "release.rollback", {"service": "api", "to": "1.2.0", "region": ["eu", "us"]}
    )
    assert envelope.ok
    args = envelope.data["args"]
    assert args["to"] == "1.2.0" and args["region"] == ["eu", "us"] and args["replicas"] == 2
    assert args["strategy"] == "rolling" and args["config_file"] is None
    missing = app.call("release.rollback", {"service": "api"})
    assert missing.exit_code == 2  # --to was required
    assert app.call("status", {}).data["args"]["services"] == []


def test_argparse_tree(generated: dict[str, Path]) -> None:
    app = load(generated["argparse"]).app
    manifest = app.manifest()["commands"]
    assert {"inventory.show", "restart"} <= set(manifest)
    envelope = app.call("restart", {"services": ["web"], "retries": 2.5, "wait": False})
    assert envelope.ok
    assert envelope.data["args"]["wait"] is False and envelope.data["args"]["mode"] == "soft"
    shown = app.call("inventory.show", {"field_names": "host"})
    assert shown.ok and shown.data["args"]["host"] is None


def test_what_treaty_provides_is_left_out_and_reported() -> None:
    code, envelope = run("scaffold-from", "typer", "fixture_scaffold_typer:app")
    assert code == 0
    data = data_of(envelope)
    assert data["effect"] == "noop" and data["out"] is None
    assert data["commands"] == ["check", "transaction.list", "transaction.add"]
    skipped = {s["item"]: s["reason"] for s in data["skipped"]}  # type: ignore[union-attr]
    assert "version" in skipped and "transaction add --yes" in skipped
    assert "transaction list --verbose" in skipped
    assert data["renamed"] == [
        {"command": "transaction list", "old": "format", "new": "output-format"}
    ]
    _, argparse_envelope = run("scaffold-from", "argparse", "fixture_scaffold_argparse:parser")
    items = [s["item"] for s in data_of(argparse_envelope)["skipped"]]  # type: ignore[union-attr]
    assert "inventory ls" in items and "(root) --quiet" in items


def test_placeholders_are_what_the_audit_reports(generated: dict[str, Path]) -> None:
    app = load(generated["argparse"]).app
    report = audit(app, "scaffolded_argparse:app", limit=3)
    failing = {r.id for r in report.rules if not r.passed}
    assert "exit-codes" in failing
    source = generated["argparse"].read_text()
    assert (
        source.count('    danger_level="mutating",') == 2
        and source.count("    exit_codes=(),") == 2
    )
    assert "TODO" not in source and "NotImplementedError" not in source


def test_plain_prints_the_module_alone() -> None:
    code, text = run("scaffold-from", "click", "fixture_scaffold_click:cli", fmt="plain")
    assert code == 0 and isinstance(text, str)
    assert text.startswith('"""\nScaffolded by treaty scaffold-from')
    compile(text, "scaffolded.py", "exec")


def test_an_existing_file_needs_force(tmp_path: Path) -> None:
    out = tmp_path / "cli.py"
    out.write_text("# mine\n")
    code, envelope = run("scaffold-from", "click", "fixture_scaffold_click:cli", "--out", str(out))
    assert code == 6 and isinstance(envelope, dict)
    assert envelope["error"]["code"] == "CONFLICT"  # type: ignore[index]
    assert out.read_text() == "# mine\n"
    code, envelope = run(
        "scaffold-from", "click", "fixture_scaffold_click:cli", "--out", str(out), "--dry-run"
    )
    assert code == 6  # a dry run still refuses what the real run would
    code, envelope = run(
        "scaffold-from",
        "click",
        "fixture_scaffold_click:cli",
        "--out",
        str(out),
        "--force",
        "--dry-run",
    )
    assert code == 0 and data_of(envelope)["effect"] == "would_update"
    assert out.read_text() == "# mine\n"
    code, envelope = run(
        "scaffold-from", "click", "fixture_scaffold_click:cli", "--out", str(out), "--force"
    )
    assert code == 0 and data_of(envelope)["effect"] == "updated"
    code, envelope = run("scaffold-from", "click", "fixture_scaffold_click:cli", "--out", str(out))
    assert code == 0 and data_of(envelope)["effect"] == "noop"  # the same module again


@pytest.mark.parametrize(
    ("argv", "exit_code", "code"),
    [
        (("typer", "fixture_scaffold_click:cli"), 4, "PRECONDITION"),
        (("argparse", "fixture_scaffold_typer:app"), 4, "PRECONDITION"),
        (("click", "fixture_scaffold_argparse:parser"), 4, "PRECONDITION"),
        (("argparse", "fixture_scaffold_argparse:main"), 4, "PRECONDITION"),
        (("click", "fixture_scaffold_nowhere:cli"), 5, "NOT_FOUND"),
        (("click", "fixture_scaffold_click:nothing"), 5, "NOT_FOUND"),
        (("click", "no-colon"), 2, "ARG_ERROR"),
        (("fire", "fixture_scaffold_click:cli"), 2, "ARG_ERROR"),
    ],
)
def test_wrong_targets_fail_with_their_code(
    argv: tuple[str, str], exit_code: int, code: str
) -> None:
    status, envelope = run("scaffold-from", *argv)
    assert isinstance(envelope, dict)
    assert status == exit_code and envelope["error"]["code"] == code  # type: ignore[index]


def test_a_factory_that_parses_argv_is_refused_with_its_fix() -> None:
    _, envelope = run("scaffold-from", "argparse", "fixture_scaffold_argparse:main")
    error = envelope["error"]  # type: ignore[index]
    assert "parses the command line" in error["message"]
    assert "build_parser" in error["fix_required"]


class _NoActions(argparse.ArgumentParser):
    def __init__(self) -> None:
        super().__init__(prog="odd")
        self._actions = None  # type: ignore[assignment]


def test_argparse_internals_missing_is_a_clear_error() -> None:
    from treaty import CliExit

    with pytest.raises(CliExit) as caught:
        scaffold("argparse", _NoActions(), "odd:parser", None, env={})
    assert caught.value.name.value == "PRECONDITION"
    assert "ArgumentParser._actions" in str(caught.value)


def test_a_module_that_does_not_register_is_refused() -> None:
    from treaty import CliExit

    parser = argparse.ArgumentParser(prog="x")
    parser.add_subparsers().add_parser("run")
    tree = scaffold("argparse", parser, "x:p", None, env={})
    source = render_module(tree).replace('"0.1.0"', '""')
    with pytest.raises(CliExit) as caught:
        check_module(source, "x:p")
    assert caught.value.code == "SCAFFOLD_INVALID"


def test_a_secret_default_is_not_written_into_the_module() -> None:
    import click

    token = "sk-live-0123456789"

    @click.group()
    def cli() -> None:
        pass

    @cli.command()
    @click.option("--token", default=token)  # as default=os.environ["DEPLOY_TOKEN"] reads
    @click.option("--endpoint", default="https://example.test")  # read from $ENDPOINT
    @click.option("--phrase", default="hunter22", hide_input=True)
    @click.option("--region", default="eu-west-1")
    def push(**kwargs: object) -> None:
        pass

    tree = scaffold("click", cli, "secretcli:cli", None, env={"ENDPOINT": "https://example.test"})
    source = render_module(tree)
    assert token not in source and "hunter22" not in source
    assert "https://example.test" not in source and "$ENDPOINT" in source
    assert 'default="eu-west-1"' in source
    check_module(source, "secretcli:cli")


def test_options_that_spell_one_field_get_distinct_names() -> None:
    import click

    @click.group()
    def cli() -> None:
        pass

    @cli.command()
    @click.option("--format")  # renamed --output-format
    @click.option("--output-format")
    @click.option("--a-b")
    @click.option("--a_b", "a_b2")
    def ls(**kwargs: object) -> None:
        pass

    source = render_module(scaffold("click", cli, "dupcli:cli", None, env={}))
    fields = [line.split(":")[0].strip() for line in source.splitlines() if "= Flag(" in line]
    assert len(fields) == len(set(fields)) == 4, fields
    check_module(source, "dupcli:cli")
