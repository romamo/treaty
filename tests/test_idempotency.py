import io
import json
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
