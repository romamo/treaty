"""Regressions for the medium findings of the pre-1.0 review: AGENTS.md markers,
check-docs launchers, skill file names, config contexts, argv and JSON agreeing on
numbers, patterns, and repeated keys, and the forgiving JSON reader's corrections."""

import io
import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from treaty import App, Ctx, Flag, NoArgs, RegistrationError
from treaty._agents_md import BEGIN, END, check, render_file
from treaty._errors import CliExit
from treaty._json5 import Unreadable, loads_forgiving

BASE_ENV = {"PATH": os.environ["PATH"], "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}


def run(app: App, argv: list[str], env: dict[str, str] | None = None) -> tuple[int, dict]:  # type: ignore[type-arg]
    out = io.StringIO()
    code = app.run(
        [*argv, "--format", "json"],
        stdout=out,
        stderr=io.StringIO(),
        env={**BASE_ENV, **(env or {})},
    )
    return code, json.loads(out.getvalue())


def demo_app() -> App:
    app = App("demo", version="1.0.0")

    @app.command("hello", description="Say hello", danger_level="safe", exit_codes=())
    def hello(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    return app


# AGENTS.md


def test_agents_md_replaces_its_block_when_prose_quotes_the_end_marker_first() -> None:
    app = demo_app()
    text = f"# Notes\n\nThe block ends at `{END}`.\n\n"
    for _ in range(3):
        text = render_file(app, text, "demo.cli:app", "demo")
    assert text.count(BEGIN) == 1 and text.count(END) == 2


def test_agents_md_refuses_a_begin_marker_without_an_end_marker() -> None:
    with pytest.raises(CliExit, match="no"):
        render_file(demo_app(), f"# Notes\n\n{BEGIN}\nstale\n", "demo.cli:app", "demo")


@pytest.mark.parametrize(
    "line",
    ["`uv run demo nosuch`", "`uvx demo nosuch`", "`$ demo nosuch`", "`pipx run demo nosuch`"],
)
def test_check_docs_checks_a_command_line_behind_a_launcher(line: str) -> None:
    text = f"<!-- cli-version: 1.0.0 -->\n# demo\n\nRun {line} to start.\n"
    [mismatch] = check(demo_app(), Path("AGENTS.md"), text, agents_md=False)
    assert (mismatch.kind, mismatch.name) == ("command", "nosuch")


def test_check_docs_passes_a_launcher_line_that_names_a_real_command() -> None:
    text = "<!-- cli-version: 1.0.0 -->\n# demo\n\nRun `uv run demo hello` to start.\n"
    assert check(demo_app(), Path("AGENTS.md"), text, agents_md=False) == []


# Skill files


def test_commands_that_differ_only_in_dot_and_hyphen_are_refused() -> None:
    app = App("cachectl", version="1.0.0")

    @app.command("cache.clear", description="Clear", danger_level="safe", exit_codes=())
    def clear(args: NoArgs, ctx: Ctx) -> None:
        return None

    with pytest.raises(RegistrationError, match="differ only in"):

        @app.command("cache-clear", description="Clear", danger_level="safe", exit_codes=())
        def clear_too(args: NoArgs, ctx: Ctx) -> None:
            return None


# Config contexts


@dataclass(frozen=True, slots=True)
class Settings:
    region: str = "us-east-1"


def settings_app() -> App:
    app = App("ctxctl", version="1.0.0", settings=Settings)

    @app.command("show", description="Show", danger_level="safe", exit_codes=())
    def show(args: NoArgs, ctx: Ctx, settings: Settings) -> dict[str, str]:
        return {"region": settings.region}

    return app


def plain_app() -> App:
    app = App("ctxctl", version="1.0.0")

    @app.command("show", description="Show", danger_level="safe", exit_codes=())
    def show(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    return app


def test_a_context_variable_does_not_break_runs_that_read_no_config(tmp_path: Path) -> None:
    env = {"CTXCTL_CONTEXT": "prod", "HOME": str(tmp_path)}
    assert run(settings_app(), ["show", "--no-config", "--cwd", str(tmp_path)], env)[0] == 0
    assert run(plain_app(), ["show", "--cwd", str(tmp_path)], env)[0] == 0


def test_the_selected_context_outranks_the_project_files_top_level(tmp_path: Path) -> None:
    (tmp_path / ".ctxctl.toml").write_text('region = "eu-west-1"\n')
    user = tmp_path / "xdg" / "ctxctl"
    user.mkdir(parents=True)
    (user / "config.toml").write_text('[contexts.prod]\nregion = "prod-region"\n')
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    argv = ["show", "--cwd", str(tmp_path)]
    assert run(settings_app(), argv, env)[1]["data"] == {"region": "eu-west-1"}
    assert run(settings_app(), [*argv, "--context", "prod"], env)[1]["data"] == {
        "region": "prod-region"
    }


# argv and JSON read numbers, patterns, and keys alike


@dataclass(frozen=True, slots=True)
class Counted:
    n: int = Flag(default=0, description="A count")
    price: float = Flag(default=0.0, description="A price", pattern=r"[0-9]+\.[0-9]")


def number_app() -> App:
    app = App("numctl", version="1.0.0")

    @app.command(
        "count", description="Count", danger_level="safe", exit_codes=(), supports_raw_payload=True
    )
    def count(args: Counted, ctx: Ctx) -> dict[str, object]:
        return {"n": args.n, "price": args.price}

    return app


@pytest.mark.parametrize("value", ["1_000", "+3", " 7", "٣", "0x1F"])
def test_an_integer_on_argv_is_written_as_json_writes_it(value: str) -> None:
    code, envelope = run(number_app(), ["count", "--n", value])
    assert code == 2, envelope


@pytest.mark.parametrize(("price", "ok"), [("1.5", True), ("1.50", True), ("1.25", False)])
def test_a_number_pattern_gives_argv_and_json_the_same_verdict(price: str, ok: bool) -> None:
    argv_code, _ = run(number_app(), ["count", "--price", price])
    payload = json.dumps({"price": float(price)})
    json_code, _ = run(number_app(), ["count", "--raw-payload", payload])
    assert (argv_code == 0, json_code == 0) == (ok, ok)


def test_a_payload_that_repeats_a_key_is_refused() -> None:
    code, envelope = run(number_app(), ["count", "--raw-payload", '{"n": 1, "n": 2}'])
    assert code == 2, envelope


# The forgiving JSON reader


def test_a_comment_right_after_a_number_leaves_the_number() -> None:
    assert loads_forgiving("{a: 1/* c */, b: true// note\n}") == {"a": 1, "b": True}


@pytest.mark.parametrize("word", ["+1", "0x1F", ".5", "NaN", "Infinity"])
def test_a_near_number_is_refused_without_a_string_correction(word: str) -> None:
    with pytest.raises(Unreadable) as caught:
        loads_forgiving(f"{{a: {word}}}")
    assert caught.value.corrected is None


def test_the_forgiving_reader_refuses_a_repeated_key() -> None:
    with pytest.raises(Unreadable, match="twice"):
        loads_forgiving("{a: 1, a: 2}")
