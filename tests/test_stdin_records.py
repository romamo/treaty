"""Typed stdin records, ``stdin_records=Sec`` (#32): another command's output, unwrapped."""

import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from fixture_records_app import Sec, app

from treaty import App, Ctx, NoArgs, RegistrationError

SECCTL = Path(__file__).resolve().parent / "fixture_records_app.py"


def run(argv: list[str], stdin: str = "") -> tuple[int, list[dict]]:
    out = io.StringIO()
    code = app.run(
        [*argv, "--format", "json"],
        stdin=io.StringIO(stdin),
        stdout=out,
        stderr=io.StringIO(),
        env={},
        isatty=False,
    )
    return code, [json.loads(line) for line in out.getvalue().splitlines()]


def produce(argv: list[str]) -> str:
    out = io.StringIO()
    app.run([*argv, "--format", "json"], stdout=out, stderr=io.StringIO(), env={}, isatty=False)
    return out.getvalue()


def bare(*records: dict) -> str:
    return "".join(json.dumps(r) + "\n" for r in records)


VWRL = {"isin": "IE00B3RBWM25", "figi": "BBG000BDTF76", "ticker": "VWRL"}
GOLD = {"isin": "LU1900066033", "figi": "BBG00MGQZSP1", "ticker": "LYXGOLD"}


def test_a_streams_envelopes_are_unwrapped() -> None:
    # The repro of #32: the terminal envelope is not a third record
    stream = produce(["securities"])
    assert len(stream.splitlines()) == 3
    code, [envelope] = run(["summary"], stream)
    assert code == 0 and envelope["data"] == {"count": 2, "tickers": ["LYXGOLD", "VWRL"]}
    code, events = run(["resolve"], stream)
    assert code == 0 and [e["data"]["ticker"] for e in events[:-1]] == ["VWRL", "LYXGOLD"]


def test_bare_json_lines_are_records() -> None:
    # As --format ndjson writes them; blank lines and a missing default are fine
    code, [envelope] = run(["summary"], bare(VWRL) + "\n" + bare({**GOLD, "weight": 0.5}))
    assert code == 0 and envelope["data"]["tickers"] == ["LYXGOLD", "VWRL"]  # sorted
    code, [envelope] = run(["summary"], "")
    assert code == 0 and envelope["data"] == {"count": 0, "tickers": []}


def test_a_whole_response_gives_each_item_then_ends_the_input() -> None:
    code, [envelope] = run(["summary"], produce(["list"]) + "not read\n")
    assert code == 0 and envelope["data"]["count"] == 2
    assert envelope["warnings"] == []


def test_one_page_of_more_warns() -> None:
    code, [envelope] = run(["summary"], produce(["list", "--limit", "1"]))
    assert code == 0 and envelope["data"]["count"] == 1
    [warning] = envelope["warnings"]
    assert warning["code"] == "UPSTREAM_TRUNCATED"
    assert warning["context"] == {"line": 1, "has_more": True, "field_truncated": False}


def test_an_upstream_failure_fails_the_run_with_its_error() -> None:
    failed = produce(["securities", "--fail"])
    code, events = run(["resolve"], failed)
    assert events[0]["data"] == {"isin": "IE00B3RBWM25", "ticker": "VWRL"}
    error = events[-1]["error"]
    assert code == 1 and error["code"] == "UPSTREAM_FAILED"
    assert events[-1]["meta"]["partial"] is True
    context = error["context"]
    assert context["line"] == 2 and context["upstream_exit_code"] == 4
    assert context["upstream_command"] == "securities"
    upstream = context["upstream"]
    assert upstream["code"] == "FEED_GONE" and upstream["context"]["feed"] == "primary"
    assert "sk-live" not in json.dumps(events)  # a secret-named key is redacted
    code, [envelope] = run(["summary"], failed)
    assert code == 1 and envelope["error"]["code"] == "UPSTREAM_FAILED"


def test_a_stream_cut_before_its_terminal_envelope_is_not_success() -> None:
    # The producer was killed: its events arrived, its terminal envelope never did
    cut = "".join(produce(["securities"]).splitlines(keepends=True)[:2])
    code, [envelope] = run(["summary"], cut)
    error = envelope["error"]
    assert code == 1 and error["code"] == "UPSTREAM_INCOMPLETE"
    assert error["context"] == {"line": 2, "events": 2}


