"""A mutating or destructive command may return an output-adapted class whose schema
requires ``effect`` (#183): registration reads the adapter's schema, and each run checks
the dumped object's ``effect`` and ``would_affect`` as it checks a dataclass's."""

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from conftest import spec_validator

from treaty import App, Arg, Ctx, Flag, NoArgs, RegistrationError


class Document:
    """A dict-backed output: its keys are written as they are, camelCase ones included"""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body


AFFECTS: dict[str, Any] = {
    "type": ["object", "null"],
    "properties": {
        "summary": {"type": "string"},
        "resources": {"type": "array", "items": {"type": "string"}, "x-ordered": True},
        "count": {"type": "integer"},
    },
}


def adapted(properties: dict[str, Any], state: Path | None = None) -> App:
    app = App("docs", version="1.0.0", state_dir=state)
    app.output_adapter(
        Document,
        schema=lambda cls: {"type": "object", "properties": properties},
        dump=lambda o: o.body,
    )
    return app


def run(app: App, argv: list[str]) -> tuple[int, dict]:
    out = io.StringIO()
    code = app.run(argv, stdin=io.StringIO(), stdout=out, stderr=io.StringIO(), env={})
    envelope = json.loads(out.getvalue())
    spec_validator("response-envelope").validate(envelope)
    return code, envelope


@dataclass(frozen=True, slots=True)
class AddArgs:
    name: str = Arg(description="Server name")
    dry_run: bool = Flag(default=False, description="Preview only")


def add_app(body: dict[str, Any] | None = None, state: Path | None = None) -> App:
    app = adapted({"status": {"type": "string"}, "effect": {"type": "string"}}, state)

    @app.command("add", description="Add a server", danger_level="mutating", exit_codes=())
    def add(args: AddArgs, _c: Ctx) -> Document:
        effect = "would_create" if args.dry_run else "created"
        return Document(body if body is not None else {"status": args.name, "effect": effect})

    return app


def test_the_issue_example_registers_and_runs() -> None:
    code, env = run(add_app(), ["add", "web"])
    assert code == 0 and env["data"] == {"status": "web", "effect": "created"}
    assert "dry_run" not in env["meta"] or env["meta"]["dry_run"] is False


def test_a_dry_run_reports_a_would_effect_and_meta_dry_run() -> None:
    code, env = run(add_app(), ["add", "web", "--dry-run"])
    assert code == 0 and env["data"]["effect"] == "would_create"
    assert env["meta"]["dry_run"] is True


def test_a_dumped_effect_is_checked_as_a_dataclass_one() -> None:
    code, env = run(add_app({"status": "x", "effect": "made"}), ["add", "web"])
    assert code == 1 and env["error"]["code"] == "INVALID_EFFECT"
    code, env = run(add_app({"status": "x", "effect": "created"}), ["add", "web", "--dry-run"])
    assert code == 1 and env["error"]["code"] == "INVALID_EFFECT"
    assert "would_*" in env["error"]["message"]


def test_a_dump_that_leaves_out_effect_fails_the_run() -> None:
    code, env = run(add_app({"status": "x"}), ["add", "web"])
    assert code != 0 and env["error"]["code"] == "INVALID_OUTPUT"
    assert "'effect'" in env["error"]["message"]


def test_an_idempotent_replay_reports_noop(tmp_path: Path) -> None:
    app = add_app(state=tmp_path)
    run(app, ["add", "web", "--idempotency-key", "k1"])
    code, env = run(app, ["add", "web", "--idempotency-key", "k1"])
    assert code == 0 and env["data"] == {"status": "web", "effect": "noop"}
    assert env["meta"]["idempotency_hit"] is True


def test_a_closed_effect_enum_admits_the_replays_noop() -> None:
    app = adapted({"effect": {"type": "string", "enum": ["created"]}})

    @app.command("add", description="Add", danger_level="mutating", exit_codes=())
    def add(_a: NoArgs, _c: Ctx) -> Document:
        return Document({"effect": "created"})

    _, env = run(app, ["add", "--output-schema"])
    assert env["data"]["properties"]["effect"]["enum"] == ["created", "noop"]


