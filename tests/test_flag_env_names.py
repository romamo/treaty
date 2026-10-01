"""Declared variable names on a command's flag, ``Flag(env=(...))`` (#9): a flag reads the
variable a wrapped service already uses, such as ``IBKR_FLEX_TOKEN``"""

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from conftest import spec_validator

from treaty import App, Ctx, Deprecated, EnvName, Flag, NoArgs, RegistrationError
from treaty._agents_md import env_vars
from treaty._audit import audit

TOKEN = "flex-tok-8f3a9c1d"


@dataclass(frozen=True, slots=True)
class Download:
    token: str = Flag(
        description="Flex Web Service token",
        pattern=r"flex-tok-[0-9a-f]{8}",
        env=("IBKR_FLEX_TOKEN", EnvName("IBKR_TOKEN", deprecated=Deprecated("0.9.0"))),
    )
    query_id: int = Flag(description="Flex Query ID", env=("IBKR_FLEX_QUERY_ID", "IBKR_QUERY"))
    project: Path | None = Flag(
        default=None, description="Project directory", env=("CLOUDFALL_PROJECT",)
    )
    accounts: tuple[str, ...] = Flag(default=(), description="Accounts", env=("IBKR_ACCOUNTS",))


@dataclass(frozen=True, slots=True)
class Downloaded:
    token_length: int
    query_id: int
    project: str | None
    accounts: list[str]


def make_app() -> App:
    app = App("py-ibkr", version="1.0.0", description="Flex reports")

    @app.command(
        "download",
        description="Download a report",
        danger_level="safe",
        exit_codes=(),
        supports_raw_payload=True,
    )
    def download(args: Download, ctx: Ctx) -> Downloaded:
        ctx.log(f"downloading with {args.token}")
        project = None if args.project is None else args.project.name
        return Downloaded(len(args.token), args.query_id, project, list(args.accounts))

    return app


