import io
import json
from dataclasses import dataclass

import pytest

from treaty import App, Ctx, Flag

HINT = "use --format json (or --format human) to choose the output representation"


@dataclass(frozen=True, slots=True)
class ListArgs:
    limit: int = Flag(default=10, description="Maximum rows")


@dataclass(frozen=True, slots=True)
class ExportArgs:
    output: str = Flag(default="", description="File to write")


def hint_app() -> App:
    app = App("listctl", version="1")

    @app.command("list", description="List items")
    def list_(args: ListArgs, ctx: Ctx) -> dict[str, int]:
        return {"limit": args.limit}

    @app.command("export", description="Export items")
    def export(args: ExportArgs, ctx: Ctx) -> dict[str, str]:
        return {"output": args.output}

    return app


def run(argv: list[str]) -> tuple[int, dict]:
    out = io.StringIO()
    code = hint_app().run(argv, stdout=out, stderr=io.StringIO(), env={}, isatty=False)
    return code, json.loads(out.getvalue())


@pytest.mark.parametrize(
    "argv",
    [
        ["list", "--output", "json"],
        ["list", "--output=json"],
        ["list", "--output-format", "json"],
        ["list", "--json"],
        ["--output", "json", "list"],
        ["--json", "list"],
    ],
)
def test_guessed_representation_flags_point_to_format(argv: list[str]) -> None:
    code, env = run(argv)
    assert code == 2 and env["error"]["code"] == "ARG_ERROR"
    assert env["error"]["suggestion"] == HINT


def test_other_unknown_flags_get_no_hint() -> None:
    _, env = run(["list", "--verbose"])
    assert "suggestion" not in env["error"]


def test_command_owned_output_flag_is_not_hijacked() -> None:
    code, env = run(["export", "--output", "items.csv"])
    assert code == 0 and env["data"] == {"output": "items.csv"}
