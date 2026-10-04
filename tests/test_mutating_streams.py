"""Mutating streams: an effect per event, counted on the summary line (REQ-O-004, #175)."""

import dataclasses
import io
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import spec_validator

from treaty import App, Arg, Ctx, Exit, Flag, RegistrationError
from treaty._envelope import with_meta
from treaty._mcp import call_tool, tool_entries
from treaty._skills import render

RAN: list[str] = []


@dataclass(frozen=True, slots=True)
class SyncArgs:
    users: str = Arg(description="Comma-separated user ids; a trailing * marks an unchanged one")
    dry_run: bool = Flag(default=False, description="Report what would change, change nothing")
    fail_after: int | None = Flag(default=None, description="Raise UPSTREAM after this many")
    crash_after: int | None = Flag(default=None, description="Raise RuntimeError after this many")
    effect: str | None = Flag(default=None, description="Report this effect on every event")


@dataclass(frozen=True, slots=True)
class Synced:
    id: str
    effect: str


@dataclass(frozen=True, slots=True)
class Missing:
    id: str
    effect: str | None = None


def sync_app() -> App:
    app = App("syncctl", version="1.0.0", default_timeout=5)
    app.exit_code("UPSTREAM", 80, description="Directory down", retryable=True, side_effects="none")

    @app.command(
        "sync",
        description="Create the users that are missing",
        streaming=True,
        danger_level="mutating",
        exit_codes=["UPSTREAM"],
    )
    def sync(args: SyncArgs, ctx: Ctx) -> Iterator[Synced]:
        for n, user in enumerate(args.users.split(","), start=1):
            if args.fail_after is not None and n > args.fail_after:
                raise Exit.UPSTREAM("directory down", context={"after": n - 1})
            if args.crash_after is not None and n > args.crash_after:
                raise RuntimeError("boom")
            unchanged = user.endswith("*")
            if args.effect is not None:
                effect = args.effect
            elif args.dry_run:
                effect = "would_noop" if unchanged else "would_create"
            else:
                effect = "noop" if unchanged else "created"
            if not args.dry_run and not unchanged:
                RAN.append(user)
            yield Synced(user.rstrip("*"), effect)

    @app.command(
        "tail", description="Emit lines", streaming=True, danger_level="safe", exit_codes=()
    )
    def tail(args: SyncArgs, ctx: Ctx) -> Iterator[Synced]:
        for user in args.users.split(","):
            yield Synced(user, "seen")

    return app


def run(
    argv: list[str], *, env: dict[str, str] | None = None, stdin: str = ""
) -> tuple[int, list[dict]]:
    RAN.clear()
    out, err = io.StringIO(), io.StringIO()
    code = sync_app().run(
        argv, stdin=io.StringIO(stdin), stdout=out, stderr=err, env=env or {}, isatty=False
    )
    return code, [json.loads(line) for line in out.getvalue().splitlines()]


def valid(lines: list[dict]) -> None:
    validator = spec_validator("response-envelope")
    for line in lines:
        validator.validate(line)


# Registration


def register(match: str, **kwargs: object) -> None:
    app = App("x", version="1.0.0")
    with pytest.raises(RegistrationError, match=match):

        @app.command("x", description="x", streaming=True, exit_codes=(), **kwargs)  # type: ignore[arg-type]
        def handler(args: SyncArgs, ctx: Ctx) -> Iterator[Synced]:
            yield Synced("u", "created")


def test_a_destructive_stream_is_refused() -> None:
    register(r"cannot ask confirmation for each action.*REQ-O-021", danger_level="destructive")


def test_a_mutating_stream_taking_an_idempotency_key_is_refused() -> None:
    @dataclass(frozen=True, slots=True)
    class Keyed:
        idempotency_key: str = Flag(default="", description="Key")

    app = App("x", version="1.0.0")
    with pytest.raises(RegistrationError, match="drop the idempotency_key field"):

        @app.command("x", description="x", streaming=True, danger_level="mutating", exit_codes=())
        def handler(args: Keyed, ctx: Ctx) -> Iterator[Synced]:
            yield Synced("u", "created")