def run(
    argv: list[str], env: dict[str, str], *, app: App | None = None, stdin: str = ""
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = (app or make_app()).run(
        argv,
        stdin=io.StringIO(stdin),
        stdout=out,
        stderr=err,
        env={"PY_IBKR_AUDIT_LOG": "0", **env},
        isatty=False,
    )
    return code, out.getvalue(), err.getvalue()


def envelope(argv: list[str], env: dict[str, str]) -> tuple[int, Any, str]:
    code, out, err = run(argv, env)
    return code, json.loads(out), err


def test_a_plain_and_a_secret_flag_read_their_declared_names() -> None:
    env = {"IBKR_FLEX_TOKEN": TOKEN, "IBKR_FLEX_QUERY_ID": "42", "CLOUDFALL_PROJECT": "/w/p"}
    code, result, _ = envelope(["download"], env)
    assert code == 0, result
    assert result["data"] == {
        "token_length": len(TOKEN),
        "query_id": 42,
        "project": "p",
        "accounts": [],
    }


def test_argv_wins_then_the_prefixed_name_then_the_declared_names_in_order(
    tmp_path: Path,
) -> None:
    secret = tmp_path / "token"
    secret.write_text("flex-tok-00000000\n")
    env = {
        "PY_IBKR_TOKEN": "flex-tok-11111111",
        "IBKR_FLEX_TOKEN": "flex-tok-22222222",
        "IBKR_FLEX_QUERY_ID": "1",
        "IBKR_QUERY": "2",
    }
    app = make_app()
    seen: list[tuple[str, int]] = []

    @app.command("peek", description="Peek", danger_level="safe", exit_codes=())
    def peek(args: Download, ctx: Ctx) -> NoArgs:
        seen.append((args.token, args.query_id))
        return NoArgs()

    run(["peek", "--token-from-file", str(secret), "--query-id", "9"], env, app=app)
    run(["peek"], env, app=app)
    run(["peek"], {k: v for k, v in env.items() if not k.startswith("PY_")}, app=app)
    run(
        ["peek"],
        {"IBKR_FLEX_TOKEN": "", "IBKR_TOKEN": "flex-tok-33333333", "IBKR_QUERY": "2"},
        app=app,
    )
    assert seen == [
        ("flex-tok-00000000", 9),
        ("flex-tok-11111111", 1),
        ("flex-tok-22222222", 1),
        ("flex-tok-33333333", 2),
    ]


def test_every_input_path_reads_the_same_variables() -> None:
    env = {"IBKR_FLEX_TOKEN": TOKEN, "IBKR_QUERY": "7", "IBKR_ACCOUNTS": "U1,U2"}
    expected = {
        "token_length": len(TOKEN),
        "query_id": 7,
        "project": None,
        "accounts": ["U1", "U2"],
    }
    _, argv, _ = envelope(["download"], env)
    _, payload, _ = envelope(["download", "--raw-payload", "{}"], env)
    code, out, _ = run(["exec"], env, stdin='{"_cmd": "download"}\n')
    called = make_app().call("download", {}, env={"PY_IBKR_AUDIT_LOG": "0", **env})
    assert argv["data"] == payload["data"] == json.loads(out)["data"] == expected
    assert called.ok and called.data == expected
    # A value given in the payload still wins over the variable
    _, given, _ = envelope(["download", "--raw-payload", '{"query_id": 3}'], env)
    assert given["data"]["query_id"] == 3


def test_a_bad_value_from_a_declared_name_exits_2_naming_the_variable() -> None:
    code, result, _ = envelope(["download"], {"IBKR_FLEX_TOKEN": TOKEN, "IBKR_QUERY": "many"})
    error = result["error"]
    assert code == 2 and error["message"].startswith("IBKR_QUERY: ")
    assert error["context"]["source"] == "IBKR_QUERY"
    assert error["context"]["flag"] == "query-id"


def test_a_secret_from_a_declared_name_stays_redacted(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    env = {"IBKR_FLEX_TOKEN": TOKEN, "IBKR_QUERY": "1", "PY_IBKR_AUDIT_LOG": str(log)}
    code, out, err = run(["download", "--verbose"], env)
    assert code == 0 and TOKEN not in out and TOKEN not in err
    assert TOKEN not in log.read_text() and "[REDACTED]" in log.read_text()
    bad = "flex-tok-NOT-HEX!"
    code, out, err = run(["download"], {"IBKR_FLEX_TOKEN": bad, "IBKR_QUERY": "1"})
    error = json.loads(out)["error"]
    assert code == 2 and bad not in out and bad not in err
    assert error["context"]["source"] == "IBKR_FLEX_TOKEN"


def test_a_deprecated_name_warns_naming_the_replacement() -> None:
    code, result, err = envelope(["download"], {"IBKR_TOKEN": TOKEN, "IBKR_QUERY": "1"})
    assert code == 0
    [warning] = result["warnings"]
    assert warning["code"] == "DEPRECATED_ENV_VAR"
    assert warning["context"] == {
        "since": "0.9.0",
        "variable": "IBKR_TOKEN",
        "replacement": "PY_IBKR_TOKEN",
    }
    assert TOKEN not in err
    _, quiet, _ = envelope(["download"], {"IBKR_FLEX_TOKEN": TOKEN, "IBKR_QUERY": "1"})
    assert quiet["warnings"] == []


def test_the_manifest_lists_the_names_and_validates_against_the_spec() -> None:
    manifest = make_app().manifest()
    spec_validator("manifest-response").validate(manifest)
    entry = manifest["commands"]["download"]
    assert entry["secret_env_vars"] == ["PY_IBKR_TOKEN", "IBKR_FLEX_TOKEN", "IBKR_TOKEN"]
    query = entry["flags"]["query-id"]
    assert query["required"] is False
    assert query["description"] == (
        "Flex Query ID (read from $IBKR_FLEX_QUERY_ID or $IBKR_QUERY when not passed)"
    )


def test_help_and_agents_md_list_the_names() -> None:
    _, _, err = run(["download", "--help"], {})
    assert "$IBKR_FLEX_TOKEN" in err and "read when $PY_IBKR_TOKEN is not set" in err
    assert "$IBKR_QUERY" in err and "Flex Query ID: read when --query-id is not given" in err
    assert "(deprecated since 0.9.0; use PY_IBKR_TOKEN)" in err
    _, _, root = run(["--help"], {})
    assert "CLOUDFALL_PROJECT" in root.split("Environment\n", 1)[1]
    docs = {d.name: d for d in env_vars(make_app())}
    assert docs["IBKR_FLEX_QUERY_ID"].type == "integer"
    assert docs["IBKR_FLEX_TOKEN"].description == (
        "Default of --token of download, when PY_IBKR_TOKEN is not set"
    )


def test_the_env_prefix_rule_accepts_a_name_the_command_declares() -> None:
    app = make_app()

    @app.command("raw", description="Raw", danger_level="safe", exit_codes=())
    def raw(args: Download, ctx: Ctx) -> NoArgs:
        ctx.env.get("IBKR_FLEX_TOKEN")
        return NoArgs()

    @app.command("other", description="Other", danger_level="safe", exit_codes=())
    def other(args: NoArgs, ctx: Ctx) -> NoArgs:
        ctx.env.get("IBKR_FLEX_TOKEN")
        return args

    report = audit(app, "py-ibkr", limit=3)
    [rule] = [r for r in report.rules if r.id == "env-prefix"]
    assert [f.command for f in rule.findings] == ["other"]


def test_flags_of_different_commands_may_share_a_name() -> None:
    app = make_app()

    @app.command("again", description="Again", danger_level="safe", exit_codes=())
    def again(args: Download, ctx: Ctx) -> NoArgs:
        return NoArgs()

    assert "again" in app.manifest()["commands"]


@dataclass(frozen=True)
class Settings:
    region: str = Flag(default="eu", description="Region", env=("AWS_REGION",))


@pytest.mark.parametrize(
    ("env", "secret", "match"),
    [
        (("PY_IBKR_TOKEN",), True, "own variable"),
        (("PY_IBKR_FORMAT",), False, "framework"),
        (("PY_IBKR_REGION",), False, "setting 'region'"),
        (("AWS_REGION",), False, "setting 'region'"),
        (("PY_IBKR_OTHER_KEY",), False, "--other-key"),
        (("SHARED",), False, "--token of cmd"),
        ((EnvName("OLD", deprecated=Deprecated("1.0.0", replacement="NOPE")),), False, "'NOPE'"),
    ],
)
def test_a_bad_declared_name_is_refused_at_registration(
    env: tuple[object, ...], secret: bool, match: str
) -> None:
    @dataclass(frozen=True)
    class Args:
        token: str = Flag(default="", description="T", secret=secret, env=env)
        other_key: str = Flag(default="", description="K")
        other: str = Flag(default="", description="O", env=("SHARED",))

    app = App("py-ibkr", version="1.0.0", settings=Settings)
    with pytest.raises(RegistrationError, match=match):

        @app.command("cmd", description="Cmd", danger_level="safe", exit_codes=())
        def cmd(args: Args, ctx: Ctx) -> NoArgs:
            return NoArgs()


def test_a_plain_flag_may_name_its_prefixed_variable_and_a_deprecation_names_the_flag() -> None:
    @dataclass(frozen=True)
    class Args:
        region: str = Flag(
            default="eu",
            description="Region",
            env=("PY_IBKR_REGION", EnvName("REGION", deprecated=Deprecated("1.0.0"))),
        )

    app = App("py-ibkr", version="1.0.0")

    @app.command("where", description="Where", danger_level="safe", exit_codes=())
    def where(args: Args, ctx: Ctx) -> dict[str, str]:
        return {"region": args.region}

    result = app.call("where", {}, env={"REGION": "us", "PY_IBKR_AUDIT_LOG": "0"})
    assert result.data == {"region": "us"}
    [warning] = result.warnings
    assert warning.context["replacement"] == "--region"
    assert app.call("where", {}, env={"PY_IBKR_REGION": "ap"}).data == {"region": "ap"}


@dataclass(frozen=True, slots=True)
class Window:
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start > 99:
            raise ZeroDivisionError("a bug in the object's own check")


def test_an_object_flag_reads_its_json_from_a_declared_name() -> None:
    @dataclass(frozen=True)
    class Args:
        window: Window = Flag(description="Window", env=("IBKR_WINDOW",))

    app = App("py-ibkr", version="1.0.0")

    @app.command("span", description="Span", danger_level="safe", exit_codes=())
    def span(args: Args, ctx: Ctx) -> dict[str, int]:
        return {"width": args.window.end - args.window.start}

    code, out, _ = run(["span"], {"IBKR_WINDOW": '{"start": 2, "end": 5}'}, app=app)
    assert code == 0 and json.loads(out)["data"] == {"width": 3}
    code, out, _ = run(["span"], {"IBKR_WINDOW": '{"start": 2}'}, app=app)
    error = json.loads(out)["error"]
    assert code == 2 and error["message"].startswith("IBKR_WINDOW: ")
    # An object's __post_init__ bug read from a variable is reported as on argv
    crashed = '{"start": 100, "end": 101}'
    via_argv = run(["span", "--window", crashed], {}, app=app)
    via_env = run(["span"], {"IBKR_WINDOW": crashed}, app=app)
    assert via_env[0] == via_argv[0] != 0
    argv_error, env_error = (json.loads(r[1])["error"] for r in (via_argv, via_env))
    assert env_error["code"] == argv_error["code"]


def test_a_list_of_objects_cannot_declare_names() -> None:
    @dataclass(frozen=True)
    class Args:
        windows: tuple[Window, ...] = Flag(default=(), description="Windows", env=("WINDOWS",))

    app = App("py-ibkr", version="1.0.0")
    with pytest.raises(RegistrationError, match="list of JSON objects"):

        @app.command("spans", description="Spans", danger_level="safe", exit_codes=())
        def spans(args: Args, ctx: Ctx) -> NoArgs:
            return NoArgs()
