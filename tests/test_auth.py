"""Auth and scopes: REQ-C-021, C-029, O-033, O-047."""

import io
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import pytest
from conftest import spec_validator

from examples.authctl import app as authctl
from treaty import App, Ctx, NoArgs, RegistrationError
from treaty._audit import audit

TOKEN = "s3cr3t-token-value"
LOGGED_IN = {"AUTHCTL_TOKEN": TOKEN}


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def run(
    app: App,
    argv: list[str],
    env: dict[str, str] | None = None,
    *,
    stdin: IO[str] | None = None,
    isatty: bool = False,
) -> tuple[int, dict[str, object], str]:
    out, err = io.StringIO(), io.StringIO()
    code = app.run(
        [*argv, "--format", "json"],
        stdin=stdin if stdin is not None else io.StringIO(),
        stdout=out,
        stderr=err,
        env={"PATH": "/usr/bin", **(env or {})},
        isatty=isatty,
    )
    return code, json.loads(out.getvalue()), err.getvalue()


def error_of(envelope: dict[str, object]) -> dict[str, object]:
    error = envelope["error"]
    assert isinstance(error, dict)
    return error


class Fixed:
    def __init__(self, scopes: Iterable[str] | None) -> None:
        self.scopes = scopes

    def active_scopes(self, ctx: Ctx) -> Iterable[str] | None:
        return self.scopes


@dataclass(frozen=True, slots=True)
class Login:
    token_length: int | None
    headless: bool


def login_app(kind: str = "browser", **meta: object) -> App:
    app = App("authctl", version="1.0.0")

    @app.command(
        "login",
        description="Log in",
        danger_level="safe",
        exit_codes=(),
        auth=kind,
        **meta,  # type: ignore[arg-type]
    )
    def login(args: NoArgs, ctx: Ctx) -> Login:
        ctx.log("got token", value=ctx.token, echo=f"token is {ctx.token}")
        return Login(None if ctx.token is None else len(ctx.token), ctx.headless)

    return app


# C-029: required scopes


def test_requires_auth_without_required_scopes_fails_registration() -> None:
    app = App("x", version="1.0.0", credentials=Fixed(()))
    with pytest.raises(RegistrationError, match="required_scopes"):

        @app.command(
            "sync", description="Sync", danger_level="safe", exit_codes=(), requires_auth=True
        )
        def sync(args: NoArgs, ctx: Ctx) -> None:
            return None


def test_requires_auth_without_credentials_fails_registration() -> None:
    app = App("x", version="1.0.0")
    with pytest.raises(RegistrationError, match="credentials"):

        @app.command(
            "sync",
            description="Sync",
            danger_level="safe",
            exit_codes=(),
            requires_auth=True,
            required_scopes=["repo:read"],
        )
        def sync(args: NoArgs, ctx: Ctx) -> None:
            return None


def test_required_scopes_is_in_every_schema() -> None:
    for path in ("repos list", "version", "login"):
        _, env, _ = run(authctl, [*path.split(), "--schema"])
        data = env["data"]
        assert isinstance(data, dict)
        assert data["required_scopes"] == (["repo:read"] if path == "repos list" else [])


def test_not_logged_in_exits_8() -> None:
    code, env, _ = run(authctl, ["repos", "list"])
    error = error_of(env)
    assert code == 8 and error["code"] == "UNAUTHENTICATED"
    assert "authctl login" in str(error["suggestion"])


def test_a_missing_scope_exits_7_with_missing_scopes() -> None:
    code, env, _ = run(authctl, ["repos", "list"], {**LOGGED_IN, "AUTHCTL_SCOPES": "issues:read"})
    error = error_of(env)
    assert code == 7 and error["code"] == "PERMISSION_DENIED"
    assert error["context"]["missing_scopes"] == ["repo:read"]  # type: ignore[index]


def test_an_extra_scope_exits_0_with_an_over_privileged_warning() -> None:
    scopes = {**LOGGED_IN, "AUTHCTL_SCOPES": "repo:read,admin:org"}
    code, env, _ = run(authctl, ["repos", "list"], scopes)
    assert code == 0 and env["data"] == {"repos": ["api", "web"]}
    assert env["warnings"] == [
        {
            "code": "CREDENTIAL_OVER_PRIVILEGED",
            "message": "Credential has scopes beyond what repos.list requires",
            "context": {
                "command": "repos.list",
                "excess_scopes": ["admin:org"],
                "required_scopes": ["repo:read"],
            },
        }
    ]


def test_exact_scopes_exit_0_without_a_warning() -> None:
    code, env, _ = run(authctl, ["repos", "list"], LOGGED_IN)
    assert code == 0 and env["warnings"] == []


def test_auth_exit_codes_are_in_the_manifest() -> None:
    _, env, _ = run(authctl, ["repos", "list", "--schema"])
    data = env["data"]
    assert isinstance(data, dict)
    assert {"7", "8"} <= set(data["exit_codes"])