def test_a_mutating_stream_must_yield_events_with_an_effect_field() -> None:
    app = App("x", version="1.0.0")

    @dataclass(frozen=True, slots=True)
    class Bare:
        id: str

    with pytest.raises(RegistrationError, match=r"'effect' field \(REQ-C-003\)"):

        @app.command("x", description="x", streaming=True, danger_level="mutating", exit_codes=())
        def handler(args: SyncArgs, ctx: Ctx) -> Iterator[Bare]:
            yield Bare("u")


def test_a_mutating_stream_cannot_write_config() -> None:
    register(
        "drop config_write_scope= or streaming=True",
        danger_level="mutating",
        config_write_scope="local",
    )


def test_the_manifest_offers_no_idempotency_key_or_conflict_on_a_mutating_stream() -> None:
    code, (manifest,) = run(["manifest"])
    assert code == 0
    spec_validator("manifest-response").validate(manifest["data"])
    entry = manifest["data"]["commands"]["sync"]
    assert "idempotency-key" not in entry["flags"]
    assert "dry-run" in entry["flags"]
    assert "5" not in entry["exit_codes"]  # CONFLICT answers a reused key, which a stream lacks
    assert entry["danger_level"] == "mutating" and entry["streaming_default"] is True


def test_idempotency_key_is_an_unknown_flag_on_a_mutating_stream() -> None:
    code, lines = run(["sync", "a", "--idempotency-key", "k1"])
    assert code == 2
    assert RAN == []
    assert lines[-1]["error"]["code"] == "ARG_ERROR"


# The stream


def test_each_event_carries_its_effect_and_the_summary_counts_them() -> None:
    code, lines = run(["sync", "u1,u2*,u3"])
    assert code == 0
    valid(lines)
    assert [line["data"] for line in lines[:3]] == [
        {"id": "u1", "effect": "created"},
        {"id": "u2", "effect": "noop"},
        {"id": "u3", "effect": "created"},
    ]
    summary = lines[3]["meta"]
    assert summary["end"] is True and summary["total"] == 3
    assert summary["effects"] == {"created": 2, "noop": 1}
    assert "dry_run" not in summary
    assert RAN == ["u1", "u3"]


def test_an_empty_mutating_stream_counts_no_effects() -> None:
    @dataclass(frozen=True, slots=True)
    class NoArgs:
        pass

    app = App("x", version="1.0.0")

    @app.command("x", description="x", streaming=True, danger_level="mutating", exit_codes=())
    def handler(args: NoArgs, ctx: Ctx) -> Iterator[Synced]:
        yield from ()

    out = io.StringIO()
    assert app.run(["x"], stdout=out, stderr=io.StringIO(), env={}, isatty=False) == 0
    (summary,) = [json.loads(line) for line in out.getvalue().splitlines()]
    assert summary["meta"]["effects"] == {}


def test_dry_run_covers_the_whole_stream() -> None:
    code, lines = run(["sync", "u1,u2*,u3", "--dry-run"])
    assert code == 0
    valid(lines)
    assert [line["data"]["effect"] for line in lines[:3]] == [
        "would_create",
        "would_noop",
        "would_create",
    ]
    summary = lines[3]["meta"]
    assert summary["dry_run"] is True
    assert summary["effects"] == {"would_create": 2, "would_noop": 1}
    assert RAN == []


def test_a_failed_dry_run_stream_says_dry_run_on_its_error() -> None:
    code, lines = run(["sync", "u1,u2", "--dry-run", "--fail-after", "1"])
    assert code == 80
    valid(lines)
    error = lines[-1]
    assert error["ok"] is False and error["meta"]["dry_run"] is True
    # Nothing was applied, so the exit code's own retryable stands
    assert error["error"]["retryable"] is True


def test_a_stream_failing_after_a_live_effect_is_not_retryable() -> None:
    code, lines = run(["sync", "u1,u2", "--fail-after", "1"])
    assert code == 80
    valid(lines)
    assert lines[0]["data"]["effect"] == "created"
    error = lines[-1]["error"]
    assert error["code"] == "UPSTREAM" and error["retryable"] is False
    assert "retry_strategy" not in error and "no side effects" not in error["suggestion"]
    assert lines[-1]["meta"]["partial"] is True and lines[-1]["meta"]["seq"] == 1
    assert "effects" not in lines[-1]["meta"]


