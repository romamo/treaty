"""Declared variable names, ``Flag(env=(...))`` (#7): a setting kept under the variable its
users already export, such as ``BEANCOUNT_FILE``"""

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from treaty import App, Ctx, Deprecated, EnvName, Flag, NoArgs, RegistrationError
from treaty._agents_md import env_vars
from treaty._audit import audit


@dataclass(frozen=True, slots=True)
class Settings:
    file: Path = Flag(
        default=Path("main.beancount"),
        description="Ledger file",
        env=("BEANCOUNT_FILE", EnvName("LEDGER_FILE", deprecated=Deprecated("1.4.0"))),
    )
    retries: int = Flag(default=3, description="Retries", env=("BEAN_RETRIES_LEGACY",))


@dataclass(frozen=True, slots=True)
class Shown:
    file: str
    retries: int


def make_app() -> App:
    app = App("bean", version="2.0.0", description="Ledger", settings=Settings)

    @app.command("show", description="Show the settings", danger_level="safe", exit_codes=())
    def show(args: NoArgs, ctx: Ctx, settings: Settings) -> Shown:
        return Shown(settings.file.name, settings.retries)

    return app


def run(argv: list[str], env: dict[str, str], cwd: Path | None = None) -> tuple[int, Any, str]:
    out, err = io.StringIO(), io.StringIO()
    base = {"BEAN_AUDIT_LOG": "0", "XDG_CONFIG_HOME": str(cwd or Path("/nonexistent"))}
    code = make_app().run(argv, stdout=out, stderr=err, env={**base, **env}, isatty=False)
    return code, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()


def test_a_declared_name_supplies_the_setting_and_show_config_names_it(tmp_path: Path) -> None:
    # An absolute path on every platform: "/books" has no drive on Windows
    ledger = str(tmp_path / "a.beancount")
    code, shown, _ = run(["--show-config"], {"BEANCOUNT_FILE": ledger})
    data = shown["data"]
    assert code == 0 and data["sources"]["file"] == "env:BEANCOUNT_FILE"
    assert data["effective_config"]["file"] == ledger
    code, ran, _ = run(["show"], {"BEANCOUNT_FILE": ledger})
    assert code == 0 and ran["data"]["file"] == "a.beancount"


def test_the_prefixed_name_wins_then_the_declared_names_in_order() -> None:
    env = {
        "BEAN_FILE": "/p.beancount",
        "BEANCOUNT_FILE": "/b.beancount",
        "LEDGER_FILE": "/l.beancount",
    }
    _, shown, _ = run(["--show-config"], env)
    assert shown["data"]["sources"]["file"] == "env:BEAN_FILE"
    del env["BEAN_FILE"]
    _, shown, _ = run(["--show-config"], env)
    assert shown["data"]["sources"]["file"] == "env:BEANCOUNT_FILE"
    env["BEANCOUNT_FILE"] = ""  # an empty variable is unset, as for the prefixed name
    _, shown, _ = run(["--show-config"], env)
    assert shown["data"]["sources"]["file"] == "env:LEDGER_FILE"


def test_a_declared_name_outranks_the_config_files_and_the_default(tmp_path: Path) -> None:
    config = tmp_path / "bean" / "config.toml"
    config.parent.mkdir()
    config.write_text('file = "/from-file.beancount"\n')
    _, shown, _ = run(["--show-config"], {}, cwd=tmp_path)
    assert shown["data"]["sources"]["file"] == f"file:{config}"
    _, shown, _ = run(["--show-config"], {"BEANCOUNT_FILE": "/env.beancount"}, cwd=tmp_path)
    assert shown["data"]["sources"]["file"] == "env:BEANCOUNT_FILE"
    _, shown, _ = run(["--show-config"], {})
    assert shown["data"]["sources"]["file"] == "default"


def test_a_bad_value_under_a_declared_name_is_config_invalid_naming_that_variable() -> None:
    code, envelope, _ = run(["show"], {"BEAN_RETRIES_LEGACY": "many"})
    error = envelope["error"]
    assert code == 2 and error["code"] == "CONFIG_INVALID"
    assert error["context"] == {"source": "BEAN_RETRIES_LEGACY", "key": "retries"}
    assert error["message"].startswith("BEAN_RETRIES_LEGACY: ")


def test_a_deprecated_name_still_works_and_warns_naming_the_replacement() -> None:
    code, envelope, err = run(["show"], {"LEDGER_FILE": "/old.beancount"})
    assert code == 0 and envelope["data"]["file"] == "old.beancount"
    [warning] = envelope["warnings"]
    assert warning["code"] == "DEPRECATED_ENV_VAR"
    assert warning["context"] == {
        "since": "1.4.0",
        "variable": "LEDGER_FILE",
        "replacement": "BEAN_FILE",
    }
    assert "LEDGER_FILE is deprecated since 1.4.0; use BEAN_FILE instead" in err


def test_a_permanent_shared_name_is_silent() -> None:
    _, envelope, err = run(["show"], {"BEANCOUNT_FILE": "/shared.beancount"})
    assert envelope["warnings"] == [] and "DEPRECATED" not in err