@dataclass(frozen=True, slots=True)
class DropArgs:
    name: str = Arg(description="Server name")
    dry_run: bool = Flag(default=False, description="Preview only")


DROPPED: list[str] = []


def drop_app() -> App:
    app = adapted({"effect": {"type": "string"}, "would_affect": AFFECTS})

    @app.command("drop", description="Drop a server", danger_level="destructive", exit_codes=())
    def drop(args: DropArgs, _c: Ctx) -> Document:
        if args.dry_run:
            affects = {"summary": f"Drops {args.name}", "resources": [args.name], "count": 1}
            return Document({"effect": "would_delete", "would_affect": affects})
        DROPPED.append(args.name)
        return Document({"effect": "deleted", "would_affect": None})

    return app


def test_a_destructive_adapted_command_previews_then_applies() -> None:
    DROPPED.clear()
    code, env = run(drop_app(), ["drop", "web"])
    assert code == 2 and env["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert env["data"]["would_affect"]["summary"] == "Drops web" and DROPPED == []
    code, env = run(drop_app(), ["drop", "web", "--dry-run"])
    assert code == 0 and env["meta"]["dry_run"] is True and DROPPED == []
    code, env = run(drop_app(), ["drop", "web", "--confirm-destructive"])
    assert code == 0 and env["data"]["effect"] == "deleted" and DROPPED == ["web"]


def test_a_destructive_preview_without_would_affect_fails_the_run() -> None:
    app = adapted({"effect": {"type": "string"}, "would_affect": AFFECTS})

    @app.command("drop", description="Drop", danger_level="destructive", exit_codes=())
    def drop(args: DropArgs, _c: Ctx) -> Document:
        return Document({"effect": "would_delete", "would_affect": None})

    code, env = run(app, ["drop", "web", "--dry-run"])
    assert code == 1 and env["error"]["code"] == "INVALID_EFFECT"
    assert "would_affect" in env["error"]["message"]


def test_an_adapter_without_would_affect_cannot_be_destructive() -> None:
    app = adapted({"effect": {"type": "string"}})
    with pytest.raises(RegistrationError, match="'would_affect' field"):

        @app.command("drop", description="Drop", danger_level="destructive", exit_codes=())
        def drop(args: DropArgs, _c: Ctx) -> Document:
            return Document({"effect": "deleted"})


def test_an_adapter_without_effect_cannot_be_mutating() -> None:
    app = adapted({"status": {"type": "string"}})
    with pytest.raises(RegistrationError, match=r"'effect' field \(REQ-C-003\)"):

        @app.command("add", description="Add", danger_level="mutating", exit_codes=())
        def add(_a: NoArgs, _c: Ctx) -> Document:
            return Document({"status": "ok"})


def test_an_adapter_that_lists_effect_without_requiring_it_names_why() -> None:
    app = adapted({"effect": {"type": "string", "x-volatile": True}})
    with pytest.raises(RegistrationError, match="lists 'effect' but does not require it"):

        @app.command("add", description="Add", danger_level="mutating", exit_codes=())
        def add(_a: NoArgs, _c: Ctx) -> Document:
            return Document({"effect": "created"})


def test_a_list_of_adapted_objects_carries_no_effect() -> None:
    app = adapted({"effect": {"type": "string"}})
    with pytest.raises(RegistrationError, match="'effect' field"):

        @app.command("add", description="Add", danger_level="mutating", exit_codes=())
        def add(_a: NoArgs, _c: Ctx) -> list[Document]:
            return [Document({"effect": "created"})]


def test_a_command_registered_before_its_adapter_fails_registration() -> None:
    app = App("docs", version="1.0.0")
    with pytest.raises(RegistrationError, match=r"app\.output_adapter"):

        @app.command("add", description="Add", danger_level="mutating", exit_codes=())
        def add(_a: NoArgs, _c: Ctx) -> Document:
            return Document({"effect": "created"})
