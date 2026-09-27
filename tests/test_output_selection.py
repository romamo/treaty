"""Output selection and streaming flags: --fields, --stream, --format id,
--heartbeat-interval, the token budget flags, and exec (REQ-O-002, REQ-O-004, REQ-O-005,
REQ-O-012, REQ-O-049, REQ-O-050)"""

import importlib.util
import io
import json
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from conftest import spec_validator

from treaty import App, Arg, Ctx, Exit, Flag, NoArgs, RegistrationError
from treaty._audit import audit


@dataclass(frozen=True, slots=True)
class User:
    id: str
    name: str
    email: str


@dataclass(frozen=True, slots=True)
class Get:
    id: str = Arg(description="User id")


@dataclass(frozen=True, slots=True)
class Mail:
    user_id: str = Flag(description="Who gets it", from_stdin=True)
    dry_run: bool = Flag(default=False, description="Preview only")


@dataclass(frozen=True, slots=True)
class Mailed:
    user_id: str
    effect: str


@dataclass(frozen=True, slots=True)
class Count:
    count: int = Arg(description="Events to emit")
    sleep: float = Flag(default=0.0, description="Seconds before each event")


@dataclass(frozen=True, slots=True)
class Wait:
    seconds: float = Flag(default=0.0, description="How long to work")


@dataclass(frozen=True, slots=True)
class Order:
    order_id: int
    total: int


@dataclass(frozen=True, slots=True)
class Ticket:
    ticket_id: str
    title: str


@dataclass(frozen=True, slots=True)
class Counted:
    id: int


@dataclass(frozen=True, slots=True)
class Finished:
    done: bool


@dataclass(frozen=True, slots=True)
class Named:
    name: str = Flag(default="x", description="Name")


FINISHED: list[str] = []
PIDS: list[int] = []
USERS = [User(f"u{i}", f"user {i}", f"u{i}@example.com") for i in range(1, 4)]


def make_app() -> App:
    app = App("selctl", version="1.0.0", default_timeout=5)
    app.exit_code("NO_USER", 79, description="No such user", retryable=False, side_effects="none")

    @app.command("user", description="Show a user", danger_level="safe", exit_codes=["NO_USER"])
    def user(args: Get, ctx: Ctx) -> User:
        PIDS.append(os.getpid())
        match = [u for u in USERS if u.id == args.id]
        if not match:
            raise Exit.NO_USER(f"no user {args.id}")
        return match[0]

    @app.command("users", description="List users", danger_level="safe", exit_codes=())
    def users(args: NoArgs, ctx: Ctx) -> list[User]:
        return USERS

    @app.command("counter", description="Answer id 42", danger_level="safe", exit_codes=())
    def counter(args: NoArgs, ctx: Ctx) -> Counted:
        return Counted(42)

    @app.command(
        "counter-typed", description="Id 42", danger_level="safe", exit_codes=(), id_field="id"
    )
    def counter_typed(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"id": 42}

    @app.command(
        "mail",
        description="Mail a user",
        danger_level="mutating",
        exit_codes=(),
        id_field="user_id",
    )
    def mail(args: Mail, ctx: Ctx) -> Mailed:
        return Mailed(args.user_id, "would_create" if args.dry_run else "created")

    @app.command(
        "tail",
        description="Emit events",
        streaming=True,
        danger_level="safe",
        exit_codes=(),
    )
    def tail(args: Count, ctx: Ctx) -> Iterator[Counted]:
        try:
            for n in range(1, args.count + 1):
                time.sleep(args.sleep)
                yield Counted(n)
        finally:
            FINISHED.append("tail")

    @app.command(
        "migrate", description="Work a while", danger_level="safe", exit_codes=(), heartbeat=True
    )
    def migrate(args: Wait, ctx: Ctx) -> dict[str, bool]:
        ctx.progress("Connecting to database...")
        time.sleep(args.seconds / 2)
        ctx.progress("Running migration batch 1/2...")
        time.sleep(args.seconds / 2)
        return {"done": True}

    return app