@pytest.mark.parametrize(
    ("line", "field", "fragment"),
    [
        ({**VWRL, "weight": "heavy"}, "weight", "expects a number"),
        ({"isin": "IE00B3RBWM25", "figi": "X"}, "ticker", "lacks ticker"),
        ({**VWRL, "price": 1}, "price", "has no field for"),
        ({**VWRL, "isin": "short"}, "isin", "12 characters"),
        ({**VWRL, "ticker": None}, "ticker", "is null"),
    ],
)
def test_a_record_that_fails_names_its_line_and_field(
    line: dict, field: str, fragment: str
) -> None:
    code, [envelope] = run(["summary"], bare(GOLD) + json.dumps(line) + "\n")
    error = envelope["error"]
    assert code == 1 and error["code"] == "RECORD_INVALID"
    assert error["context"] == {"line": 2, "field": field}
    assert fragment in error["message"]


def test_a_line_that_is_not_an_object_names_its_line() -> None:
    for text in ("{not json\n", "[1, 2]\n", '"text"\n'):
        code, [envelope] = run(["summary"], bare(VWRL) + text)
        assert code == 1 and envelope["error"]["context"] == {"line": 2}, text
    # An envelope's array item that is not an object, too
    item = json.dumps({"ok": True, "data": [3], "error": None, "meta": {}, "warnings": []})
    code, [envelope] = run(["summary"], item + "\n")
    assert code == 1 and envelope["error"]["code"] == "RECORD_INVALID"


def test_heartbeats_and_trust_tags_are_not_record_fields() -> None:
    beat = json.dumps({"status": "running", "heartbeat": True, "elapsed_ms": 10})
    tagged = {"_source": "external", "_trusted": False, **VWRL}
    code, [envelope] = run(["summary"], beat + "\n" + bare(tagged))
    assert code == 0 and envelope["data"]["tickers"] == ["VWRL"]


def test_a_heartbeat_naming_its_step_is_skipped() -> None:
    # A producer inside ctx.step adds the step to its heartbeat lines (REQ-C-008)
    beat = json.dumps({"status": "running", "heartbeat": True, "elapsed_ms": 10, "step": "1"})
    code, [envelope] = run(["summary"], beat + "\n" + bare(VWRL))
    assert code == 0 and envelope["data"]["tickers"] == ["VWRL"]


def test_call_and_exec_take_records_as_input_lines() -> None:
    lines = [json.dumps(VWRL), json.dumps(GOLD)]
    assert app.call("summary", {"input_lines": lines}).data == {
        "count": 2,
        "tickers": ["LYXGOLD", "VWRL"],
    }
    plan = json.dumps({"_cmd": "summary", "input_lines": lines[:1]}) + "\n"
    code, [envelope] = run(["exec"], plan)
    assert code == 0 and envelope["data"]["count"] == 1


def test_schema_records_the_input_type() -> None:
    code, [envelope] = run(["summary", "--schema"])
    data = envelope["data"]
    assert data["stdin_mode"] == "lines"
    records = data["stdin_records_schema"]
    assert records["required"] == ["isin", "figi", "ticker"]
    assert set(records["properties"]) == {"isin", "figi", "ticker", "weight"}
    # An agent matches it to the producer's output
    code, [producer] = run(["securities", "--schema"])
    assert set(producer["data"]["output_schema"]["properties"]) == set(records["properties"])


def test_registration_checks_the_record_type() -> None:
    target = App("reg", version="1.0.0")

    @dataclass(slots=True)
    class Loose:
        a: str

    @dataclass(frozen=True, slots=True)
    class Nested:
        sec: Sec

    @dataclass(frozen=True, slots=True)
    class Out:
        n: int

    for record, kwargs, match in (
        (dict, {}, "frozen dataclass"),
        (Loose, {}, "must be frozen"),
        (Nested, {}, "Nested.sec"),
        (Sec, {"stdin_input": True}, "line by line"),
    ):
        with pytest.raises(RegistrationError, match=match):

            @target.command("c", description="C", danger_level="safe", exit_codes=(),
                            stdin_records=record, **kwargs)  # fmt: skip
            def c(args: NoArgs, ctx: Ctx) -> Out:
                raise AssertionError


def test_a_real_pipeline_streams_records_between_processes() -> None:
    env = {"PATH": os.environ["PATH"]}
    producer = subprocess.Popen(
        [sys.executable, str(SECCTL), "securities", "--fail"], stdout=subprocess.PIPE, env=env
    )
    assert producer.stdout is not None
    consumer = subprocess.run(
        [sys.executable, str(SECCTL), "resolve"],
        stdin=producer.stdout,
        capture_output=True,
        env=env,
        timeout=20,
    )
    producer.stdout.close()
    assert producer.wait(timeout=10) == 4
    events = [json.loads(line) for line in consumer.stdout.splitlines()]
    assert events[0]["data"]["ticker"] == "VWRL"
    assert consumer.returncode == 1 and events[-1]["error"]["code"] == "UPSTREAM_FAILED"
    assert isinstance(Sec("IE00B3RBWM25", "f", "t"), Sec)