# O-047: check-permissions


def test_check_permissions_for_one_command() -> None:
    scopes = {**LOGGED_IN, "AUTHCTL_SCOPES": "repo:read,admin:org"}
    code, env, _ = run(authctl, ["check-permissions", "--for", "repos list"], scopes)
    assert code == 0
    assert env["data"] == {
        "command": "repos.list",
        "required_scopes": ["repo:read"],
        "active_scopes": ["admin:org", "repo:read"],
        "over_privileged": True,
    }
    assert [w["code"] for w in env["warnings"]] == ["CREDENTIAL_OVER_PRIVILEGED"]  # type: ignore[index, union-attr]


def test_check_permissions_without_for_maps_every_command() -> None:
    code, env, _ = run(authctl, ["check-permissions"], LOGGED_IN)
    assert code == 0 and env["warnings"] == []
    assert env["data"] == {
        "active_scopes": ["repo:read"],
        "commands": {
            "repos.list": {
                "required_scopes": ["repo:read"],
                "covered": True,
                "over_privileged": False,
            }
        },
    }


def test_check_permissions_with_insufficient_scopes_exits_8() -> None:
    scopes = {**LOGGED_IN, "AUTHCTL_SCOPES": "issues:read"}
    code, env, _ = run(authctl, ["check-permissions", "--for", "repos.list"], scopes)
    error = error_of(env)
    assert code == 8 and error["code"] == "INSUFFICIENT_SCOPES"
    assert error["context"]["missing_scopes"] == ["repo:read"]  # type: ignore[index]


def test_a_credential_without_scopes_is_never_over_privileged() -> None:
    app = App("x", version="1.0.0", credentials=Fixed(()))

    @app.command(
        "sync",
        description="Sync",
        danger_level="safe",
        exit_codes=(),
        requires_auth=True,
        required_scopes=["repo:read"],
    )
    def sync(args: NoArgs, ctx: Ctx) -> None:
        return None

    code, env, _ = run(app, ["check-permissions"])
    assert code == 0 and env["warnings"] == []
    assert env["data"]["commands"]["sync"]["over_privileged"] is False  # type: ignore[index]


def test_check_permissions_for_an_unknown_command_exits_5() -> None:
    code, env, _ = run(authctl, ["check-permissions", "--for", "nope"], LOGGED_IN)
    assert code == 5 and error_of(env)["code"] == "UNKNOWN_COMMAND"


def test_check_permissions_exists_only_with_credentials() -> None:
    assert "check-permissions" not in App("x", version="1.0.0").manifest()["commands"]  # type: ignore[operator]


# C-021, O-033: login commands


def test_browser_login_off_a_terminal_without_a_token_exits_4_listing_the_variable() -> None:
    code, env, _ = run(authctl, ["login"])
    error = error_of(env)
    assert code == 4 and error["code"] == "TOKEN_REQUIRED"
    assert error["context"]["token_env_vars"] == ["AUTHCTL_TOKEN"]  # type: ignore[index]
    assert error["auth_methods"] == [
        {"type": "env_var", "name": "AUTHCTL_TOKEN", "hint": "Set AUTHCTL_TOKEN to your API token"}
    ]


def test_browser_login_off_a_terminal_with_a_token_succeeds_and_never_prints_it() -> None:
    app = login_app()
    code, env, err = run(app, ["login"], {"AUTHCTL_TOKEN": TOKEN})
    assert code == 0 and env["data"] == {"token_length": len(TOKEN), "headless": True}
    assert TOKEN not in json.dumps(env) and TOKEN not in err
    assert "got token" in err


def test_token_env_var_reads_the_named_variable() -> None:
    code, env, _ = run(
        login_app(), ["login", "--headless", "--token-env-var", "MY_TOKEN"], {"MY_TOKEN": "abcdef"}
    )
    assert code == 0 and env["data"] == {"token_length": 6, "headless": True}


def test_headless_without_a_token_exits_4_listing_the_named_variable() -> None:
    code, env, _ = run(login_app(), ["login", "--headless", "--token-env-var", "MY_TOKEN"])
    error = error_of(env)
    assert code == 4 and error["context"]["token_env_vars"] == ["MY_TOKEN"]  # type: ignore[index]


def test_headless_on_a_terminal_suppresses_the_browser_login() -> None:
    app = login_app()
    code, _, _ = run(app, ["login"], {"DISPLAY": ":0"}, stdin=Terminal(), isatty=True)
    assert code == 0  # a person is there: the handler runs its browser flow
    code, env, _ = run(
        app, ["login", "--headless"], {"DISPLAY": ":0"}, stdin=Terminal(), isatty=True
    )
    assert code == 4 and error_of(env)["code"] == "TOKEN_REQUIRED"


