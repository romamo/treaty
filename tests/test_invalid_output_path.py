"""#330: INVALID_OUTPUT names the field of the result that has no JSON form, in its message
and in ``context.path``, spelled as the audit spells a field, with indexes"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from treaty import App, Batch, Ctx, Flag, NoArgs, SchemaError
from treaty import Item as BatchItem
from treaty._schema import ScalarRegistry, to_jsonable, value_path


def run(
    app: App, argv: list[str], env: dict[str, str] | None = None
) -> tuple[int, list[dict[str, object]]]:
    out = io.StringIO()
    code = app.run(argv, stdin=io.StringIO(), stdout=out, stderr=io.StringIO(), env=env or {})
    return code, [json.loads(line) for line in out.getvalue().splitlines()]


def failure(app: App, argv: list[str]) -> tuple[str, dict[str, object]]:
    code, envelopes = run(app, argv)
    error = envelopes[-1]["error"]
    assert code == 1 and isinstance(error, dict) and error["code"] == "INVALID_OUTPUT"
    return str(error["message"]), error["context"]


@dataclass(frozen=True, slots=True)
class Bracket:
    lo: float
    hi: float


@dataclass(frozen=True, slots=True)
class Login:
    token: str = Flag(description="API token")


def test_a_non_finite_float_names_its_path() -> None:
    app = App("taxctl", version="1.0.0")

    @app.command("x", description="X", danger_level="safe", exit_codes=())
    def x(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"brackets": [{"hi": 1.0}, {"hi": float("inf")}]}

    message, context = failure(app, ["x"])
    assert context == {"command": "x", "path": "brackets[1].hi"}
    assert message.startswith("Command x returned at brackets[1].hi: inf is not a finite")


def test_the_top_of_a_list_starts_with_its_index() -> None:
    app = App("taxctl", version="1.0.0")

    @app.command("x", description="X", danger_level="safe", exit_codes=())
    def x(args: NoArgs, ctx: Ctx) -> list[Bracket]:
        return [Bracket(0, 1), Bracket(1, 2), Bracket(2, float("nan"))]

    _, context = failure(app, ["x"])
    assert context["path"] == "[2].hi"


def test_each_value_check_names_its_path() -> None:
    app = App("taxctl", version="1.0.0")

    @app.command("big", description="Big", danger_level="safe", exit_codes=())
    def big(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"rows": [{"n": 1}, {"n": 10**100_000}]}

    @app.command("opaque", description="Opaque", danger_level="safe", exit_codes=())
    def opaque(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"rows": [{"n": object()}]}

    @app.command("keys", description="Keys", danger_level="safe", exit_codes=())
    def keys(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"rows": [{1: "a"}]}

    assert failure(app, ["big"])[1]["path"] == "rows[1].n"
    assert failure(app, ["opaque"])[1]["path"] == "rows[0].n"
    assert failure(app, ["keys"])[1]["path"] == "rows[0]"


def test_a_key_with_a_dot_or_a_bracket_is_quoted() -> None:
    app = App("taxctl", version="1.0.0")

    @app.command("x", description="X", danger_level="safe", exit_codes=())
    def x(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"a.b": {"c[0]": [{"": float("inf")}]}}

    _, context = failure(app, ["x"])
    assert context["path"] == '["a.b"]["c[0]"][0][""]'


def test_a_root_failure_has_no_path() -> None:
    app = App("taxctl", version="1.0.0")

    @app.command("x", description="X", danger_level="safe", exit_codes=())
    def x(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return object()  # type: ignore[return-value]

    message, context = failure(app, ["x"])
    assert context == {"command": "x"} and " at " not in message.split(";")[0]


def test_the_path_names_keys_never_values_and_secrets_stay_redacted() -> None:
    app = App("taxctl", version="1.0.0")

    @app.command("login", description="Login", danger_level="safe", exit_codes=())
    def login(args: Login, ctx: Ctx) -> dict[str, object]:
        return {"rows": [{"token": args.token, "score": float("inf")}]}

    code, [env] = run(app, ["login", "--token-from-env", "K"], env={"K": "s3cr3t-value-42"})
    assert code == 1 and "s3cr3t" not in json.dumps(env)
    error = env["error"]
    assert isinstance(error, dict) and error["context"]["path"] == "rows[0].score"


def test_an_unsorted_list_and_a_batch_name_where_the_item_is() -> None:
    app = App("taxctl", version="1.0.0")

    @app.command("items", description="Items", danger_level="safe", exit_codes=(), paginated=True)
    def items(args: NoArgs, ctx: Ctx) -> list[Bracket]:
        return [Bracket(0, 1), Bracket(0, float("inf"))]

    @app.command("make", description="Make", danger_level="mutating", exit_codes=())
    def make(args: NoArgs, ctx: Ctx) -> Batch[dict[str, object]]:
        return Batch(
            [
                BatchItem(1, {"effect": "created", "hi": 1.0}),
                BatchItem(2, {"effect": "created", "hi": float("inf")}),
            ]
        )

    assert failure(app, ["items"])[1]["path"] == "[1].hi"
    assert failure(app, ["make"])[1]["path"] == "results[1].hi"


def test_a_streamed_event_names_its_path() -> None:
    app = App("taxctl", version="1.0.0")

    @app.command("watch", description="Watch", danger_level="safe", exit_codes=(), streaming=True)
    def watch(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, object]]:
        yield {"tick": [1.0, float("-inf")]}

    message, context = failure(app, ["watch"])
    assert context["path"] == "tick[1]" and "yielded at tick[1]:" in message


@pytest.mark.parametrize(
    ("at", "spelled"),
    [
        ((), None),
        (("brackets", 1, "hi"), "brackets[1].hi"),
        ((2, "hi"), "[2].hi"),
        (("a b", 'q"'), '["a b"]["q\\""]'),
        (("content-type",), "content-type"),
    ],
)
def test_value_path_spelling(at: tuple[str | int, ...], spelled: str | None) -> None:
    assert value_path(at) == spelled


def test_the_converter_carries_the_parts() -> None:
    with pytest.raises(SchemaError) as caught:
        to_jsonable({"a": [Bracket(0, float("nan"))]}, ScalarRegistry(), base=Path("/"))
    assert caught.value.at == ("a", 0, "hi")
