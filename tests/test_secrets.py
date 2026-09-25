"""Secrets arrive via env var or file, never argv (REQ-C-016, REQ-O-022), and are never echoed."""

import io
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import spec_validator

from treaty import App, Arg, Ctx, Flag, RegistrationError

SECRET = "s3cr3t-value"


@dataclass(frozen=True, slots=True)
class LoginArgs:
    account: str = Arg(description="Account name")
    pin: int = Flag(default=0, secret=True, description="Numeric PIN")
    api_key: str = Flag(default="", pattern=r"[a-f0-9]{8}", description="Hex key")
    author: int = Flag(default=0, secret=False, description="Author id; not a secret")
    retries: int = Flag(default=0, description="Plain integer")
    auth_debug: bool = Flag(default=False, description="Booleans are never secrets")


@dataclass(frozen=True, slots=True)
class PushArgs:
    token: str = Flag(description="Deploy token")


def secret_app() -> App:
    app = App("vaultctl", version="1")

    @app.command("login", description="Log in")
    def login(args: LoginArgs, ctx: Ctx) -> dict[str, object]:
        return {"account": args.account, "pin": args.pin, "api_key": args.api_key}

    @app.command("push", description="Push with a required token")
    def push(args: PushArgs, ctx: Ctx) -> dict[str, str]:
        return {"token_len": str(len(args.token))}

    return app


def run(
    argv: list[str], *, env: dict[str, str] | None = None, isatty: bool = False, stdin: str = ""
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = secret_app().run(
        argv, stdin=io.StringIO(stdin), stdout=out, stderr=err, env=env or {}, isatty=isatty
    )
    return code, out.getvalue(), err.getvalue()


def error_of(out: str) -> dict[str, object]:
    error = json.loads(out)["error"]
    assert isinstance(error, dict)
    return error


# --- direct values are refused, and never echoed -------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["login", "acme", "--pin", SECRET],
        ["login", "acme", f"--api-key={SECRET}"],
        ["login", "acme", f"--token={SECRET}"],  # unknown flag with an inline value
        [f"--token={SECRET}"],  # unknown root token with an inline value
        ["login", "acme", "--api-key-from-env", "K", "--api-key-from-file", SECRET],
    ],
)
def test_secret_values_never_reach_output(argv: list[str]) -> None:
    for isatty in (False, True):
        code, out, err = run(argv, isatty=isatty, env={"K": SECRET})
        assert code == 2 and SECRET not in out + err


def test_direct_secret_flag_is_refused_with_the_two_sources() -> None:
    _, out, _ = run(["login", "acme", "--pin", "1234"])
    error = error_of(out)
    assert error["phase"] == "validation"
    assert error["context"] == {"flag": "pin", "accepted": ["pin-from-env", "pin-from-file"]}
    assert error["suggestion"] == "pass --pin-from-env VAR_NAME or --pin-from-file PATH instead"
    _, out, _ = run(["login", "acme", "--api-key=abcd"])
    assert error_of(out)["context"]["flag"] == "api-key"


def test_unknown_inline_flag_keeps_only_the_name() -> None:
    _, out, _ = run(["login", "acme", f"--token={SECRET}"])
    error = error_of(out)
    assert error["message"] == "unknown flag '--token'" and error["context"]["flag"] == "token"


def test_non_secret_values_are_still_echoed() -> None:
    for flag in ("--author", "--retries"):
        _, out, _ = run(["login", "acme", flag, "abc"])
        assert error_of(out)["context"]["value"] == "abc"


# --- env and file sources -----------------------------------------------------------------


def test_from_env_reads_the_variable_and_coerces() -> None:
    code, out, _ = run(
        ["login", "acme", "--pin-from-env", "MY_PIN", "--api-key-from-env=KEY"],
        env={"MY_PIN": "4321", "KEY": "deadbeef"},
    )
    assert code == 0
    assert json.loads(out)["data"] == {"account": "acme", "pin": 4321, "api_key": "deadbeef"}


def test_from_file_reads_the_file_and_strips_one_newline(tmp_path: Path) -> None:
    (tmp_path / "pin").write_text("77\n")
    code, out, _ = run(["login", "acme", "--pin-from-file", str(tmp_path / "pin")])
    assert code == 0 and json.loads(out)["data"]["pin"] == 77


def test_default_variable_is_read_when_no_source_is_named() -> None:
    code, out, _ = run(["login", "acme"], env={"VAULTCTL_PIN": "9", "VAULTCTL_API_KEY": "abcd1234"})
    assert code == 0 and json.loads(out)["data"] == {
        "account": "acme",
        "pin": 9,
        "api_key": "abcd1234",
    }
    code, out, _ = run(["push"], env={"VAULTCTL_TOKEN": SECRET})
    assert code == 0 and json.loads(out)["data"] == {"token_len": str(len(SECRET))}


