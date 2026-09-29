"""Exit 2 means nothing ran (REQ-F-002, REQ-F-015) and text arguments are single-line
(REQ-F-044, phase-1 half)."""

import io
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from treaty import App, Arg, Ctx, Exit, Flag, ParseError, RegistrationError
from treaty._audit import audit

SIDE_EFFECTS: list[str] = []


@dataclass(frozen=True, slots=True)
class Window:
    start: int = Flag(description="First hour")
    end: int = Flag(description="Last hour")
    note: str = Flag(default="", description="Free text")
    message: str = Flag(default="", description="Body text", multiline=True)
    out: Path | None = Flag(default=None, description="File to write")

    def __post_init__(self) -> None:
        errors = []
        if self.end < self.start:
            errors.append(ParseError("end is before start", context={"field": "end"}))
        if self.end - self.start > 12:
            errors.append(ParseError("window is longer than 12 hours", context={"field": "end"}))
        if errors:
            raise ParseError.combine(errors)


def make_app() -> App:
    app = App("sched", version="1.0.0")

    @app.command(
        "book",
        description="Book a window",
        supports_raw_payload=True,
        exit_codes=(),
        danger_level="safe",
    )
    def book(args: Window, ctx: Ctx) -> dict[str, object]:
        SIDE_EFFECTS.append("book")
        if args.out is not None:
            args.out.write_text("booked")
        if args.note == "late":
            raise ParseError("note 'late' is refused", context={"flag": "note"})
        if args.note == "exit":
            raise Exit.ARG_ERROR("note 'exit' is refused", context={"flag": "note"})
        return {"start": args.start, "end": args.end, "message": args.message}

    return app


def run(argv: list[str], *, stdin: str = "") -> tuple[int, dict[str, object]]:
    SIDE_EFFECTS.clear()
    out = io.StringIO()
    code = make_app().run(
        argv, stdin=io.StringIO(stdin), stdout=out, stderr=io.StringIO(), env={}, isatty=False
    )
    return code, json.loads(out.getvalue().splitlines()[-1])


def error_of(envelope: dict[str, object]) -> dict[str, object]:
    error = envelope["error"]
    assert isinstance(error, dict)
    return error


# REQ-F-002: a handler's ParseError is not exit 2 (decision D2)


def test_handler_parse_error_after_a_write_is_validation_after_start(tmp_path: Path) -> None:
    target = tmp_path / "booking"
    code, env = run(["book", "--start", "1", "--end", "2", "--note", "late", "--out", str(target)])
    error = error_of(env)
    assert code == 1 and target.read_text() == "booked"
    assert error["code"] == "VALIDATION_AFTER_START" and error["phase"] == "execution"
    assert error["retryable"] is False and error["context"] == {"flag": "note"}
    assert "__post_init__" in str(error["fix_required"])


def test_handler_exit_arg_error_is_validation_after_start() -> None:
    code, env = run(["book", "--start", "1", "--end", "2", "--note", "exit"])
    assert code == 1 and error_of(env)["code"] == "VALIDATION_AFTER_START"
    assert SIDE_EFFECTS == ["book"]


# REQ-F-015: __post_init__ is the phase-1 cross-field check


def test_post_init_parse_error_exits_2_before_the_handler() -> None:
    code, env = run(["book", "--start", "5", "--end", "3"])
    error = error_of(env)
    assert code == 2 and error["phase"] == "validation" and error["code"] == "ARG_ERROR"
    assert error["message"] == "End is before start."
    assert SIDE_EFFECTS == []


def test_two_post_init_errors_in_one_run() -> None:
    @dataclass(frozen=True, slots=True)
    class Pair:
        a: int = Flag(description="A")
        b: int = Flag(description="B")

        def __post_init__(self) -> None:
            raise ParseError.combine(
                [
                    ParseError("a is odd", context={"field": "a"}),
                    ParseError("b is odd", context={"field": "b"}),
                ]
            )

    app = App("x", version="1.0.0")

    @app.command("pair", description="Pair", exit_codes=(), danger_level="safe")
    def pair(args: Pair, ctx: Ctx) -> dict[str, int]:
        return {"a": args.a}

    out = io.StringIO()
    code = app.run(
        ["pair", "--a", "1", "--b", "1"], stdout=out, stderr=io.StringIO(), env={}, isatty=False
    )
    errors = json.loads(out.getvalue())["error"]["errors"]
    assert code == 2 and [e["field"] for e in errors] == ["a", "b"]


def test_a_bad_flag_and_a_failing_post_init_are_reported_together() -> None:
    code, env = run(["book", "--start", "5", "--end", "3", "--bogus"])
    error = error_of(env)
    assert code == 2 and error["context"]["error_count"] == 2  # type: ignore[index]
    messages = [e["message"] for e in error["errors"]]  # type: ignore[union-attr]
    assert messages == ["Unknown flag '--bogus'.", "End is before start."]


def test_post_init_is_skipped_while_a_field_is_invalid() -> None:
    code, env = run(["book", "--start", "x", "--end", "3"])
    errors = error_of(env)["errors"]
    assert code == 2 and [e["field"] for e in errors] == ["start"]  # type: ignore[union-attr]


# REQ-F-044: newlines refused in phase 1 on every route


