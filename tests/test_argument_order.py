"""REQ-F-067 and REQ-F-079: global options anywhere, conflicting repeats exit 2"""

import io
import json
from dataclasses import dataclass

import pytest

from treaty import App, Ctx, Flag, RegistrationError

ROLLBACK = ["deploy", "rollback", "api", "--dry-run"]


def run(app: App, argv: list[str]) -> tuple[int, dict]:
    out = io.StringIO()
    code = app.run(argv, stdout=out, stderr=io.StringIO(), env={}, isatty=False)
    return code, json.loads(out.getvalue())


@pytest.mark.parametrize(
    "argv",
    [
        ["--format", "json", *ROLLBACK, "--format", "human"],
        [*ROLLBACK, "--format=json", "--format", "human"],
        ["--max-output", "5000", *ROLLBACK, "--max-output=6000"],
    ],
)
def test_conflicting_global_repeat_is_arg_error(app: App, argv: list[str]) -> None:
    code, env = run(app, argv)
    assert code == 2 and env["error"]["code"] == "ARG_ERROR"


def test_identical_global_repeat_is_accepted(app: App) -> None:
    code, env = run(app, ["--format", "json", *ROLLBACK, "--format=json"])
    assert code == 0 and env["data"]["service"] == "api"


@pytest.mark.parametrize(
    ("extra", "flag"),
    [
        (["--strategy", "fast", "--strategy", "safe"], "strategy"),
        (["--timeout", "5", "--timeout", "6"], "timeout"),
        (["--no-dry-run"], "dry-run"),
    ],
)
def test_conflicting_local_repeat_is_arg_error(app: App, extra: list[str], flag: str) -> None:
    code, env = run(app, [*ROLLBACK, *extra])
    assert code == 2 and env["error"]["context"]["flag"] == flag


def test_identical_local_repeat_is_accepted(app: App) -> None:
    code, env = run(app, [*ROLLBACK, "--strategy", "fast", "--strategy=fast", "--dry-run"])
    assert code == 0 and env["data"]["strategy"] == "fast"


def test_negated_boolean_with_value_is_inverted(app: App) -> None:
    code, env = run(
        app, ["deploy", "rollback", "api", "--no-dry-run=true", "--confirm-destructive"]
    )
    assert code == 0 and env["data"]["dry_run"] is False


def test_manifest_lists_global_options_at_the_root(app: App) -> None:
    _, env = run(app, ["manifest"])
    manifest = env["data"]
    assert set(manifest["flags"]) == {"format", "max-output", "schema", "help"}
    assert manifest["flags"]["format"]["enum_values"] == ["human", "json"]
    for entry in manifest["commands"].values():
        assert not set(entry["flags"]) & set(manifest["flags"])


def test_short_alias_of_a_global_option_is_rejected() -> None:
    app = App("t", version="1.0", description="t")

    @dataclass(frozen=True, slots=True)
    class Host:
        host: str = Flag(default="localhost", description="Host", short="h")

    with pytest.raises(RegistrationError, match="global options"):

        @app.command("connect", description="Connect")
        def connect(args: Host, ctx: Ctx) -> dict[str, str]:
            return {"host": args.host}