def test_missing_env_var_or_file_fails_validation(tmp_path: Path) -> None:
    _, out, _ = run(["login", "acme", "--pin-from-env", "NOPE"])
    error = error_of(out)
    assert error["code"] == "ARG_ERROR" and error["phase"] == "validation"
    assert error["context"] == {"flag": "pin-from-env", "var": "NOPE"}
    _, out, _ = run(["login", "acme", "--pin-from-env", "EMPTY"], env={"EMPTY": ""})
    assert error_of(out)["context"] == {"flag": "pin-from-env", "var": "EMPTY"}
    missing = str(tmp_path / "absent")
    _, out, _ = run(["login", "acme", "--pin-from-file", missing])
    assert error_of(out)["context"] == {"flag": "pin-from-file", "path": missing}
    (tmp_path / "empty").write_text("\n")
    _, out, _ = run(["login", "acme", "--pin-from-file", str(tmp_path / "empty")])
    assert "is empty" in str(error_of(out)["message"])


def test_secret_file_paths_are_hardened() -> None:
    _, out, _ = run(["login", "acme", "--pin-from-file", "../pin"])
    assert error_of(out)["context"]["rejected_pattern"] == "path_traversal"


def test_required_secret_names_its_env_flag_when_missing() -> None:
    code, out, _ = run(["push"])
    assert code == 2 and error_of(out)["context"]["missing"] == ["token-from-env"]


def test_resolved_secret_still_goes_through_pattern_and_redaction() -> None:
    _, out, _ = run(["login", "acme", "--api-key-from-env", "K"], env={"K": SECRET})
    error = error_of(out)
    assert SECRET not in out and error["context"]["value"] == "[REDACTED]"
    assert error["context"]["pattern"] == "[a-f0-9]{8}"


# --- exec and raw payload -----------------------------------------------------------------


def test_exec_accepts_sources_and_refuses_direct_values() -> None:
    line = json.dumps({"_cmd": "login", "account": "acme", "pin_from_env": "P"})
    code, out, _ = run(["exec"], stdin=line + "\n", env={"P": "5"})
    assert code == 0 and json.loads(out)["data"]["pin"] == 5
    line = json.dumps({"_cmd": "login", "account": "acme", "pin": SECRET})
    code, out, _ = run(["exec"], stdin=line + "\n")
    assert code == 1 and SECRET not in out
    assert error_of(out)["context"]["accepted"] == ["pin-from-env", "pin-from-file"]


# --- manifest and help --------------------------------------------------------------------


def test_manifest_lists_sources_and_secret_env_vars() -> None:
    _, out, _ = run(["manifest"])
    manifest = json.loads(out)["data"]
    spec_validator("manifest-response").validate(manifest)
    login = manifest["commands"]["login"]
    flags = login["flags"]
    assert "pin" not in flags and "api-key" not in flags
    assert flags["pin-from-env"] == {
        "type": "string",
        "required": False,
        "description": "Name of the environment variable holding: Numeric PIN",
    }
    assert flags["pin-from-file"]["pattern_type"] == "filepath"
    assert login["secret_env_vars"] == ["VAULTCTL_PIN", "VAULTCTL_API_KEY"]
    assert "auth-debug" in flags and "secret_env_vars" not in manifest["commands"]["version"]


def test_help_shows_sources_and_default_variable() -> None:
    _, out, _ = run(["push", "--help"], isatty=True)
    assert "--token-from-env VAR" in out and "--token-from-file PATH" in out
    assert "$VAULTCTL_TOKEN" in out and "(required)" in out


# --- registration ---------------------------------------------------------------------------


def test_secret_positional_is_a_registration_error() -> None:
    @dataclass(frozen=True, slots=True)
    class Positional:
        token: str = Arg(description="Token")

    app = App("x", version="1")
    with pytest.raises(RegistrationError, match="cannot be positional"):

        @app.command("go", description="Go")
        def go(args: Positional, ctx: Ctx) -> None:
            return None


def test_secret_bool_array_or_short_is_a_registration_error() -> None:
    @dataclass(frozen=True, slots=True)
    class Flagged:
        debug: bool = Flag(default=False, secret=True, description="No")

    @dataclass(frozen=True, slots=True)
    class Many:
        tokens: tuple[str, ...] = Flag(default=(), description="No")

    @dataclass(frozen=True, slots=True)
    class Short:
        token: str = Flag(default="", short="t", description="No")

    for args_type, match in ((Flagged, "boolean"), (Many, "array"), (Short, "short flag")):
        app = App("x", version="1")
        with pytest.raises(RegistrationError, match=match):
            app.command("go", description="Go")(_handler_for(args_type))


def _handler_for(args_type: type):  # type: ignore[no-untyped-def]
    def go(args, ctx: Ctx) -> None:  # type: ignore[no-untyped-def]
        return None

    go.__annotations__ = {"args": args_type, "ctx": Ctx, "return": None}
    return go
