import io
import json
import os
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import spec_validator

from treaty import App, Arg, Ctx, Exit, Flag, RegistrationError
from treaty._idempotency import TTL_SECONDS, IdempotencyKey, Record, claim


@dataclass(frozen=True, slots=True)
class CreateArgs:
    name: str = Arg(description="Item name")
    dry_run: bool = Flag(default=False, description="Preview only")


@dataclass(frozen=True, slots=True)
class Created:
    effect: str
    name: str
    serial: int
    key: str | None


def counting_app(state: Path) -> tuple[App, list[str]]:
    calls: list[str] = []
    app = App("itemctl", version="1", state_dir=state)
    app.exit_code("FLAKY", 79, description="Upstream hiccup", retryable=False, side_effects="none")

    @app.command(
        "create",
        description="Create an item",
        danger_level="mutating",
        exit_codes=["FLAKY"],
        supports_raw_payload=True,
    )
    def create(args: CreateArgs, ctx: Ctx) -> Created:
        calls.append(args.name)
        if args.name == "flaky" and len(calls) == 1:
            raise Exit.FLAKY("first attempt fails")
        effect = "would_create" if args.dry_run else "created"
        return Created(effect, args.name, len(calls), ctx.idempotency_key)

    @app.command("broken", description="Forgets its effect", danger_level="mutating")
    def broken(args: CreateArgs, ctx: Ctx) -> dict[str, object]:
        return {"effect": "created" if args.dry_run else "made"}

    @app.command("show", description="Read only")
    def show(args: CreateArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    return app, calls


def run(
    app: App, argv: list[str], *, stdin: str = "", env: dict[str, str] | None = None
) -> tuple[int, dict]:
    out = io.StringIO()
    code = app.run(argv, stdin=io.StringIO(stdin), stdout=out, stderr=io.StringIO(), env=env or {})
    envelope = json.loads(out.getvalue().splitlines()[0])
    spec_validator("response-envelope").validate(envelope)
    return code, envelope


def test_repeat_key_replays_the_original_result_as_noop(tmp_path: Path) -> None:
    app, calls = counting_app(tmp_path)
    code, first = run(app, ["create", "widget", "--idempotency-key", "k1"])
    assert code == 0 and first["data"]["effect"] == "created" and first["data"]["key"] == "k1"
    code, second = run(app, ["create", "widget", "--idempotency-key=k1"])
    assert code == 0 and calls == ["widget"]
    assert second["data"] == {**first["data"], "effect": "noop"}
    assert second["meta"]["idempotency_hit"] is True


def test_same_key_with_different_arguments_is_a_conflict(tmp_path: Path) -> None:
    app, calls = counting_app(tmp_path)
    run(app, ["create", "widget", "--idempotency-key", "k1"])
    code, env = run(app, ["create", "gadget", "--idempotency-key", "k1"])
    assert code == 6 and env["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert env["error"]["context"] == {"command": "create"} and calls == ["widget"]


def test_failures_are_not_stored_so_retries_run_again(tmp_path: Path) -> None:
    app, calls = counting_app(tmp_path)
    code, _ = run(app, ["create", "flaky", "--idempotency-key", "k1"])
    assert code == 79
    code, env = run(app, ["create", "flaky", "--idempotency-key", "k1"])
    assert code == 0 and env["data"]["effect"] == "created" and calls == ["flaky", "flaky"]


def test_dry_runs_neither_read_nor_write_the_store(tmp_path: Path) -> None:
    app, calls = counting_app(tmp_path)
    code, env = run(app, ["create", "widget", "--dry-run", "--idempotency-key", "k1"])
    assert code == 0 and env["data"]["effect"] == "would_create"
    code, env = run(app, ["create", "widget", "--idempotency-key", "k1"])
    assert env["data"]["effect"] == "created" and "idempotency_hit" not in env["meta"]
    code, env = run(app, ["create", "widget", "--dry-run", "--idempotency-key", "k1"])
    assert env["data"]["effect"] == "would_create" and len(calls) == 3


def test_expired_records_are_ignored(tmp_path: Path) -> None:
    app, calls = counting_app(tmp_path)
    run(app, ["create", "widget", "--idempotency-key", "k1"])
    (record,) = tmp_path.glob("*.json")
    stored = json.loads(record.read_text())
    stored["created_at"] -= TTL_SECONDS + 1
    record.write_text(json.dumps(stored))
    code, env = run(app, ["create", "widget", "--idempotency-key", "k1"])
    assert env["data"]["effect"] == "created" and len(calls) == 2


def test_exec_and_raw_payload_accept_the_key(tmp_path: Path) -> None:
    app, calls = counting_app(tmp_path)
    line = json.dumps({"_cmd": "create", "name": "widget", "idempotency_key": "k1"})
    run(app, ["exec"], stdin=line + "\n")
    code, env = run(
        app, ["create", "--raw-payload", '{"name": "widget"}', "--idempotency-key", "k1"]
    )
    assert code == 0 and env["data"]["effect"] == "noop" and calls == ["widget"]


def test_safe_commands_do_not_take_a_key(tmp_path: Path) -> None:
    app, _ = counting_app(tmp_path)
    code, env = run(app, ["show", "x", "--idempotency-key", "k1"])
    assert code == 2 and env["error"]["context"]["flag"] == "idempotency-key"


def test_state_dir_falls_back_to_env_and_fails_without_one(tmp_path: Path) -> None:
    app, _ = counting_app(tmp_path)
    app.state_dir = None
    code, _ = run(app, ["create", "a", "--idempotency-key", "k"], env={"HOME": str(tmp_path)})
    assert code == 0 and any((tmp_path / ".local/state/treaty/itemctl").glob("*.json"))
    code, env = run(app, ["create", "a", "--idempotency-key", "k"])
    assert code == 4 and env["error"]["code"] == "STATE_DIR_UNKNOWN"


def test_effect_contract_is_checked_at_run_time(tmp_path: Path) -> None:
    app, _ = counting_app(tmp_path)
    code, env = run(app, ["broken", "x"])
    assert code == 1 and env["error"]["code"] == "INVALID_EFFECT"
    code, env = run(app, ["broken", "x", "--dry-run"])
    assert code == 1 and "would_*" in env["error"]["message"]


def test_registration_requires_an_effect_field_and_reserves_the_key() -> None:
    app = App("x", version="1")
    with pytest.raises(RegistrationError, match="'effect' field"):

        @app.command("make", description="Make", danger_level="mutating")
        def make(args: CreateArgs, ctx: Ctx) -> list[str]:
            return []

    @dataclass(frozen=True, slots=True)
    class Keyed:
        idempotency_key: str = Flag(default="", description="Own key")

    with pytest.raises(RegistrationError, match="ctx.idempotency_key"):

        @app.command("keyed", description="Keyed", danger_level="mutating")
        def keyed(args: Keyed, ctx: Ctx) -> dict[str, object]:
            return {}


def test_concurrent_claims_on_one_key_are_serialized(tmp_path: Path) -> None:
    key = IdempotencyKey("k1")
    seen: list[Record | None] = []

    def first() -> None:
        with claim(tmp_path, key) as slot:
            time.sleep(0.2)
            slot.save(Record("fp", "create", {"effect": "created"}, time.time()))

    def second() -> None:
        with claim(tmp_path, key) as slot:
            seen.append(slot.record)

    a = threading.Thread(target=first)
    a.start()
    time.sleep(0.05)
    b = threading.Thread(target=second)
    b.start()
    a.join()
    b.join()
    assert seen[0] is not None and seen[0].data == {"effect": "created"}


def test_invalid_key_is_arg_error(tmp_path: Path) -> None:
    app, calls = counting_app(tmp_path)
    code, env = run(app, ["create", "widget", "--idempotency-key", "a\tb"])
    assert code == 2 and env["error"]["code"] == "ARG_ERROR" and calls == []


def test_timed_out_handler_keeps_the_key_until_it_finishes(tmp_path: Path) -> None:
    started: list[float] = []
    app = App("slowctl", version="1", state_dir=tmp_path, default_timeout=0.2)

    @app.command("create", description="Slow create", danger_level="mutating")
    def create(args: CreateArgs, ctx: Ctx) -> Created:
        started.append(time.monotonic())
        time.sleep(0.5)
        return Created("created", args.name, len(started), ctx.idempotency_key)

    first = app.call("create", {"name": "w", "idempotency_key": "k1"})
    assert first.error is not None and first.error.code == "TIMEOUT"
    # The retry waits no longer than its own timeout, and never runs beside the first
    busy = app.call("create", {"name": "w", "idempotency_key": "k1"})
    assert busy.error is not None and busy.error.code == "IDEMPOTENCY_KEY_BUSY"
    assert len(started) == 1, "the retry ran the mutation beside the abandoned handler"
    time.sleep(0.4)  # the abandoned handler finishes and its result is recorded
    replay = app.call("create", {"name": "w", "idempotency_key": "k1"})
    assert len(started) == 1
    assert replay.ok and isinstance(replay.data, dict) and replay.data["effect"] == "noop"


def test_prune_keeps_a_lock_that_is_held(tmp_path: Path) -> None:
    held, other = IdempotencyKey("held"), IdempotencyKey("other")
    acquired = threading.Event()
    with claim(tmp_path, held):
        lock = next(tmp_path.glob("*.lock"))
        stale = time.time() - TTL_SECONDS - 3600
        os.utime(lock, (stale, stale))
        with claim(tmp_path, other) as slot:
            slot.save(Record("fp", "create", {"effect": "created"}, time.time()))
        assert lock.exists()

        def contend() -> None:
            with claim(tmp_path, held):
                acquired.set()

        rival = threading.Thread(target=contend, daemon=True)
        rival.start()
        assert not acquired.wait(0.2), "a second claim got the key while it was held"
    assert acquired.wait(2)


def test_prune_removes_an_idle_expired_lock(tmp_path: Path) -> None:
    with claim(tmp_path, IdempotencyKey("idle")):
        pass
    lock = next(tmp_path.glob("*.lock"))
    stale = time.time() - TTL_SECONDS - 3600
    os.utime(lock, (stale, stale))
    with claim(tmp_path, IdempotencyKey("other")) as slot:
        slot.save(Record("fp", "create", {"effect": "created"}, time.time()))
    assert not lock.exists()


def test_signal_interrupts_a_retry_waiting_for_the_key(tmp_path: Path) -> None:
    app, calls = counting_app(tmp_path)
    released = threading.Event()

    def hold() -> None:
        with claim(tmp_path, IdempotencyKey("k1")):
            released.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    time.sleep(0.05)
    threading.Timer(0.2, signal.raise_signal, args=(signal.SIGTERM,)).start()
    try:
        code, env = run(app, ["create", "widget", "--idempotency-key", "k1"])
    finally:
        released.set()
        holder.join()
    assert code == 143 and env["error"]["code"] == "CANCELLED" and calls == []


def test_prune_keeps_a_record_whose_key_is_held(tmp_path: Path) -> None:
    held = IdempotencyKey("held")
    with claim(tmp_path, held) as slot:
        slot.save(Record("fp", "create", {"effect": "created"}, time.time()))
        record = slot.path
        stale = time.time() - TTL_SECONDS - 3600
        os.utime(record, (stale, stale))
        with claim(tmp_path, IdempotencyKey("other")) as other:
            other.save(Record("fp", "create", {"effect": "created"}, time.time()))
        assert record.exists(), "a record was pruned while its key was held"


def test_corrupt_record_is_a_precondition_error(tmp_path: Path) -> None:
    app, calls = counting_app(tmp_path)
    run(app, ["create", "widget", "--idempotency-key", "k1"])
    next(tmp_path.glob("*.json")).write_text("{not json")
    code, env = run(app, ["create", "widget", "--idempotency-key", "k1"])
    assert code == 4 and env["error"]["code"] == "IDEMPOTENCY_RECORD_CORRUPT"


def test_unwritable_state_dir_is_a_precondition_error(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    app, calls = counting_app(locked / "state")
    try:
        code, env = run(app, ["create", "widget", "--idempotency-key", "k1"])
    finally:
        locked.chmod(0o700)
    assert code == 4 and env["error"]["code"] == "STATE_DIR_UNWRITABLE" and calls == []