def test_a_stream_failing_after_only_noop_effects_stays_retryable() -> None:
    code, lines = run(["sync", "u1*,u2", "--fail-after", "1"])
    assert code == 80
    assert lines[-1]["error"]["retryable"] is True


def test_a_handler_crash_after_a_live_effect_is_not_retryable() -> None:
    code, lines = run(["sync", "u1,u2", "--crash-after", "1"])
    assert code == 1
    valid(lines)
    assert lines[-1]["error"]["code"] == "HANDLER_CRASHED"
    assert lines[-1]["error"]["retryable"] is False


@pytest.mark.parametrize(
    ("argv", "problem"),
    [
        (["--effect", "made"], "'made' is not one of created, deleted, noop, updated"),
        (["--effect", "would_create"], "'would_create' is not one of"),
        (["--dry-run", "--effect", "created"], "previews use a would_* value"),
    ],
)
def test_an_event_with_a_wrong_effect_ends_the_stream(argv: list[str], problem: str) -> None:
    code, lines = run(["sync", "u1,u2", *argv])
    assert code == 1
    valid(lines)
    assert len(lines) == 1  # the bad event is never written
    error = lines[0]["error"]
    assert error["code"] == "INVALID_EFFECT" and error["retryable"] is False
    assert "event 1" in error["message"] and problem in error["message"]
    assert lines[0]["meta"]["seq"] == 0


def test_an_event_without_an_effect_ends_the_stream() -> None:
    app = App("x", version="1.0.0")

    @app.command("x", description="x", streaming=True, danger_level="mutating", exit_codes=())
    def handler(args: SyncArgs, ctx: Ctx) -> Iterator[Missing]:
        yield Missing("u1", "created")
        yield Missing("u2")

    out = io.StringIO()
    code = app.run(["x", "a"], stdout=out, stderr=io.StringIO(), env={}, isatty=False)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert code == 1
    assert lines[0]["data"] == {"id": "u1", "effect": "created"}
    assert lines[1]["error"]["code"] == "INVALID_EFFECT"
    assert "event 2" in lines[1]["error"]["message"]
    assert "no string 'effect' field" in lines[1]["error"]["message"]


def test_a_safe_stream_gains_no_effect_keys() -> None:
    code, lines = run(["tail", "a,b"])
    assert code == 0
    assert lines[0]["data"] == {"id": "a", "effect": "seen"}
    assert "effects" not in lines[-1]["meta"] and "dry_run" not in lines[-1]["meta"]


# Buffered answers


def test_no_stream_puts_the_counts_in_meta_effects() -> None:
    code, (envelope,) = run(["sync", "u1,u2*", "--no-stream"])
    assert code == 0
    valid([envelope])
    assert envelope["data"] == [
        {"id": "u1", "effect": "created"},
        {"id": "u2", "effect": "noop"},
    ]
    assert envelope["meta"]["effects"] == {"created": 1, "noop": 1}


def test_no_stream_dry_run_says_dry_run() -> None:
    code, (envelope,) = run(["sync", "u1", "--no-stream", "--dry-run"])
    assert code == 0
    assert envelope["meta"]["effects"] == {"would_create": 1}
    assert envelope["meta"]["dry_run"] is True


def test_no_stream_failure_keeps_the_events_and_their_counts() -> None:
    code, (envelope,) = run(["sync", "u1,u2", "--no-stream", "--fail-after", "1"])
    assert code == 80
    valid([envelope])
    assert envelope["data"] == [{"id": "u1", "effect": "created"}]
    assert envelope["meta"]["effects"] == {"created": 1}
    assert envelope["error"]["retryable"] is False


def test_app_call_buffers_a_mutating_stream_with_its_counts() -> None:
    RAN.clear()
    envelope = sync_app().call("sync", {"users": "u1,u2*"}).to_json()
    assert envelope["ok"] is True
    assert envelope["meta"]["effects"] == {"created": 1, "noop": 1}
    preview = sync_app().call("sync", {"users": "u1", "dry_run": True}).to_json()
    assert preview["meta"]["effects"] == {"would_create": 1}
    assert preview["meta"]["dry_run"] is True
    keyed = sync_app().call("sync", {"users": "u1", "idempotency_key": "k"}).to_json()
    assert keyed["meta"]["exit_code"] == 2


