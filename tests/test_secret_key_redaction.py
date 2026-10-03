"""A secret used as a mapping key is redacted as it is as a value (#262): in ``Exit``
context, a warning, a log field, the audit log, an exec line, and ``App.call``. Two keys
that redact to the same text both stay, the later one suffixed, never one dropped."""

import io
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from treaty import App, Ctx, Envelope, Exit, Flag, ParseError
from treaty._redact import redacted, scrub, scrub_fields

PIN = 987654
WORD = "sk_live_abcdef"
ENV = {"PROBE_PIN": str(PIN), "PROBE_WORD": WORD}
ARGV = ["--pin-from-env", "PROBE_PIN", "--word-from-env", "PROBE_WORD"]


@dataclass(frozen=True, slots=True)
class KeyArgs:
    pin: int = Flag(default=0, secret=True, description="Account PIN")
    word: str = Flag(default="", secret=True, description="A string secret")


def probe_app() -> App:
    app = App("probe", version="1.0.0")

    @app.command("fail", description="Fail", danger_level="safe", exit_codes=("NOT_FOUND",))
    def fail(args: KeyArgs, ctx: Ctx) -> dict[str, bool]:
        raise Exit.NOT_FOUND(
            "no such account",
            context={
                # Exit context keys are strings: a number's spellings stand in for it
                "by_pin": {str(args.pin): "a", str(float(args.pin)): "b"},
                "by_word": {args.word: 1, f"x-{args.word}": 2, "[REDACTED]": 3},
                args.word: "top",
            },
        )

    @app.command("show", description="Show", danger_level="safe", exit_codes=())
    def show(args: KeyArgs, ctx: Ctx) -> dict[str, object]:
        ctx.warn("SEEN", "keys were seen", by_pin={args.pin: 1}, by_word={args.word: 2})
        ctx.log("checked", **{args.word: 1}, seen={str(args.pin): 2})
        # Command data is no log layer: it is not redacted (REQ-F-034)
        return {"ok": True}

    return app


def run(argv: list[str], *, stdin: str = "", env: Mapping[str, str] = ENV) -> tuple[str, str]:
    out, err = io.StringIO(), io.StringIO()
    probe_app().run(
        argv, stdin=io.StringIO(stdin), stdout=out, stderr=err, env=dict(env), isatty=False
    )
    return out.getvalue(), err.getvalue()


def assert_context_redacted(context: object) -> None:
    assert context == {
        "by_pin": {"[REDACTED]": "a", "[REDACTED].0": "b"},
        "by_word": {"[REDACTED]": 1, "x-[REDACTED]": 2, "[REDACTED]#2": 3},
        "[REDACTED]": "top",
    }


def test_exit_context_keys_under_main() -> None:
    out, err = run(["fail", *ARGV])
    assert str(PIN) not in out + err and WORD not in out + err
    assert_context_redacted(json.loads(out)["error"]["context"])


def test_exit_context_keys_in_exec_lines() -> None:
    plan = json.dumps({"_cmd": "fail", "pin_from_env": "PROBE_PIN", "word_from_env": "PROBE_WORD"})
    out, _ = run(["exec", "--ignore-errors"], stdin=plan + "\n")
    assert str(PIN) not in out and WORD not in out
    assert_context_redacted(json.loads(out)["error"]["context"])


def test_exit_context_keys_under_app_call() -> None:
    called = probe_app().call(
        "fail", {"pin_from_env": "PROBE_PIN", "word_from_env": "PROBE_WORD"}, env=ENV
    )
    assert isinstance(called, Envelope) and called.error is not None
    assert str(PIN) not in json.dumps(called.to_json()) and WORD not in json.dumps(called.to_json())
    assert_context_redacted(called.error.context)


def test_warning_and_log_field_keys() -> None:
    out, err = run(["show", *ARGV, "-vv"])
    assert str(PIN) not in out + err and WORD not in out + err
    warning = json.loads(out)["warnings"][0]
    assert warning["context"] == {"by_pin": {"[REDACTED]": 1}, "by_word": {"[REDACTED]": 2}}
    lines = [json.loads(line) for line in err.splitlines()]
    [logged] = [line for line in lines if line["message"] == "checked"]
    assert logged["fields"] == {"[REDACTED]": 1, "seen": {"[REDACTED]": 2}}


class Legacy:
    """An exec_fallback that answers with nothing"""

    def __call__(self, cmd: str, payload: Mapping[str, object]) -> object:
        return None


def test_exec_fallback_payload_keys_in_the_audit_log(tmp_path: Path) -> None:
    app = App("bean", version="1.0.0", exec_fallback=Legacy())
    log = tmp_path / "audit.jsonl"
    line = {"_cmd": "old", "password": WORD, "map": {WORD: 1, str(PIN): 2}, WORD: 3}
    app.run(
        ["exec"],
        stdin=io.StringIO(json.dumps(line) + "\n"),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        env={"BEAN_AUDIT_LOG": str(log)},
        isatty=False,
    )
    assert WORD not in log.read_text()
    entry = json.loads(log.read_text())
    assert entry["args"] == {
        "password": "[REDACTED]",
        "map": {"[REDACTED]": 1, "987654": 2},
        "[REDACTED]": 3,
    }


def redact(text: str) -> str:
    return text.replace(WORD, "[REDACTED]").replace(str(PIN), "[REDACTED]")


def test_colliding_keys_all_stay() -> None:
    """A key that redacts to a key already there, or is one, is suffixed, skipping a
    suffix already taken; the numeric key's ``_number`` rule redacts it too"""
    value = {WORD: 1, "[REDACTED]": 2, "[REDACTED]#2": 3, PIN: 4, "plain": 5}
    expected = {
        "[REDACTED]": 1,
        "[REDACTED]#2": 2,
        "[REDACTED]#2#2": 3,
        "[REDACTED]#3": 4,
        "plain": 5,
    }
    assert redacted(value, redact) == expected
    assert scrub("", value, redact) == expected
    assert scrub_fields(value, redact) == expected


def test_a_key_named_like_a_secret_still_hides_its_value() -> None:
    """scrub masks by the original key's name before the key itself is redacted"""
    assert scrub_fields({f"{WORD}_token": "x", "n": 1}, redact) == {
        "[REDACTED]_token": "[REDACTED]",
        "n": 1,
    }


class TupleKeyed:
    """An exec_fallback refusing with a context keyed by a tuple that holds the secret"""

    def __call__(self, cmd: str, payload: Mapping[str, object]) -> object:
        context: dict[object, object] = {(WORD, 1): "t", "nested": {(WORD,): 1}, ("ok",): 2}
        raise ParseError("bad", context=cast(dict[str, object], context))


def test_a_tuple_key_holding_a_secret_is_redacted() -> None:
    """A key that is neither text nor a number is read as the text JSON prints it as"""
    out = io.StringIO()
    line = {"_cmd": "old", "password": WORD}
    App("bean", version="1.0.0", exec_fallback=TupleKeyed()).run(
        ["exec"],
        stdin=io.StringIO(json.dumps(line) + "\n"),
        stdout=out,
        stderr=io.StringIO(),
        env={},
        isatty=False,
    )
    assert WORD not in out.getvalue()
    assert json.loads(out.getvalue())["error"]["context"] == {
        "([REDACTED], 1)": "t",  # the quoted spelling is replaced whole
        "nested": {"([REDACTED],)": 1},
        "('ok',)": 2,
    }
    assert redacted({(WORD,): 1, ("ok",): 2}, redact) == {"('[REDACTED]',)": 1, ("ok",): 2}