def run(
    argv: list[str],
    *,
    app: App | None = None,
    stdin: str = "",
    isatty: bool = False,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = (app or make_app()).run(
        argv, stdin=io.StringIO(stdin), stdout=out, stderr=err, env=env or {}, isatty=isatty
    )
    return code, out.getvalue(), err.getvalue()


def envelope(argv: list[str], **kwargs: Any) -> tuple[int, dict[str, Any]]:
    code, out, _ = run([*argv, "--format", "json"], **kwargs)
    return code, json.loads(out.splitlines()[-1])


def lines_of(out: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.splitlines()]


# REQ-O-002: --fields


def test_fields_id_name_returns_a_data_object_with_only_id_and_name_keys() -> None:
    code, env = envelope(["user", "u1", "--fields", "id,name"])
    assert code == 0 and env["data"] == {"id": "u1", "name": "user 1"}
    code, env = envelope(["users", "--fields", "id,name"])
    assert env["data"] == [{"id": f"u{i}", "name": f"user {i}"} for i in range(1, 4)]


def test_ok_error_meta_are_always_present_even_with_fields() -> None:
    for argv in (["user", "u1"], ["user", "nobody"]):
        _, env = envelope([*argv, "--fields", "id"])
        assert {"ok", "error", "meta", "warnings", "data"} <= set(env)
        spec_validator("response-envelope").validate(env)
    _, env = envelope(["users", "--fields", "id"])
    assert env["meta"]["pagination"]["returned"] == 3


def test_an_unknown_field_name_in_fields_is_silently_ignored() -> None:
    code, env = envelope(["user", "u1", "--fields", "id,nickname"])
    assert code == 0 and env["data"] == {"id": "u1"} and env["warnings"] == []


def test_fields_is_available_on_every_command() -> None:
    app = make_app()
    manifest = json.loads(run(["manifest"], app=app)[1])["data"]
    assert "fields" in manifest["flags"]
    for argv in (["user", "u1"], ["users"], ["counter"], ["tail", "1"], ["manifest"]):
        code, out, _ = run(["--fields", "id", *argv], app=app)
        assert code == 0, out
    _, env = envelope(["--fields", "version", "version"], app=app)
    assert set(env["data"]) == {"version"}


def test_fields_applies_to_each_stream_event_and_each_exec_line() -> None:
    _, out, _ = run(["tail", "2", "--fields", "nothing"])
    assert [line["data"] for line in lines_of(out)[:2]] == [{}, {}]
    plan = (
        '{"_cmd": "user", "id": "u2", "_opts": {"fields": "email"}}\n{"_cmd": "user", "id": "u3"}\n'
    )
    code, out, _ = run(["exec"], stdin=plan)
    first, second = lines_of(out)
    assert code == 0 and first["data"] == {"email": "u2@example.com"}
    assert set(second["data"]) == {"id", "name", "email"}


def test_an_empty_name_in_fields_is_an_argument_error() -> None:
    code, env = envelope(["user", "u1", "--fields", "id,,name"])
    assert code == 2 and env["error"]["context"]["flag"] == "fields"


def test_fields_keeps_the_trust_tags_of_external_content() -> None:
    app = App("t", version="1.0.0")

    @app.command("read", description="Read", danger_level="safe", exit_codes=(), external=True)
    def read(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"body": "text", "path": "/x"}

    code, env = envelope(["read", "--fields", "body"], app=app)
    assert env["data"] == {"_source": "external", "_trusted": False, "body": "text"}


# REQ-O-004: --stream


class _Recorder(io.StringIO):
    """stdout that notes whether the handler had finished when the first line came"""

    def __init__(self) -> None:
        super().__init__()
        self.first_line_after_finish: bool | None = None

    def write(self, text: str, /) -> int:
        if self.first_line_after_finish is None:
            self.first_line_after_finish = bool(FINISHED)
        return super().write(text)


def test_stream_causes_output_to_begin_appearing_before_the_command_completes() -> None:
    FINISHED.clear()
    out = _Recorder()
    code = make_app().run(
        ["tail", "3", "--sleep", "0.05", "--stream"],
        stdout=out,
        stderr=io.StringIO(),
        env={},
        isatty=False,
    )
    assert code == 0 and out.first_line_after_finish is False and FINISHED == ["tail"]


def test_each_line_of_streaming_output_is_a_valid_self_contained_json_object() -> None:
    _, out, _ = run(["tail", "3", "--stream"])
    lines = out.splitlines()
    assert len(lines) == 4
    for line in lines:
        spec_validator("response-envelope").validate(json.loads(line))


def test_the_final_line_of_streaming_output_is_a_summary_object_containing_pagination() -> None:
    _, out, _ = run(["tail", "3", "--stream"])
    last = lines_of(out)[-1]
    assert last["meta"]["end"] is True
    assert last["meta"]["pagination"] == {
        "total": 3,
        "returned": 3,
        "truncated": False,
        "has_more": False,
        "next_cursor": None,
    }
    spec_validator("response-envelope").validate(last)


def test_a_command_that_does_not_declare_supports_streaming_warns_when_stream_is_passed() -> None:
    code, env = envelope(["user", "u1", "--stream"])
    assert code == 0 and env["data"]["id"] == "u1"
    assert [w["code"] for w in env["warnings"]] == ["STREAMING_NOT_SUPPORTED"]
    assert env["warnings"][0]["context"] == {"command": "user"}


def test_a_command_that_declares_streaming_default_emits_jsonl_without_any_flags() -> None:
    code, out, _ = run(["tail", "2"])
    assert code == 0 and [line["data"] for line in lines_of(out)] == [{"id": 1}, {"id": 2}, None]


def test_passing_no_stream_to_a_streaming_default_command_returns_a_valid_response_envelope() -> (
    None
):
    code, out, _ = run(["tail", "2", "--no-stream"])
    (env,) = lines_of(out)
    spec_validator("response-envelope").validate(env)
    assert code == 0 and env["data"] == [{"id": 1}, {"id": 2}] and env["meta"]["total"] == 2


def test_the_manifest_exposes_streaming_default_for_commands_that_declare_it() -> None:
    commands = json.loads(run(["manifest"])[1])["data"]["commands"]
    assert commands["tail"]["streaming_default"] is True
    assert "streaming_default" not in commands["user"]


def test_stream_and_no_stream_together_are_an_argument_error() -> None:
    code, env = envelope(["tail", "2", "--stream", "--no-stream"])
    assert code == 2 and "contradict" in env["error"]["message"]


# REQ-O-005: --format id


def test_format_id_on_a_command_that_returns_data_id_42_produces_42_newline() -> None:
    for command in ("counter", "counter-typed"):
        code, out, err = run([command, "--format", "id"])
        assert (code, out, err) == (0, "42\n", "")


def test_the_output_is_directly_pipeable_to_another_commands_stdin() -> None:
    _, ids, _ = run(["user", "u2", "--format", "id"])
    code, env = envelope(["mail", "--user-id", "-"], stdin=ids)
    assert code == 0 and env["data"]["user_id"] == "u2"


def test_no_json_structure_no_whitespace_beyond_the_terminal_newline() -> None:
    _, out, _ = run(["user", "u1", "--format", "id"])
    assert out == "u1\n" and not re.search(r"[{}\[\]\"\s]", out[:-1])
    _, out, _ = run(["users", "--format", "id"])
    assert out == "u1\nu2\nu3\n"
    _, out, _ = run(["tail", "2", "--format", "id"])
    assert out == "1\n2\n"


def test_format_id_on_a_command_without_an_id_is_an_argument_error() -> None:
    code, out, err = run(["migrate", "--format", "id"])
    env = json.loads(out)
    assert code == 2 and env["error"]["context"]["value"] == "id"
    assert "id_field=" in env["error"]["suggestion"]


def test_an_id_that_cannot_be_piped_is_invalid_output() -> None:
    app = make_app()

    @app.command(
        "ticket", description="A ticket", danger_level="safe", exit_codes=(), id_field="ticket_id"
    )
    def ticket(args: NoArgs, ctx: Ctx) -> Ticket:
        return Ticket("t 1", "has a space")

    code, out, err = run(["ticket", "--format", "id"], app=app)
    assert code == 1 and out == "" and "INVALID_OUTPUT" in err


def test_a_page_with_more_writes_the_next_cursor_on_stderr() -> None:
    app = App("t", version="1.0.0")

    @app.command("list", description="List", danger_level="safe", exit_codes=(), default_limit=2)
    def listing(args: NoArgs, ctx: Ctx) -> list[User]:
        return USERS

    code, out, err = run(["list", "--format", "id"], app=app)
    assert out == "u1\nu2\n" and err.startswith("next: --cursor ")


def test_id_field_is_checked_at_registration() -> None:
    app = App("t", version="1.0.0")
    with pytest.raises(RegistrationError, match="not a field of the output"):

        @app.command("a", description="A", danger_level="safe", exit_codes=(), id_field="uid")
        def a(args: NoArgs, ctx: Ctx) -> User:
            return USERS[0]

    with pytest.raises(RegistrationError, match="str, int, UUID"):

        @app.command("b", description="B", danger_level="safe", exit_codes=(), id_field="done")
        def b(args: NoArgs, ctx: Ctx) -> Finished:
            return Finished(True)


def test_the_manifest_offers_id_where_a_command_has_an_id() -> None:
    data = json.loads(run(["manifest"])[1])["data"]
    assert "id" in data["flags"]["format"]["enum_values"]
    assert data["commands"]["user"]["output_formats"] == ["id"]
    assert data["commands"]["mail"]["output_formats"] == ["id"]
    assert "output_formats" not in data["commands"]["migrate"]


def test_audit_suggests_id_field_for_an_output_with_one_id_like_field() -> None:
    app = App("t", version="1.0.0")

    @app.command("orders", description="Orders", danger_level="safe", exit_codes=())
    def orders(args: NoArgs, ctx: Ctx) -> list[Order]:
        return [Order(1, 2)]

    rule = next(r for r in audit(app, "t", limit=3).rules if r.id == "id-field")
    assert [f.fix for f in rule.findings] == ['id_field="order_id"']
    clean = next(r for r in audit(make_app(), "selctl", limit=3).rules if r.id == "id-field")
    assert clean.passed


# REQ-O-012: --heartbeat-interval

HEARTBEAT = re.compile(r"\[(\d+)s\] (.+)")


def test_heartbeat_interval_causes_a_progress_message_to_stderr_every_interval() -> None:
    code, _, err = run(["migrate", "--seconds", "0.6", "--heartbeat-interval", "0.1"])
    beats = [line for line in err.splitlines() if HEARTBEAT.fullmatch(line)]
    assert code == 0 and 3 <= len(beats) <= 7


def test_the_heartbeat_message_includes_elapsed_time_and_the_most_recent_progress_status() -> None:
    _, _, err = run(["migrate", "--seconds", "0.6", "--heartbeat-interval", "0.1"])
    statuses = [HEARTBEAT.fullmatch(line) for line in err.splitlines()]
    assert all(m is not None and m.group(1) == "0" for m in statuses)
    said = [m.group(2) for m in statuses if m is not None]
    assert said[0] == "Connecting to database..."
    assert said[-1] == "Running migration batch 1/2..."


def test_heartbeat_messages_are_plain_text_never_json() -> None:
    code, out, err = run(["migrate", "--seconds", "0.3", "--heartbeat-interval", "0.1"])
    assert err and all(not line.startswith("{") for line in err.splitlines())
    assert [line for line in out.splitlines() if "heartbeat" in line] == []


def test_with_quiet_heartbeat_messages_are_suppressed() -> None:
    argv = ["migrate", "--seconds", "0.3", "--heartbeat-interval", "0.1", "--quiet"]
    code, out, err = run(argv)
    assert code == 0 and err == "" and json.loads(out)["data"] == {"done": True}


def test_heartbeat_interval_takes_positive_seconds_and_only_heartbeat_commands() -> None:
    code, env = envelope(["migrate", "--heartbeat-interval", "0"])
    assert code == 2
    code, env = envelope(["user", "u1", "--heartbeat-interval", "1"])
    assert code == 2
    flags = json.loads(run(["manifest"])[1])["data"]["commands"]["migrate"]["flags"]
    assert flags["heartbeat-interval"]["type"] == "number"


# REQ-O-049: token budget flags

PAD = "x" * 59  # each item is 80 bytes of JSON: 20 tokens of the approx tokenizer


def window_app() -> App:
    app = App("t", version="1.0.0")

    @app.command("events", description="Events", danger_level="safe", exit_codes=())
    def events(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"items": [{"id": f"i{n:02}", "pad": PAD} for n in range(30)], "kind": "event"}

    @app.command("huge", description="One big value", danger_level="safe", exit_codes=())
    def huge(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"text": "y" * 4000}

    return app


def tokens(data: object) -> int:
    return -(-len(json.dumps(data, separators=(",", ":"), sort_keys=True).encode()) // 4)


def test_token_limit_500_produces_output_within_the_limit_with_truncated_and_token_limit() -> None:
    code, env = envelope(["events", "--token-limit", "500"], app=window_app())
    assert code == 0 and tokens(env["data"]) <= 500
    assert env["meta"]["truncated"] is True and env["meta"]["token_limit"] == 500
    assert env["data"]["kind"] == "event" and 0 < len(env["data"]["items"]) < 30
    code, env = envelope(["huge", "--token-limit", "500"], app=window_app())
    assert tokens(env["data"]) <= 500 and env["data"]["text"].endswith("[truncated]")
    assert env["warnings"][0]["code"] == "FIELD_TRUNCATED"


def test_token_count_returns_data_null_and_meta_token_count_without_writing_the_payload() -> None:
    app = window_app()
    _, full = envelope(["events"], app=app)
    code, out, _ = run(["events", "--token-count", "--format", "json"], app=app)
    env = json.loads(out)
    assert code == 0 and env["data"] is None and env["meta"]["token_count"] == tokens(full["data"])
    assert PAD not in out


def test_token_offset_200_token_limit_200_returns_the_second_window() -> None:
    app = window_app()
    _, second = envelope(["events", "--token-offset", "200", "--token-limit", "200"], app=app)
    items = second["data"]["items"]
    # Each item spans 20 tokens: the window starts with the item from token 200 on
    assert items[0]["id"] == "i10" and second["meta"]["token_offset"] == 200
    assert tokens(second["data"]) <= 200
    # Following next_token_offset visits every item once
    seen: list[str] = []
    offset = 0
    while True:
        argv = ["events", "--token-offset", str(offset), "--token-limit", "200"]
        _, env = envelope(argv, app=app)
        seen += [i["id"] for i in env["data"]["items"]]
        if "next_token_offset" not in env["meta"]:
            break
        offset = env["meta"]["next_token_offset"]
    assert seen == [f"i{n:02}" for n in range(30)]


def test_all_three_flags_are_available_on_every_command_without_per_command_implementation() -> (
    None
):
    app = make_app()
    global_flags = json.loads(run(["manifest"], app=app)[1])["data"]["flags"]
    assert {"token-limit", "token-offset", "token-count", "tokenizer"} <= set(global_flags)
    for argv in (["user", "u1"], ["users"], ["tail", "2"], ["migrate"], ["version"]):
        for flags in (["--token-limit", "50"], ["--token-offset", "0"], ["--token-count"]):
            code, out, _ = run([*argv, *flags, "--format", "json"], app=app)
            assert code == 0, out
            assert json.loads(out.splitlines()[-1])["meta"]["tokenizer"] == "approx"


def test_meta_token_count_is_present_in_every_response_regardless_of_format() -> None:
    for mode in ("plain", "json", "tsv", "id"):
        code, out, _ = run(["user", "u1", "--token-count", "--format", mode], isatty=True)
        env = json.loads(out)
        assert env["data"] is None and env["meta"]["token_count"] > 0
    code, out, _ = run(["user", "nobody", "--token-count", "--format", "plain"], isatty=True)
    assert code == 79 and "token_count" in json.loads(out)["meta"]


def test_an_app_tokenizer_is_selected_with_tokenizer() -> None:
    app = window_app()
    app.tokenizer("chars", count=len)
    code, env = envelope(["huge", "--token-count", "--tokenizer", "chars"], app=app)
    assert env["meta"]["token_count"] == len('{"text":"' + "y" * 4000 + '"}')
    assert env["meta"]["tokenizer"] == "chars"
    app.tokenizer("words", count=lambda text: len(text.split()), default=True)
    _, env = envelope(["huge", "--token-count"], app=app)
    assert env["meta"]["tokenizer"] == "words" and env["meta"]["token_count"] == 1
    code, env = envelope(["huge", "--token-count", "--tokenizer", "nope"], app=app)
    assert code == 2 and "approx" in env["error"]["context"]["available"]


def test_cl100k_base_comes_from_the_tiktoken_extra() -> None:
    code, env = envelope(["huge", "--token-count", "--tokenizer", "cl100k_base"], app=window_app())
    if importlib.util.find_spec("tiktoken") is None:
        assert code == 2 and env["error"]["code"] == "TOKENIZER_UNAVAILABLE"
        assert "treaty[tiktoken]" in env["error"]["suggestion"]
    else:
        assert code == 0 and env["meta"]["token_count"] > 0


def test_token_count_takes_no_window() -> None:
    code, env = envelope(["huge", "--token-count", "--token-limit", "5"], app=window_app())
    assert code == 2


# REQ-O-050: exec


def exec_run(plan: list[str], *flags: str) -> tuple[int, list[dict[str, Any]]]:
    code, out, _ = run(["exec", *flags, "--format", "jsonl"], stdin="\n".join(plan) + "\n")
    return code, lines_of(out)


def test_cat_ops_jsonl_exec_format_jsonl_dispatches_all_lines_in_process() -> None:
    PIDS.clear()
    plan = ['{"_cmd": "user", "id": "u1"}', '{"_cmd": "user", "id": "u2"}']
    code, out = exec_run(plan)
    assert code == 0 and len(out) == 2 and PIDS == [os.getpid(), os.getpid()]


def test_each_response_line_is_a_valid_response_envelope_with_cmd_and_line_in_meta() -> None:
    _, out = exec_run(['{"_cmd": "user", "id": "u1"}', '{"_cmd": "counter"}'])
    for n, line in enumerate(out, 1):
        spec_validator("response-envelope").validate(line)
        assert line["meta"]["_line"] == n
    assert [line["meta"]["_cmd"] for line in out] == ["user", "counter"]


def test_without_ignore_errors_dispatch_stops_at_the_first_failure_and_exits_1() -> None:
    code, out = exec_run(['{"_cmd": "user", "id": "nobody"}', '{"_cmd": "counter"}'])
    assert code == 1 and len(out) == 1 and out[0]["error"]["code"] == "NO_USER"


def test_with_ignore_errors_dispatch_continues_past_line_failures_exit_still_1() -> None:
    plan = ['{"_cmd": "user", "id": "nobody"}', '{"_cmd": "counter"}']
    code, out = exec_run(plan, "--ignore-errors")
    assert code == 1 and len(out) == 2 and out[1]["ok"] is True


def test_dry_run_is_forwarded_to_every_dispatched_command_that_declares_danger_level_not_safe() -> (
    None
):
    plan = ['{"_cmd": "mail", "user_id": "u1"}', '{"_cmd": "user", "id": "u1"}']
    code, out = exec_run(plan, "--dry-run")
    assert code == 0 and out[0]["data"]["effect"] == "would_create" and out[1]["ok"] is True


def test_a_jsonl_parse_error_emits_dispatch_parse_error_with_phase_validation() -> None:
    PIDS.clear()
    code, out = exec_run(['{"id": "u1"}', '{"_cmd": "user", "id": "u1"}'], "--ignore-errors")
    assert out[0]["error"]["code"] == "DISPATCH_PARSE_ERROR"
    assert out[0]["error"]["phase"] == "validation" and out[0]["meta"]["exit_code"] == 2
    assert PIDS == [os.getpid()]  # only the second line ran a handler
    assert code == 1


def test_a_fully_malformed_stream_exits_2() -> None:
    code, out = exec_run(["{", "not json"], "--ignore-errors")
    assert code == 2 and len(out) == 2


def test_exec_is_available_with_zero_per_command_implementation_work() -> None:
    app = App("t", version="1.0.0")

    @app.command("show", description="Show", danger_level="safe", exit_codes=())
    def show(args: Named, ctx: Ctx) -> dict[str, str]:
        return {"name": args.name}

    code, out, _ = run(["exec"], app=app, stdin='{"_cmd": "show", "name": "a"}\n')
    assert code == 0 and lines_of(out)[0]["data"] == {"name": "a"}