STREAM_KEYS = {"seq", "end", "total", "effects", "dry_run", "partial"}


def test_app_call_puts_the_stream_keys_on_meta_not_extra_meta() -> None:
    # #348: typed fields of Meta, so a test reads env.meta.effects
    envelope = sync_app().call("sync", {"users": "u1,u2*"}, env={})
    assert envelope.meta.effects == {"created": 1, "noop": 1}
    assert envelope.meta.total == 2
    assert envelope.meta.dry_run is None and envelope.meta.partial is None
    assert envelope.meta.seq is None and envelope.meta.end is None
    preview = sync_app().call("sync", {"users": "u1", "dry_run": True}, env={})
    assert preview.meta.effects == {"would_create": 1} and preview.meta.dry_run is True
    failed = sync_app().call("sync", {"users": "u1,u2", "fail_after": 1}, env={})
    assert failed.meta.partial is True and failed.meta.effects == {"created": 1}
    for env in (envelope, preview, failed):
        assert not STREAM_KEYS & set(env.extra_meta)


def test_the_stream_keys_serialize_as_before() -> None:
    # #348: moving the keys onto Meta leaves every line's meta as it was
    code, lines = run(["sync", "u1,u2*", "--stable-output"])
    assert code == 0
    valid(lines)
    assert [STREAM_KEYS & set(line["meta"]) for line in lines] == [
        {"seq"},
        {"seq"},
        {"seq", "end", "total", "effects"},
    ]
    assert lines[2]["meta"]["seq"] == 2 and lines[2]["meta"]["pagination"]["total"] == 2
    code, lines = run(["sync", "u1,u2", "--no-stream", "--fail-after", "1", "--dry-run"])
    assert code == 80
    valid(lines)
    (failed,) = lines
    assert {k: failed["meta"][k] for k in STREAM_KEYS & set(failed["meta"])} == {
        "total": 1,
        "effects": {"would_create": 1},
        "dry_run": True,
        "partial": True,
    }
    code, lines = run(["tail", "a", "--no-stream"])
    assert code == 0 and STREAM_KEYS & set(lines[0]["meta"]) == {"total"}


def test_a_non_stream_envelope_has_none_of_the_stream_keys() -> None:
    app = App("plain", version="1.0.0")

    @app.command("show", description="Show", danger_level="safe", exit_codes=())
    def show(args: SyncArgs, ctx: Ctx) -> Synced:
        return Synced(args.users, "seen")

    envelope = app.call("show", {"users": "u1"}, env={})
    assert envelope.ok
    meta = envelope.to_json()["meta"]
    assert isinstance(meta, dict) and not STREAM_KEYS & set(meta)
    assert all(getattr(envelope.meta, key) is None for key in STREAM_KEYS)


def test_extra_meta_refuses_a_key_meta_declares() -> None:
    envelope = sync_app().call("sync", {"users": "u1"}, env={})
    with pytest.raises(RegistrationError, match="extra_meta holds effects, total"):
        dataclasses.replace(envelope, extra_meta={"total": 1, "effects": {}})
    with pytest.raises(RegistrationError, match=r"meta\.total must be a whole number"):
        with_meta(envelope, {"total": True})
    with pytest.raises(RegistrationError, match=r"meta\.effects must count effects"):
        with_meta(envelope, {"effects": {"created": -1}})
    added = with_meta(envelope, {"dry_run": False, "confirmed": True})
    assert added.meta.dry_run is False and added.extra_meta["confirmed"] is True


def test_meta_stays_hashable_with_effects() -> None:
    meta = sync_app().call("sync", {"users": "u1,u2*"}, env={}).meta
    assert hash(meta) == hash(dataclasses.replace(meta, effects={"noop": 1, "created": 1}))
    assert meta.effects == {"created": 1, "noop": 1}
    assert meta.to_json()["effects"] == {"created": 1, "noop": 1}


