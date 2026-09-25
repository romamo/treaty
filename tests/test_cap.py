import io
import json
from dataclasses import dataclass

import pytest
from conftest import spec_validator

from treaty import App, Ctx, Flag, NoArgs, RegistrationError
from treaty._cap import MARKER, MIN_BYTES


def big_app() -> App:
    app = App("bigctl", version="1", max_output_bytes=MIN_BYTES)

    @app.command("items", description="Many small items")
    def items(args: NoArgs, ctx: Ctx) -> list[dict[str, object]]:
        return [{"id": i, "name": f"item-{i}"} for i in range(1000)]

    @app.command("log", description="One record with a huge field")
    def log(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"records": [{"id": 1, "log": "x" * 50_000}], "source": "api"}

    @app.command("table", description="A wide object with no list or long string")
    def table(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"name": "t", "rows": {f"k{i}": {"v": i} for i in range(2000)}}

    @app.command("small", description="Fits easily")
    def small(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"ok": True}

    return app


def run(
    app: App, argv: list[str], *, env: dict[str, str] | None = None, stdin: str = ""
) -> tuple[int, str]:
    out = io.StringIO()
    code = app.run(
        argv,
        stdin=io.StringIO(stdin),
        stdout=out,
        stderr=io.StringIO(),
        env=env or {},
        isatty=False,
    )
    return code, out.getvalue()


def envelope(text: str) -> dict:
    env = json.loads(text)
    spec_validator("response-envelope").validate(env)
    return env


def test_under_cap_is_untouched() -> None:
    code, out = run(big_app(), ["small"])
    env = envelope(out)
    assert code == 0 and env["data"] == {"ok": True}
    assert "truncated" not in env["meta"] and env["warnings"] == []


def test_list_is_cut_to_the_longest_prefix_that_fits() -> None:
    code, out = run(big_app(), ["items"])
    env = envelope(out)
    assert code == 0 and len(out.rstrip("\n").encode()) <= MIN_BYTES
    meta = env["meta"]
    assert meta["truncated"] is True and meta["total_count"] == 1000
    assert meta["returned_count"] == len(env["data"]) > 1
    assert env["data"] == [{"id": i, "name": f"item-{i}"} for i in range(len(env["data"]))]
    assert meta["truncation_hint"] == (
        f"rerun with --max-output {meta['total_bytes']} "
        f"or TREATY_MAX_OUTPUT_BYTES={meta['total_bytes']}"
    )
    (warning,) = env["warnings"]
    assert warning["code"] == "FIELD_TRUNCATED" and warning["context"]["field"] == "$"


def test_hint_returns_the_full_response() -> None:
    _, out = run(big_app(), ["items"])
    total = envelope(out)["meta"]["total_bytes"]
    code, out = run(big_app(), ["items", "--max-output", str(total)])
    env = envelope(out)
    assert code == 0 and len(env["data"]) == 1000 and "truncated" not in env["meta"]


def test_one_huge_item_keeps_the_item_and_cuts_its_field() -> None:
    _, out = run(big_app(), ["log"])
    env = envelope(out)
    record = env["data"]["records"][0]
    assert record["id"] == 1 and env["data"]["source"] == "api"
    assert record["log"].endswith(MARKER) and len(record["log"]) < 50_000
    (warning,) = env["warnings"]
    assert warning["context"]["field"] == "$.records[0].log"
    assert warning["context"]["original_length"] == 50_000
    assert "total_count" not in env["meta"]  # data is an object, not a list


def test_wide_object_loses_trailing_keys() -> None:
    code, out = run(big_app(), ["table"])
    env = envelope(out)
    assert code == 0 and env["meta"]["truncated"] is True
    assert len(out.rstrip("\n").encode()) <= MIN_BYTES
    rows = env["data"]["rows"]
    assert env["data"]["name"] == "t" and set(rows) == {f"k{i}" for i in range(len(rows))}
    assert [w["context"]["field"] for w in env["warnings"]] == ["$.rows"]


def test_env_var_raises_the_cap_and_flag_overrides_it() -> None:
    raised = {"TREATY_MAX_OUTPUT_BYTES": "10000000"}
    _, out = run(big_app(), ["items"], env=raised)
    assert "truncated" not in envelope(out)["meta"]
    _, out = run(big_app(), ["items", f"--max-output={MIN_BYTES}"], env=raised)
    assert envelope(out)["meta"]["truncated"] is True


@pytest.mark.parametrize(
    ("argv", "env", "source"),
    [
        (["small", "--max-output", "lots"], {}, "--max-output"),
        (["small", "--max-output", "100"], {}, "--max-output"),
        (["small"], {"TREATY_MAX_OUTPUT_BYTES": "-1"}, "TREATY_MAX_OUTPUT_BYTES"),
    ],
)
def test_invalid_cap_is_arg_error(argv: list[str], env: dict[str, str], source: str) -> None:
    code, out = run(big_app(), argv, env=env)
    error = envelope(out)["error"]
    assert code == 2 and error["code"] == "ARG_ERROR" and error["context"]["source"] == source


def test_exec_caps_each_line() -> None:
    code, out = run(big_app(), ["exec"], stdin='{"_cmd": "items"}\n{"_cmd": "small"}\n')
    first, second = (envelope(line) for line in out.splitlines())
    assert code == 0
    assert first["meta"]["truncated"] is True and first["meta"]["_line"] == 1
    assert "truncated" not in second["meta"]


def test_human_mode_is_not_capped() -> None:
    out = io.StringIO()
    big_app().run(["items"], stdout=out, stderr=io.StringIO(), env={}, isatty=True)
    assert len(json.loads(out.getvalue())) == 1000


def test_command_flag_named_like_a_global_is_rejected() -> None:
    @dataclass(frozen=True, slots=True)
    class Args:
        max_output: int = Flag(default=0, description="Shadowed by the global flag")

    app = App("x", version="1")
    with pytest.raises(RegistrationError, match="max-output"):

        @app.command("go", description="Go")
        def go(args: Args, ctx: Ctx) -> dict[str, str]:
            return {}


def test_in_process_call_is_capped_like_stdout() -> None:
    envelope = big_app().call("items", {}, env={})
    assert envelope.extra_meta.get("truncated") is True
    assert {w.code for w in envelope.warnings} == {"FIELD_TRUNCATED"}
