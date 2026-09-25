import io
import json
from dataclasses import dataclass

import pytest

from treaty import App, Arg, Ctx, Flag

SECRET = "s3cr3t-value"


@dataclass(frozen=True, slots=True)
class LoginArgs:
    account: str = Arg(description="Account name")
    pin: int = Flag(default=0, secret=True, description="Numeric PIN")
    api_key: str = Flag(default="", pattern=r"[a-f0-9]{8}", description="Hex key")
    author: int = Flag(default=0, secret=False, description="Author id; not a secret")
    retries: int = Flag(default=0, description="Plain integer")


def secret_app() -> App:
    app = App("vaultctl", version="1")

    @app.command("login", description="Log in")
    def login(args: LoginArgs, ctx: Ctx) -> dict[str, str]:
        return {"account": args.account}

    return app


def run(argv: list[str], *, isatty: bool = False, stdin: str = "") -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = secret_app().run(
        argv, stdin=io.StringIO(stdin), stdout=out, stderr=err, env={}, isatty=isatty
    )
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize(
    "argv",
    [
        ["login", "acme", "--pin", SECRET],  # declared secret, fails integer coercion
        ["login", "acme", f"--api-key={SECRET}"],  # inferred from the name, fails its pattern
        ["login", "acme", f"--token={SECRET}"],  # unknown flag with an inline value
        [f"--token={SECRET}"],  # unknown root token with an inline value
    ],
)
def test_secret_values_never_reach_output(argv: list[str]) -> None:
    for isatty in (False, True):
        code, out, err = run(argv, isatty=isatty)
        assert code == 2 and SECRET not in out + err


def test_declared_and_inferred_secrets_are_redacted_in_context() -> None:
    _, out, _ = run(["login", "acme", "--pin", SECRET])
    assert json.loads(out)["error"]["context"] == {"flag": "pin", "value": "[REDACTED]"}
    _, out, _ = run(["login", "acme", "--api-key", SECRET])
    assert json.loads(out)["error"]["context"]["value"] == "[REDACTED]"


def test_unknown_inline_flag_keeps_only_the_name() -> None:
    _, out, _ = run(["login", "acme", f"--token={SECRET}"])
    error = json.loads(out)["error"]
    assert error["message"] == "unknown flag '--token'" and error["context"]["flag"] == "token"


def test_non_secret_values_are_still_echoed() -> None:
    for flag in ("--author", "--retries"):
        _, out, _ = run(["login", "acme", flag, "abc"])
        assert json.loads(out)["error"]["context"]["value"] == "abc"


def test_exec_payload_secrets_are_redacted() -> None:
    line = json.dumps({"_cmd": "login", "account": "acme", "pin": SECRET})
    code, out, _ = run(["exec"], stdin=line + "\n")
    assert code == 1 and SECRET not in out
    assert json.loads(out)["error"]["context"]["value"] == "[REDACTED]"