def test_mcp_serves_a_mutating_stream_buffered() -> None:
    app = sync_app()
    entries = {e.name: e for e in tool_entries(app)}
    entry = entries["sync"]
    assert "idempotency_key" not in entry.input_schema["properties"]
    assert "a repeat runs again" in entry.description
    envelope = call_tool(app, entries, "sync", {"users": "u1,u2"}).to_json()
    assert envelope["meta"]["effects"] == {"created": 2}


def test_exec_streams_each_event_with_its_effect() -> None:
    plan = "\n".join(
        json.dumps(line)
        for line in (
            {"_cmd": "sync", "users": "u1,u2*"},
            {"_cmd": "sync", "users": "u3", "_opts": {"no-stream": True}},
        )
    )
    code, lines = run(["exec"], stdin=plan)
    assert code == 0
    valid(lines)
    assert [line["data"]["effect"] for line in lines[:2]] == ["created", "noop"]
    assert lines[2]["meta"]["effects"] == {"created": 1, "noop": 1}
    assert lines[3]["meta"]["effects"] == {"created": 1}
    assert lines[3]["meta"]["_line"] == 2


def test_exec_dry_run_covers_a_mutating_stream() -> None:
    plan = json.dumps({"_cmd": "sync", "users": "u1,u2*"})
    code, lines = run(["exec", "--dry-run"], stdin=plan)
    assert code == 0
    assert [line["data"]["effect"] for line in lines[:2]] == ["would_create", "would_noop"]
    assert lines[2]["meta"]["dry_run"] is True
    assert RAN == []


# One audit entry per run


def audit(log: Path) -> list[dict]:
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    for entry in entries:
        spec_validator("audit-log-entry").validate(entry)
    return entries


def test_the_audit_log_records_a_stream_once_with_its_counts(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    env = {"SYNCCTL_AUDIT_LOG": str(log)}
    assert run(["sync", "u1,u2*,u3"], env=env)[0] == 0
    assert run(["sync", "u1,u2", "--fail-after", "1"], env=env)[0] == 80
    assert run(["sync", "u1", "--dry-run", "--no-stream"], env=env)[0] == 0
    assert run(["tail", "a"], env=env)[0] == 0
    entries = audit(log)
    assert len(entries) == 4
    assert entries[0]["effects"] == {"created": 2, "noop": 1}
    assert entries[1]["effects"] == {"created": 1} and entries[1]["exit_code"] == 80
    assert entries[2]["effects"] == {"would_create": 1}
    assert entries[2]["args"]["dry_run"] is True
    assert "effects" not in entries[3]


def test_exec_writes_one_audit_entry_per_stream_line(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    plan = "\n".join(json.dumps({"_cmd": "sync", "users": users}) for users in ("u1", "u2*,u3*"))
    assert run(["exec"], env={"SYNCCTL_AUDIT_LOG": str(log)}, stdin=plan)[0] == 0
    effects = [e.get("effects") for e in audit(log) if e["command"] == "sync"]
    assert effects == [{"created": 1}, {"noop": 2}]


# No replay


def test_a_session_never_replays_a_stream() -> None:
    env = {"SYNCCTL_SESSION": "s-1"}
    first = run(["sync", "u1"], env=env)
    assert RAN == ["u1"]
    second = run(["sync", "u1"], env=env)
    assert RAN == ["u1"]  # the handler ran again: a repeated loop is a new loop
    for _, lines in (first, second):
        assert lines[0]["data"]["effect"] == "created"
        assert "idempotency_key" not in lines[-1]["meta"]
        assert "idempotency_hit" not in lines[-1]["meta"]


# Docs


def test_help_and_skills_describe_the_per_event_effect() -> None:
    out = io.StringIO()
    sync_app().run(["sync", "--help"], stdout=out, stderr=io.StringIO(), env={}, isatty=True)
    assert "Each event reports its own effect" in out.getvalue()
    assert "--idempotency-key" not in out.getvalue()
    skill = render(sync_app())["SKILL-sync.md"]
    assert "Mutating stream" in skill and "pass --idempotency-key" not in skill