def test_token_env_var_takes_only_a_variable_name() -> None:
    code, env, _ = run(login_app(), ["login", "--token-env-var", "abc=def"])
    assert code == 2 and "environment variable" in str(error_of(env)["message"])


def test_device_login_works_off_a_terminal_without_a_token() -> None:
    code, env, _ = run(login_app("device"), ["login"])
    assert code == 0 and env["data"] == {"token_length": None, "headless": True}


def test_extra_token_env_vars_are_read_in_order() -> None:
    app = login_app(token_env_vars=["AUTHCTL_API_KEY"])
    code, env, _ = run(app, ["login"], {"AUTHCTL_API_KEY": "abcd"})
    assert code == 0 and env["data"] == {"token_length": 4, "headless": True}


def test_login_flags_work_in_exec() -> None:
    line = {"_cmd": "login", "headless": True, "token_env_var": "MY_TOKEN"}
    out = io.StringIO()
    code = login_app().run(
        ["exec"],
        stdin=io.StringIO(json.dumps(line) + "\n"),
        stdout=out,
        stderr=io.StringIO(),
        env={"MY_TOKEN": "abcdef"},
    )
    assert code == 0 and json.loads(out.getvalue())["data"]["token_length"] == 6


def test_schema_of_a_login_command_declares_headless_support() -> None:
    _, env, _ = run(authctl, ["login", "--schema"])
    data = env["data"]
    assert isinstance(data, dict)
    assert data["headless_supported"] is False and data["token_env_vars"] == ["AUTHCTL_TOKEN"]
    assert {"headless", "token-env-var"} <= set(data["flags"]) and "4" in data["exit_codes"]
    _, env, _ = run(login_app("device"), ["login", "--schema"])
    assert env["data"]["headless_supported"] is True  # type: ignore[index]


def test_the_manifest_is_valid() -> None:
    _, env, _ = run(authctl, ["manifest"])
    errors = list(spec_validator("manifest-response").iter_errors(env["data"]))
    assert errors == []


@pytest.mark.parametrize(
    ("meta", "match"),
    [
        ({"auth": "oauth"}, "not one of"),
        ({"auth": None, "token_env_vars": ["X_TOKEN"]}, "login commands"),
        ({"auth": "browser", "token_env_vars": ["NOT-A-NAME"]}, "not environment variable"),
    ],
)
def test_bad_login_declarations_fail_registration(meta: dict[str, object], match: str) -> None:
    app = App("x", version="1.0.0")
    with pytest.raises(RegistrationError, match=match):

        @app.command("login", description="Log in", danger_level="safe", exit_codes=(), **meta)  # type: ignore[arg-type]
        def login(args: NoArgs, ctx: Ctx) -> None:
            return None


# Audit rules


def test_audit_flags_broad_scopes_and_undeclared_logins() -> None:
    app = App("x", version="1.0.0", credentials=Fixed(()))

    @app.command(
        "wipe",
        description="Remove everything",
        danger_level="safe",
        exit_codes=(),
        requires_auth=True,
        required_scopes=["admin:org"],
    )
    def wipe(args: NoArgs, ctx: Ctx) -> None:
        return None

    @app.command("login", description="Log in", danger_level="safe", exit_codes=())
    def login(args: NoArgs, ctx: Ctx) -> None:
        return None

    rules = {r.id: r for r in audit(app, "x", limit=10).rules}
    assert [f.command for f in rules["broad-scope"].findings] == ["wipe"]
    assert [f.command for f in rules["auth-declared"].findings] == ["login"]
    assert rules["broad-scope"].passed is False


def test_a_broad_scope_explained_in_the_description_passes() -> None:
    app = App("x", version="1.0.0", credentials=Fixed(()))

    @app.command(
        "wipe",
        description="Remove the org; the API offers only admin:org for it",
        danger_level="safe",
        exit_codes=(),
        requires_auth=True,
        required_scopes=["admin:org"],
    )
    def wipe(args: NoArgs, ctx: Ctx) -> None:
        return None

    rules = {r.id: r for r in audit(app, "x", limit=10).rules}
    assert rules["broad-scope"].passed


def test_an_idempotent_replay_still_checks_the_credential(tmp_path: Path) -> None:
    credentials = Fixed(["repo:write"])
    app = App("gated", version="1.0.0", credentials=credentials, state_dir=tmp_path)

    @app.command(
        "push",
        description="Push",
        danger_level="mutating",
        exit_codes=(),
        requires_auth=True,
        required_scopes=["repo:write"],
    )
    def push(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"effect": "created"}

    argv = ["push", "--idempotency-key", "k1"]
    assert run(app, argv)[0] == 0
    code, env, _ = run(app, argv)
    assert code == 0 and env["meta"]["idempotency_hit"] is True  # type: ignore[index]
    credentials.scopes = None
    code, env, _ = run(app, argv)
    assert code == 8 and error_of(env)["code"] == "UNAUTHENTICATED"
