"""``--format ndjson``: ``data`` alone as JSON lines, status on stderr and the exit code (#34)"""

import io
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from treaty import App, Ctx, Exit, Format, NoArgs, Page, ParseError, RegistrationError


@dataclass(frozen=True, slots=True)
class Sec:
    ticker: str
    isin: str


SECS = [Sec("VWRL", "IE00B3RBWM25"), Sec("LYXGOLD", "LU1900066033")]


def make_app() -> App:
    app = App("probe", version="1.0.0")

    @app.command("list", description="Every security", danger_level="safe", exit_codes=())
    def list_(args: NoArgs, ctx: Ctx) -> list[Sec]:
        return SECS

    @app.command("one", description="One security", danger_level="safe", exit_codes=())
    def one(args: NoArgs, ctx: Ctx) -> Sec:
        return SECS[0]

    @app.command("nothing", description="No data", danger_level="safe", exit_codes=())
    def nothing(args: NoArgs, ctx: Ctx) -> None:
        return None

    @app.command("empty", description="No rows", danger_level="safe", exit_codes=())
    def empty(args: NoArgs, ctx: Ctx) -> list[Sec]:
        return []

    @app.command("missing", description="Fail", exit_codes=["NOT_FOUND"], danger_level="safe")
    def missing(args: NoArgs, ctx: Ctx) -> Sec:
        raise Exit.NOT_FOUND("no such security")

    @app.command("login", description="Fail with a secret", danger_level="safe", exit_codes=())
    def login(args: NoArgs, ctx: Ctx) -> Sec:
        raise ParseError("bad login", context={"Password": "hunter2", "user": "alice"})

    @app.command("warned", description="Warn", danger_level="safe", exit_codes=())
    def warned(args: NoArgs, ctx: Ctx) -> Sec:
        ctx.warn("STALE_QUOTE", "the quote is a day old", ticker="VWRL")
        return SECS[0]

    @app.command("token", description="A credential", danger_level="safe", exit_codes=())
    def token(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"token": "ghp_abc123456789abcdef", "user": "alice"}

    @app.command(
        "tick", description="Securities", streaming=True, danger_level="safe", exit_codes=()
    )
    def tick(args: NoArgs, ctx: Ctx) -> Iterator[Sec]:
        ctx.log("starting", count=2)
        yield from SECS

    @app.command(
        "noted", description="Warns mid-stream", streaming=True, danger_level="safe", exit_codes=()
    )
    def noted(args: NoArgs, ctx: Ctx) -> Iterator[Sec]:
        yield SECS[0]
        ctx.warn("STALE_QUOTE", "the quote is a day old")
        yield SECS[1]

    @app.command(
        "pairs", description="Lists as events", streaming=True, danger_level="safe", exit_codes=()
    )
    def pairs(args: NoArgs, ctx: Ctx) -> Iterator[list[int]]:
        yield [1, 2]
        yield [3]

    @app.command(
        "broken",
        description="Fails mid-stream",
        streaming=True,
        danger_level="safe",
        exit_codes=["NOT_FOUND"],
    )
    def broken(args: NoArgs, ctx: Ctx) -> Iterator[Sec]:
        yield SECS[0]
        raise Exit.NOT_FOUND("the feed ended")

    @app.command(
        "paged",
        description="A page",
        danger_level="safe",
        exit_codes=(),
        paginated=True,
        ordered=True,
    )
    def paged(args: NoArgs, ctx: Ctx) -> Page[Sec]:
        return Page(items=SECS[:1], next_cursor="1", total=2)

    return app


