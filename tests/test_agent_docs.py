"""Agent docs: REQ-O-043, REQ-O-044, REQ-O-045, REQ-O-046."""

import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import fixture_config_app
import fixture_prompt_app

from examples.authctl import app as authctl
from examples.deployctl import app as deployctl
from treaty import App
from treaty._agents_md import (
    BEGIN,
    END,
    SECTIONS,
    SHARED,
    check,
    env_vars,
    help_texts,
    render_file,
)
from treaty._cli import cli
from treaty._env import UNPREFIXED

ROOT = Path(__file__).resolve().parents[1]


def run_cli(argv: list[str], cwd: Path, *, plain: bool = False) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.run(
        [*argv, "--cwd", str(cwd), "--format", "plain" if plain else "json"],
        stdout=out,
        stderr=io.StringIO(),
        env={"PATH": os.environ["PATH"]},
    )
    return code, out.getvalue()


def sections(text: str) -> dict[str, str]:
    """``## `` heading to its body"""
    found: dict[str, str] = {}
    current = ""
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:]
            found[current] = ""
        elif current:
            found[current] += line + "\n"
    return found


def doc(app: App) -> str:
    return render_file(app, None, "examples.deployctl:app", app.name)


# REQ-O-043


def test_agents_md_contains_a_canonical_invocation_section_with_the_exact_command_string() -> None:
    body = sections(doc(deployctl))["Canonical Invocation"]
    assert "`deployctl <command> [arguments] [flags]`" in body
    assert "- `deployctl deploy rollback`: " in body


def test_agents_md_non_interactive_flags_lists_all_flags_that_suppress_prompts() -> None:
    body = sections(doc(fixture_prompt_app.app))["Non-Interactive Flags"]
    assert "`--yes`" in body and "`--non-interactive`" in body and "`--format json`" in body
    assert "`--confirm-destructive` (`deployctl cleanup`, `deployctl deploy rollback`)" in doc(
        deployctl
    )
    assert "`--headless`" in doc(authctl)


def test_agents_md_lists_every_env_var_with_type_and_description() -> None:
    app = fixture_config_app.app
    body = sections(doc(app))["Environment Variables"]
    for name, _ in app.environment():
        assert f"- `{name}` (" in body
    assert "- `CONFIGCTL_RETRIES` (integer, optional): Setting retries" in body
    assert "- `CONFIGCTL_MAX_OUTPUT_BYTES` (integer, optional): " in body
    shared = {n for names, _ in SHARED for n in names}
    assert shared == {v for v in UNPREFIXED if v.isupper()}
    assert all(f"`{n}`" in body for n in shared)
    assert [d.name for d in env_vars(authctl)] == sorted(d.name for d in env_vars(authctl))
    assert any("token" in d.description.lower() for d in env_vars(authctl))


def test_agents_md_input_conventions_describe_how_structured_input_is_passed() -> None:
    body = sections(doc(deployctl))["Input Conventions"]
    assert "positionals" in body and "--schema" in body and "`deployctl exec`" in body


def test_agents_md_includes_cli_version_on_line_1_matching_binary_version_output() -> None:
    first = doc(deployctl).splitlines()[0]
    out = io.StringIO()
    deployctl.run(["--version", "--format", "plain"], stdout=out, stderr=io.StringIO(), env={})
    assert first == f"<!-- cli-version: {out.getvalue().strip()} -->"


def test_every_flag_command_and_env_var_documented_in_agents_md_is_present_in_help_output() -> None:
    for app in (deployctl, authctl, fixture_config_app.app, fixture_prompt_app.app):
        assert check(app, Path("AGENTS.md"), doc(app), agents_md=True) == []
    texts = help_texts(deployctl)
    for path in deployctl.commands:
        entry = deployctl.manifest()["commands"][path.value]  # type: ignore[index]
        for flag in entry["flags"]:
            # A positional is shown as <name>, and --name also sets it
            assert re.search(rf"(--|<){flag}(?![\w-])", texts[path.parts]), (path, flag)


def test_agents_md_rewrite_keeps_text_outside_the_markers() -> None:
    old = (
        "<!-- cli-version: 0.0.1 -->\n# Mine\n\nKept above\n\n"
        + doc(deployctl).split(BEGIN)[1].join((BEGIN, "")).replace("deploy", "stale")
        + "\n## Developing\n\nKept below\n"
    )
    new = render_file(deployctl, old, "examples.deployctl:app", "deployctl")
    assert new.startswith(f"<!-- cli-version: {deployctl.version} -->\n# Mine\n\nKept above")
    assert new.endswith("## Developing\n\nKept below\n") and "stale" not in new
    assert render_file(deployctl, new, "examples.deployctl:app", "deployctl") == new


def test_agents_md_command_creates_then_reports_noop(tmp_path: Path) -> None:
    code, out = run_cli(["agents-md", "examples.deployctl:app"], tmp_path)
    assert code == 0 and json.loads(out)["data"]["effect"] == "created"
    code, out = run_cli(["agents-md", "examples.deployctl:app"], tmp_path)
    assert code == 0 and json.loads(out)["data"]["effect"] == "noop"
    text = (tmp_path / "AGENTS.md").read_text()
    assert all(f"## {title}\n" in text for title in SECTIONS) and END in text


# REQ-O-044


def test_agents_md_contains_an_installation_section_with_a_verification_command_after_it() -> None:
    body = sections(doc(deployctl))["Installation"]
    lines = [line for line in body.splitlines() if line and not line.startswith("```")]
    assert lines[0].startswith("uv tool install deployctl ")
    assert lines[1].startswith("deployctl --version ")