def test_the_same_precedence_holds_for_an_in_process_call() -> None:
    env = {"BEAN_AUDIT_LOG": "0", "BEANCOUNT_FILE": "/b.beancount", "LEDGER_FILE": "/l.beancount"}
    envelope = make_app().call("show", {}, env=env)
    assert envelope.ok and envelope.data == {"file": "b.beancount", "retries": 3}


def test_help_and_agents_md_list_the_declared_names() -> None:
    err = io.StringIO()
    make_app().run(["--help"], stdout=io.StringIO(), stderr=err, env={}, isatty=False)
    section = err.getvalue().split("Environment\n", 1)[1]
    assert "BEANCOUNT_FILE" in section and "Setting file, when BEAN_FILE is not set" in section
    assert "LEDGER_FILE" in section and "(deprecated since 1.4.0; use BEAN_FILE)" in section
    docs = {d.name: d for d in env_vars(make_app())}
    assert docs["BEAN_RETRIES_LEGACY"].type == "integer"
    assert docs["BEANCOUNT_FILE"].description == "Setting file, when BEAN_FILE is not set"


def test_the_env_prefix_rule_accepts_a_declared_name() -> None:
    app = make_app()

    @app.command("raw", description="Raw", danger_level="safe", exit_codes=())
    def raw(args: NoArgs, ctx: Ctx) -> NoArgs:
        ctx.env.get("BEANCOUNT_FILE")
        ctx.env.get("UNDECLARED_FILE")
        return args

    report = audit(app, "bean", limit=3)
    [rule] = [r for r in report.rules if r.id == "env-prefix"]
    assert [f.fix for f in rule.findings] == [
        "read BEAN_UNDECLARED_FILE instead of UNDECLARED_FILE"
    ]


def _settings(**fields: object) -> type:
    """A settings dataclass with one ``Flag`` field per keyword"""
    cls = type("S", (), {"__annotations__": {k: str for k in fields}, **fields})
    return dataclass(frozen=True)(cls)


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        ({"file": Flag(default="", description="F", env=("BEAN_FILE",))}, "own variable"),
        ({"file": Flag(default="", description="F", env=("BEAN_FORMAT",))}, "framework"),
        (
            {
                "file": Flag(default="", description="F", env=("BEAN_DIR",)),
                "dir": Flag(default="", description="D"),
            },
            "setting 'dir'",
        ),
        (
            {
                "file": Flag(default="", description="F", env=("SHARED",)),
                "dir": Flag(default="", description="D", env=("SHARED",)),
            },
            "setting 'file'",
        ),
        (
            {
                "file": Flag(
                    default="",
                    description="F",
                    env=(EnvName("OLD", deprecated=Deprecated("1.0.0", replacement="NOPE")),),
                )
            },
            "replacement 'NOPE'",
        ),
    ],
)
def test_a_bad_declared_name_is_refused_at_registration(
    fields: dict[str, object], match: str
) -> None:
    with pytest.raises(RegistrationError, match=match):
        App("bean", version="1.0.0", settings=_settings(**fields))


def test_env_takes_a_tuple_of_names_or_env_names() -> None:
    with pytest.raises(RegistrationError, match="tuple of variable names"):
        Flag(description="F", env="BEANCOUNT_FILE")  # type: ignore[arg-type]
    with pytest.raises(RegistrationError, match="variable names or treaty.EnvName"):
        Flag(description="F", env=(3,))  # type: ignore[arg-type]
    for bad in ("", "1X", "BEAN-FILE", "A B"):
        with pytest.raises(RegistrationError, match="not an environment variable name"):
            Flag(description="F", env=(bad,))
    with pytest.raises(RegistrationError, match="more than once"):
        Flag(description="F", env=("A", EnvName("A")))
    with pytest.raises(RegistrationError, match="takes treaty.Deprecated"):
        EnvName("OLD", deprecated="1.0.0")  # type: ignore[arg-type]


def test_a_deprecated_name_may_point_at_another_declared_name() -> None:
    @dataclass(frozen=True)
    class Moved:
        file: str = Flag(
            default="",
            description="F",
            env=("NEW_FILE", EnvName("OLD_FILE", deprecated=Deprecated("1.0.0", "NEW_FILE"))),
        )

    app = App("bean", version="1.0.0", settings=Moved)

    @app.command("show", description="Show", danger_level="safe", exit_codes=())
    def show(args: NoArgs, ctx: Ctx, settings: Moved) -> dict[str, str]:
        return {"file": settings.file}

    envelope = app.call("show", {}, env={"OLD_FILE": "x", "BEAN_AUDIT_LOG": "0"})
    assert envelope.data == {"file": "x"}
    [warning] = envelope.warnings
    assert warning.context["replacement"] == "NEW_FILE"


def test_env_on_a_command_flag_is_refused_until_flags_read_variables() -> None:
    @dataclass(frozen=True)
    class Args:
        file: str = Flag(default="", description="F", env=("BEANCOUNT_FILE",))

    app = App("bean", version="1.0.0")
    with pytest.raises(RegistrationError, match="env= is for settings fields"):

        @app.command("show", description="Show", danger_level="safe", exit_codes=())
        def show(args: Args, ctx: Ctx) -> Args:
            return args