def run(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = make_app().run(argv, stdout=out, stderr=err, env=env or {}, isatty=False)
    return code, out.getvalue(), err.getvalue()


def lines(text: str) -> list[object]:
    return [json.loads(line) for line in text.splitlines()]


def test_a_list_writes_one_compact_sorted_line_per_item() -> None:
    code, out, err = run(["list", "--format", "ndjson"])
    assert code == 0 and err == ""
    assert out == (
        '{"isin":"IE00B3RBWM25","ticker":"VWRL"}\n{"isin":"LU1900066033","ticker":"LYXGOLD"}\n'
    )


def test_a_single_result_is_one_line_and_no_data_is_no_line() -> None:
    assert run(["one", "--format", "ndjson"]) == (
        0,
        '{"isin":"IE00B3RBWM25","ticker":"VWRL"}\n',
        "",
    )
    assert run(["nothing", "--format", "ndjson"]) == (0, "", "")
    assert run(["empty", "--format", "ndjson"]) == (0, "", "")


def test_a_stream_writes_one_line_per_event_with_or_without_buffering() -> None:
    expected = [{"isin": s.isin, "ticker": s.ticker} for s in SECS]
    code, out, _ = run(["tick", "--format", "ndjson"])
    assert code == 0 and lines(out) == expected
    code, out, _ = run(["tick", "--format", "ndjson", "--no-stream"])
    assert code == 0 and lines(out) == expected


def test_an_event_that_is_a_list_stays_one_line() -> None:
    assert run(["pairs", "--format", "ndjson"]) == (0, "[1,2]\n[3]\n", "")
    assert run(["pairs", "--format", "ndjson", "--no-stream"]) == (0, "[1,2]\n[3]\n", "")


def test_an_error_is_one_json_line_on_stderr_and_the_exit_code() -> None:
    code, out, err = run(["missing", "--format", "ndjson"])
    assert code == 5 and out == ""
    [line] = lines(err)
    assert isinstance(line, dict) and line["error"]["code"] == "NOT_FOUND"
    assert line["error"]["message"] == "No such security."


def test_an_argument_error_is_a_json_line_too() -> None:
    code, out, err = run(["list", "--bogus", "--format", "ndjson"])
    assert code == 2 and out == ""
    [line] = lines(err)
    assert isinstance(line, dict) and line["error"]["code"] == "ARG_ERROR"


def test_the_error_line_redacts_credential_named_context() -> None:
    _, _, err = run(["login", "--format", "ndjson"])
    [line] = lines(err)
    assert isinstance(line, dict)
    assert line["error"]["context"] == {"Password": "[REDACTED]", "user": "alice"}
    assert "hunter2" not in err


def test_a_failing_stream_keeps_its_records_and_reports_on_stderr() -> None:
    code, out, err = run(["broken", "--format", "ndjson"])
    assert code == 5 and lines(out) == [{"isin": "IE00B3RBWM25", "ticker": "VWRL"}]
    [line] = lines(err)
    assert isinstance(line, dict) and line["error"]["code"] == "NOT_FOUND"


def test_warnings_go_to_stderr_as_json_lines() -> None:
    code, out, err = run(["warned", "--format", "ndjson"])
    assert code == 0 and lines(out) == [{"isin": "IE00B3RBWM25", "ticker": "VWRL"}]
    [warning] = lines(err)
    assert isinstance(warning, dict) and warning["code"] == "STALE_QUOTE"


def test_a_stream_warning_is_written_once() -> None:
    code, out, err = run(["noted", "--format", "ndjson"])
    assert code == 0 and len(lines(out)) == 2
    assert [w["code"] for w in lines(err) if isinstance(w, dict)] == ["STALE_QUOTE"]


def test_a_stream_warning_given_twice_is_written_twice() -> None:
    app = App("probe", version="1.0.0")

    @app.command("skips", description="Skips", streaming=True, danger_level="safe", exit_codes=())
    def skips(args: NoArgs, ctx: Ctx) -> Iterator[Sec]:
        yield SECS[0]
        ctx.warn("ROW_SKIPPED", "a row was skipped")
        yield SECS[1]
        ctx.warn("ROW_SKIPPED", "a row was skipped")

    out, err = io.StringIO(), io.StringIO()
    code = app.run(["skips", "--format", "ndjson"], stdout=out, stderr=err, env={}, isatty=False)
    # The envelope carries both, so the stderr lines do too
    assert code == 0 and len(lines(out.getvalue())) == 2
    assert [w["code"] for w in lines(err.getvalue()) if isinstance(w, dict)] == [
        "ROW_SKIPPED",
        "ROW_SKIPPED",
    ]


def test_logs_are_json_lines_on_stderr() -> None:
    code, out, err = run(["tick", "--format", "ndjson", "--verbose"])
    assert code == 0 and len(lines(out)) == 2
    [log] = lines(err)
    assert log == {"fields": {"count": 2}, "level": "info", "message": "starting"}


def test_a_cut_page_names_the_way_on_as_a_json_line() -> None:
    code, out, err = run(["paged", "--format", "ndjson"])
    assert code == 0 and lines(out) == [{"isin": "IE00B3RBWM25", "ticker": "VWRL"}]
    [note] = lines(err)
    assert isinstance(note, dict) and note["pagination"]["has_more"] is True
    _, envelope, _ = run(["paged", "--format", "json"])
    assert note["pagination"] == json.loads(envelope)["meta"]["pagination"]


def test_fields_and_masking_apply_as_in_the_envelope() -> None:
    _, out, _ = run(["list", "--format", "ndjson", "--fields", "ticker"])
    assert lines(out) == [{"ticker": "VWRL"}, {"ticker": "LYXGOLD"}]
    _, out, _ = run(["token", "--format", "ndjson"])
    _, envelope, _ = run(["token", "--format", "json"])
    assert lines(out) == [json.loads(envelope)["data"]]
    assert "ghp_abc123456789abcdef" not in out


def test_stable_output_leaves_the_records_alone() -> None:
    _, out, _ = run(["list", "--format", "ndjson", "--stable-output"])
    assert out == run(["list", "--format", "ndjson"])[1]


def test_the_format_variable_selects_ndjson() -> None:
    code, out, _ = run(["one"], env={"PROBE_FORMAT": "ndjson"})
    assert code == 0 and lines(out) == [{"isin": "IE00B3RBWM25", "ticker": "VWRL"}]


def test_jsonl_stays_the_envelope_stream() -> None:
    _, out, _ = run(["tick", "--format", "jsonl"])
    envelopes = lines(out)
    assert all(isinstance(e, dict) and "meta" in e for e in envelopes)
    assert len(envelopes) == 3  # two events and the terminal envelope


def test_help_writes_nothing_on_stdout() -> None:
    code, out, err = run(["list", "--help", "--format", "ndjson"])
    assert code == 0 and out == "" and "Every security" in err


def test_the_manifest_and_a_schema_are_one_line_each() -> None:
    code, out, _ = run(["manifest", "--format", "ndjson"])
    [manifest] = lines(out)
    assert code == 0 and isinstance(manifest, dict)
    assert "ndjson" in manifest["flags"]["format"]["enum_values"]
    code, out, _ = run(["one", "--schema", "--format", "ndjson"])
    [schema] = lines(out)
    assert code == 0 and isinstance(schema, dict) and schema["description"] == "One security"


def test_ndjson_takes_no_renderer() -> None:
    app = App("probe", version="1.0.0")
    with pytest.raises(RegistrationError, match="ndjson writes data as JSON lines"):
        app.format(Format.NDJSON, render=lambda data: "")
    with pytest.raises(RegistrationError, match="ndjson writes data as JSON lines"):

        @app.command(
            "x",
            description="X",
            danger_level="safe",
            exit_codes=(),
            renderers={Format.NDJSON: lambda data: ""},
        )
        def x(args: NoArgs, ctx: Ctx) -> None:
            return None


def test_output_refuses_the_format_name() -> None:
    app = App("probe", version="1.0.0")

    @app.command("rows", description="Rows", danger_level="safe", exit_codes=(), output_file=True)
    def rows(args: NoArgs, ctx: Ctx) -> list[Sec]:
        return SECS

    out = io.StringIO()
    code = app.run(
        ["rows", "--output", "ndjson"], stdout=out, stderr=io.StringIO(), env={}, isatty=False
    )
    error = json.loads(out.getvalue())["error"]
    assert code == 2 and "--format ndjson" in error["errors"][0]["suggestion"]
    assert not Path("ndjson").exists()


# --max-output (REQ-F-052, REQ-F-064): a buffered answer keeps whole records up to the
# cap; a stream caps each record, as jsonl caps each envelope, and goes on

PADDED = [{"n": i, "pad": "x" * 1000} for i in range(10)]
"""Each line is 1017 bytes, so four fit the smallest cap, 4096"""
HUGE = {"pad": "x" * 5000}


def capped_app() -> App:
    app = App("probe", version="1.0.0")

    @app.command("padded", description="Big rows", danger_level="safe", exit_codes=())
    def padded(args: NoArgs, ctx: Ctx) -> list[dict[str, object]]:
        return PADDED

    @app.command(
        "flow", description="Big events", streaming=True, danger_level="safe", exit_codes=()
    )
    def flow(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, object]]:
        yield from PADDED

    @app.command(
        "spiky", description="A huge event", streaming=True, danger_level="safe", exit_codes=()
    )
    def spiky(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, object]]:
        yield {"n": 1}
        yield HUGE
        yield {"n": 3}

    @app.command("huge", description="One big row", danger_level="safe", exit_codes=())
    def huge(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return HUGE

    return app


def run_capped(argv: list[str]) -> tuple[int, str, list[dict[str, object]]]:
    out, err = io.StringIO(), io.StringIO()
    code = capped_app().run(
        [*argv, "--format", "ndjson", "--max-output", "4096"],
        stdout=out,
        stderr=err,
        env={},
        isatty=False,
    )
    return code, out.getvalue(), [json.loads(line) for line in err.getvalue().splitlines()]


@pytest.mark.parametrize("argv", [["padded"], ["flow", "--no-stream"]])
def test_a_buffered_answer_keeps_whole_records_up_to_the_cap(argv: list[str]) -> None:
    code, out, err = run_capped(argv)
    assert code == 0 and lines(out) == PADDED[:4] and len(out.encode()) <= 4096
    [truncation, warning] = err
    hint = f"probe {' '.join(argv)} --format ndjson --max-output 11194"
    assert truncation["truncation"] == {
        "truncated": True,
        "total_bytes": 10170,
        "returned_bytes": 4068,
        "total_count": 10,
        "returned_count": 4,
        "omitted_count": 6,
        "max_output_bytes": 4096,
        "truncation_hint": hint,
    }
    assert warning == {
        "code": "FIELD_TRUNCATED",
        "message": "data cut from 10 to 4",
        "context": {"field": "data", "original_length": 10, "truncated_length": 4},
    }


def test_a_stream_caps_each_record_so_it_never_goes_silent() -> None:
    code, out, err = run_capped(["flow"])
    assert code == 0 and lines(out) == PADDED and err == []


def test_a_stream_record_over_the_cap_is_left_out_and_the_stream_goes_on() -> None:
    code, out, err = run_capped(["spiky"])
    assert code == 0 and lines(out) == [{"n": 1}, {"n": 3}]
    [truncation, warning] = err
    assert truncation["truncation"] == {
        "truncated": True,
        "seq": 2,
        "total_bytes": 5011,
        "returned_bytes": 0,
        "total_count": 1,
        "returned_count": 0,
        "omitted_count": 1,
        "max_output_bytes": 4096,
        "truncation_hint": "probe spiky --format ndjson --max-output 6035",
    }
    assert warning == {
        "code": "FIELD_TRUNCATED",
        "message": "event 2 left out: 5011 bytes, over the 4096-byte cap",
        "context": {"field": "data", "seq": 2, "original_length": 5011, "truncated_length": 0},
    }


def test_a_record_larger_than_the_cap_writes_nothing() -> None:
    code, out, err = run_capped(["huge"])
    assert code == 0 and out == ""
    truncation = err[0]["truncation"]
    assert isinstance(truncation, dict)
    assert truncation["returned_count"] == 0 and truncation["omitted_count"] == 1
    assert [e.get("code") for e in err[1:]] == ["FIELD_TRUNCATED"]


@pytest.mark.parametrize(("command", "written"), [("padded", 4), ("spiky", 2)])
def test_a_cut_fails_the_run_under_warnings_as_errors(command: str, written: int) -> None:
    code, out, err = run_capped([command, "--warnings-as-errors"])
    assert code == 1 and len(lines(out)) == written
    error = err[-1]["error"]
    assert isinstance(error, dict) and error["code"] == "WARNINGS_AS_ERRORS"


def test_records_under_the_cap_are_not_reported() -> None:
    out, err = io.StringIO(), io.StringIO()
    code = capped_app().run(
        ["padded", "--format", "ndjson"], stdout=out, stderr=err, env={}, isatty=False
    )
    assert code == 0 and lines(out.getvalue()) == PADDED and err.getvalue() == ""
