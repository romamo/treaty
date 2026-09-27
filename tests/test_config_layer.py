"""Config layer and env namespace: REQ-F-073, O-042, F-028, O-015, O-016, O-024, O-036, F-076"""

import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from conftest import WINDOWS

from treaty import App, Ctx, NoArgs
from treaty._audit import audit

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
    assert version({"CONFIGCTL_FORMAT": "plain"}).startswith("name: configctl")


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