def test_the_install_command_is_run_twice_with_no_stdin_in_ci() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert ci.count("uv tool install treaty < /dev/null") == 2
    assert ci.count("uv tool install . < /dev/null") == 2
    assert "## Installation" in (ROOT / "AGENTS.md").read_text()


def test_treaty_init_writes_an_agents_md_that_passes_check_docs(tmp_path: Path) -> None:
    project = tmp_path / "shop-tool"
    code, _ = run_cli(["init", "shop-tool", "--directory", str(project)], tmp_path)
    text = (project / "AGENTS.md").read_text()
    assert code == 0 and "uv tool install .  " in text
    assert "shop_tool.cli:app" in (project / "tests" / "test_agents_md.py").read_text()
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "from treaty._cli import main; main()",
            *("check-docs", "shop_tool.cli:app", "AGENTS.md", "--format", "plain"),
        ],
        cwd=project,
        env={**os.environ, "PYTHONPATH": str(project / "src")},
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout == "1 file matches 0.1.0\n"


# REQ-O-045


def test_every_integration_artifact_contains_a_version_declaration(tmp_path: Path) -> None:
    out = io.StringIO()
    code = deployctl.run(
        ["generate-skills", "--output-dir", str(tmp_path / "skills")],
        stdout=out,
        stderr=io.StringIO(),
        env={},
    )
    assert code == 0
    for path in (tmp_path / "skills").iterdir():
        text = path.read_text()
        assert text.startswith(("<!-- cli-version: 1.4.0 -->\n", "---\n")), path
        if text.startswith("---\n"):
            assert '\nversion: "1.4.0"\n' in text.split("\n---\n")[0]


def test_declared_versions_and_names_in_skills_and_tool_lists_match_the_binary(
    tmp_path: Path,
) -> None:
    deployctl.run(
        ["generate-skills", "--output-dir", str(tmp_path / "skills")],
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        env={},
    )
    tools = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from treaty._mcp import main; sys.exit(main())",
            "examples.deployctl:app",
            "--list-tools",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    (tmp_path / "tools.json").write_text(tools.stdout)
    code, _ = run_cli(["check-docs", "examples.deployctl:app", "skills", "tools.json"], tmp_path)
    assert code == 0
    listing = json.loads(tools.stdout)
    (tmp_path / "tools.json").write_text(json.dumps({**listing, "cli_version": "0.9.0"}))
    code, out = run_cli(["check-docs", "examples.deployctl:app", "tools.json"], tmp_path)
    assert code == 81 and json.loads(out)["data"]["mismatches"][0]["name"] == "0.9.0"


# REQ-O-046


def test_ci_includes_a_step_that_runs_agents_md_validation() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "uv run treaty check-docs treaty._cli:cli AGENTS.md" in ci
    assert "\non:\n  push:" in ci and "  pull_request:" in ci


def test_treatys_own_agents_md_passes_the_check() -> None:
    code, out = run_cli(["check-docs", "treaty._cli:cli", "AGENTS.md"], ROOT)
    assert code == 0, out


def test_version_mismatch_fails(tmp_path: Path) -> None:
    text = doc(deployctl).replace("cli-version: 1.4.0", "cli-version: 1.3.0", 1)
    (tmp_path / "AGENTS.md").write_text(text)
    code, out = run_cli(["check-docs", "examples.deployctl:app", "AGENTS.md"], tmp_path)
    envelope = json.loads(out)
    assert code == 81 and envelope["error"]["code"] == "DOCS_OUT_OF_DATE"
    [m] = envelope["data"]["mismatches"]
    assert m["kind"] == "version" and m["line"] == 1


def test_any_flag_command_or_env_var_not_found_in_help_fails(tmp_path: Path) -> None:
    text = (
        doc(deployctl)
        .replace("`deployctl deploy rollback`:", "`deployctl deploy undo`:")
        .replace("`deployctl exec`", "`deployctl exec --replicas 3`")
        .replace("`DEPLOYCTL_SESSION`", "`DEPLOYCTL_SESSIONS`")
    )
    (tmp_path / "AGENTS.md").write_text(text + "\nSee `DEPLOYCTL_REGION`.\n")
    code, out = run_cli(["check-docs", "examples.deployctl:app", "AGENTS.md"], tmp_path)
    found = {(m["kind"], m["name"]) for m in json.loads(out)["data"]["mismatches"]}
    assert code == 81 and found == {
        ("command", "undo"),
        ("flag", "--replicas"),
        ("env", "DEPLOYCTL_SESSIONS"),
        ("env", "DEPLOYCTL_REGION"),
    }


def test_ci_step_produces_a_diff_style_report_listing_exactly_which_items_are_mismatched(
    tmp_path: Path,
) -> None:
    text = doc(deployctl).replace("`deployctl exec`", "`deployctl exec --replicas`")
    (tmp_path / "AGENTS.md").write_text(text.replace("## Input Conventions", "## Inputs"))
    code, out = run_cli(["check-docs", "examples.deployctl:app", "AGENTS.md"], tmp_path, plain=True)
    lines = [n for n, ln in enumerate(text.splitlines(), 1) if "--replicas" in ln]
    assert code == 81 and len(lines) == 2  # the command list and Input Conventions
    assert sorted(out.splitlines()) == [
        f"- {tmp_path / 'AGENTS.md'}:1 section Input Conventions: missing",
        *(
            f"- {tmp_path / 'AGENTS.md'}:{n} flag --replicas: not in deployctl exec --help"
            for n in lines
        ),
    ]


def test_the_validation_step_is_documented_in_agents_md_under_ci_validation() -> None:
    body = sections((ROOT / "AGENTS.md").read_text())["CI Validation"]
    assert "treaty check-docs treaty._cli:cli AGENTS.md" in body
    assert "check-docs" in sections(doc(deployctl))["CI Validation"]