@pytest.mark.parametrize(
    ("value", "pattern"),
    [("a\nb", "newline"), ("a\rb", "carriage_return"), ("a\x00b", "null_byte")],
)
def test_control_characters_are_refused_on_argv(value: str, pattern: str) -> None:
    code, env = run(["book", "--start", "1", "--end", "2", "--note", value])
    error = error_of(env)
    assert code == 2 and error["phase"] == "validation"
    assert error["context"]["rejected_pattern"] == pattern  # type: ignore[index]
    assert SIDE_EFFECTS == []


def test_newline_is_refused_in_raw_payload_and_exec() -> None:
    payload = json.dumps({"start": 1, "end": 2, "note": "a\nb"})
    code, env = run(["book", "--raw-payload", payload])
    assert code == 2 and error_of(env)["context"]["rejected_pattern"] == "newline"  # type: ignore[index]
    line = json.dumps({"_cmd": "book", "start": 1, "end": 2, "note": "a\nb"})
    code, env = run(["exec"], stdin=line + "\n")
    assert env["meta"]["exit_code"] == 2  # type: ignore[index]
    assert SIDE_EFFECTS == []


def test_newline_is_refused_through_app_call() -> None:
    envelope = make_app().call("book", {"start": 1, "end": 2, "note": "a\nb"}, env={})
    assert envelope.exit_code == 2 and envelope.error is not None
    assert envelope.error.context["rejected_pattern"] == "newline"


def test_multiline_field_accepts_newlines() -> None:
    code, env = run(["book", "--start", "1", "--end", "2", "--message", "line 1\nline 2"])
    assert code == 0 and env["data"]["message"] == "line 1\nline 2"  # type: ignore[index]
    code, env = run(["book", "--start", "1", "--end", "2", "--message", "a\x00b"])
    assert code == 2


def test_manifest_states_that_multiline_accepts_newlines() -> None:
    flags = make_app().manifest()["commands"]["book"]["flags"]  # type: ignore[index]
    assert flags["message"]["description"] == "Body text (may contain newlines)"
    assert flags["note"]["description"] == "Free text"


def test_multiline_on_a_non_text_field_fails_registration() -> None:
    @dataclass(frozen=True, slots=True)
    class Bad:
        count: int = Flag(description="Count", multiline=True)

    app = App("x", version="1.0.0")
    with pytest.raises(RegistrationError, match="multiline"):

        @app.command("bad", description="Bad", exit_codes=(), danger_level="safe")
        def bad(args: Bad, ctx: Ctx) -> None:
            return None


def test_audit_suggests_multiline_for_free_text_fields() -> None:
    @dataclass(frozen=True, slots=True)
    class Post:
        body: str = Flag(description="Post body")
        title: str = Flag(description="Title")

    app = App("x", version="1.0.0")

    @app.command("post", description="Post", exit_codes=(), danger_level="safe")
    def post(args: Post, ctx: Ctx) -> None:
        return None

    report = audit(app, "x:app", limit=3)
    rule = next(r for r in report.rules if r.id == "multiline-flag")
    assert [f.message.split()[0] for f in rule.findings] == ["body"]
    assert "multiline=True" in rule.findings[0].fix


# REQ-F-002: every framework exit 2 leaves the handler untouched


@pytest.mark.parametrize(
    "argv",
    [
        ["book", "--start", "1"],
        ["book", "--start", "one", "--end", "2"],
        ["book", "--start", "1", "--end", "2", "--bogus", "x"],
        ["book", "--start", "5", "--end", "1"],
        ["book", "--start", "1", "--end", "2", "--note", "a\nb"],
        ["book", "--start", "1", "--end", "2", "--out", "../x"],
        ["book", "--raw-payload", "{"],
        ["book", "--raw-payload", '{"start": 1}', "--end", "2"],
        ["book", "--start", "1", "--end", "2", "extra"],
        ["--format", "xml", "book"],
        ["nope"],
    ],
)
def test_every_exit_2_path_leaves_the_handler_untouched(argv: list[str]) -> None:
    code, env = run(argv)
    assert code == 2 and error_of(env)["phase"] == "validation"
    assert SIDE_EFFECTS == []


def test_a_multiline_positional_is_suggested_as_an_arg_and_accepts_newlines() -> None:
    @dataclass(frozen=True, slots=True)
    class Post:
        body: str = Arg(description="Post body")

    app = App("x", version="1.0.0")

    @app.command("post", description="Post", exit_codes=(), danger_level="safe")
    def post(args: Post, ctx: Ctx) -> None:
        return None

    report = audit(app, "x:app", limit=3)
    rule = next(r for r in report.rules if r.id == "multiline-flag")
    assert rule.findings[0].fix.startswith("body: str = Arg(..., multiline=True)")

    @dataclass(frozen=True, slots=True)
    class Fixed:
        body: str = Arg(description="Post body", multiline=True)

    fixed = App("y", version="1.0.0")

    @fixed.command("post", description="Post", exit_codes=(), danger_level="safe")
    def post_fixed(args: Fixed, ctx: Ctx) -> dict[str, str]:
        return {"body": args.body}

    env = fixed.call("post", {"body": "one\ntwo"})
    assert env.ok and env.data == {"body": "one\ntwo"}
    assert not any(
        r.findings for r in audit(fixed, "y:app", limit=3).rules if r.id == "multiline-flag"
    )
