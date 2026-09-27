"""Flag names treaty reserves for its own features (REQ-F-079)"""

import io
import json
from dataclasses import dataclass

import pytest

from treaty import App, Ctx, Flag, RegistrationError
from treaty._framework import RESERVED_GLOBAL, RESERVED_OPT_IN, UNIMPLEMENTED


@dataclass(frozen=True, slots=True)
class Plain:
    name: str = Flag(default="a", description="Name")


def _app() -> App:
    app = App("t", version="1.0.0")

    @app.command("show", description="Show", danger_level="safe", exit_codes=())
    def show(args: Plain, ctx: Ctx) -> dict[str, str]:
        return {"name": args.name}

    @app.command(
        "fetch", description="Fetch", danger_level="safe", exit_codes=(), has_network_io=True
    )
    def fetch(args: Plain, ctx: Ctx) -> dict[str, str]:
        return {"name": args.name}

    return app


def _run(app: App, argv: list[str]) -> tuple[int, dict]:
    out = io.StringIO()
    code = app.run(argv, stdout=out, stderr=io.StringIO(), env={})
    return code, json.loads(out.getvalue())


def test_a_field_named_like_a_reserved_global_fails_registration() -> None:
    @dataclass(frozen=True, slots=True)
    class Loud:
        verbose: bool = Flag(default=False, description="Chatty")

    app = App("t", version="1.0.0")
    with pytest.raises(RegistrationError, match="REQ-F-079"):

        @app.command("go", description="Go", danger_level="safe", exit_codes=())
        def go(args: Loud, ctx: Ctx) -> None:
            return None


def test_a_boolean_whose_negation_is_reserved_fails_registration() -> None:
    @dataclass(frozen=True, slots=True)
    class Checks:
        update_check: bool = Flag(default=True, description="Check for updates")

    app = App("t", version="1.0.0")
    with pytest.raises(RegistrationError, match="update-check"):

        @app.command("go", description="Go", danger_level="safe", exit_codes=())
        def go(args: Checks, ctx: Ctx) -> None:
            return None


def test_an_opt_in_name_collides_only_on_commands_that_opt_in() -> None:
    @dataclass(frozen=True, slots=True)
    class Via:
        proxy: str = Flag(default="", description="Proxy URL")

    app = App("t", version="1.0.0")

    @app.command("local", description="Local", danger_level="safe", exit_codes=())
    def local(args: Via, ctx: Ctx) -> None:
        return None

    with pytest.raises(RegistrationError, match="proxy"):

        @app.command(
            "remote", description="Remote", danger_level="safe", exit_codes=(), has_network_io=True
        )
        def remote(args: Via, ctx: Ctx) -> None:
            return None


@pytest.mark.parametrize("name", sorted(RESERVED_GLOBAL & UNIMPLEMENTED))
def test_an_unimplemented_reserved_global_exits_2_as_reserved(name: str) -> None:
    code, env = _run(_app(), ["show", f"--{name}"])
    assert code == 2
    assert env["error"]["code"] == "RESERVED_FLAG"
    assert env["error"]["context"]["flag"] == name


def test_a_reserved_global_is_a_global_before_the_command_path_too() -> None:
    code, env = _run(_app(), ["--fields", "name", "show"])
    assert code == 0 and env["data"] == {"name": "a"}


def test_an_opt_in_name_is_a_flag_only_on_a_command_that_opts_in() -> None:
    code, env = _run(_app(), ["fetch", "--proxy", "http://p"])
    assert code == 0
    code, env = _run(_app(), ["show", "--proxy", "http://p"])
    assert code == 2 and env["error"]["code"] == "ARG_ERROR"  # an unknown flag there


def test_every_reserved_name_is_a_flag_name() -> None:
    for name in (*RESERVED_GLOBAL, *RESERVED_OPT_IN):
        assert name == name.lower() and not name.startswith("-") and "_" not in name
