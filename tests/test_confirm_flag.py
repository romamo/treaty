"""#197: ``Flag(confirm=True)``, a mutating command's own confirmation flag: without it
the run is a preview under the ``--dry-run`` contract, with it the command runs"""

import io
import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import spec_validator

from treaty import App, Ctx, Flag, RegistrationError
from treaty._skills import render

ENV = {"PATH": os.environ["PATH"], "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}


@dataclass(frozen=True, slots=True)
class ApproveArgs:
    proposal: str = Flag(default="p1", description="Proposal to approve")
    yes: bool = Flag(confirm=True, description="Approve and apply it")


def approve_app(applied: list[str], *, honest: bool = True) -> App:
    app = App("cf", version="1.0.0")

    @app.command(
        "approve",
        description="Approve a recorded proposal",
        danger_level="mutating",
        exit_codes=(),
        supports_raw_payload=True,
    )
    def approve(args: ApproveArgs, ctx: Ctx) -> dict[str, str]:
        if not args.yes:
            return {"effect": "would_update" if honest else "noop", "proposal": args.proposal}
        applied.append(args.proposal)
        return {"effect": "updated" if honest else "would_update", "proposal": args.proposal}

    return app


def run(
    app: App, argv: list[str], *, stdin: str = "", env: dict[str, str] | None = None
) -> tuple[int, list[dict[str, object]]]:
    out = io.StringIO()
    code = app.run(
        [*argv, "--format", "json"],
        stdin=io.StringIO(stdin),
        stdout=out,
        stderr=io.StringIO(),
        env={**ENV, **(env or {})},
    )
    return code, [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def meta(envelope: dict[str, object]) -> dict[str, object]:
    found = envelope["meta"]
    assert isinstance(found, dict)
    return found


def test_without_the_flag_the_run_is_a_dry_run() -> None:
    applied: list[str] = []
    code, [envelope] = run(approve_app(applied), ["approve"])
    assert code == 0, envelope
    assert envelope["data"] == {"effect": "would_update", "proposal": "p1"}
    assert meta(envelope)["dry_run"] is True
    assert applied == []


def test_with_the_flag_the_command_runs() -> None:
    applied: list[str] = []
    code, [envelope] = run(approve_app(applied), ["approve", "--yes"])
    assert code == 0, envelope
    assert envelope["data"] == {"effect": "updated", "proposal": "p1"}
    assert "dry_run" not in meta(envelope)
    assert applied == ["p1"]


def test_the_effect_contract_follows_the_flag() -> None:
    code, [envelope] = run(approve_app([], honest=False), ["approve"])
    assert code == 1 and envelope["error"]["code"] == "INVALID_EFFECT"  # type: ignore[index]
    assert "would_*" in envelope["error"]["message"]  # type: ignore[index]
    code, [envelope] = run(approve_app([], honest=False), ["approve", "--yes"])
    assert code == 1 and envelope["error"]["code"] == "INVALID_EFFECT"  # type: ignore[index]


def test_every_input_path_treats_a_missing_flag_as_a_preview() -> None:
    applied: list[str] = []
    app = approve_app(applied)
    code, [envelope] = run(app, ["approve", "--raw-payload", "{}"])
    assert code == 0 and meta(envelope)["dry_run"] is True
    code, [envelope] = run(app, ["approve", "--raw-payload", '{"yes": true}'])
    assert code == 0 and envelope["data"]["effect"] == "updated"  # type: ignore[index]
    lines = '{"_cmd": "approve"}\n{"_cmd": "approve", "proposal": "p2", "yes": true}\n'
    code, [preview, ran] = run(app, ["exec"], stdin=lines)
    assert code == 0
    assert meta(preview)["dry_run"] is True
    assert ran["data"] == {"effect": "updated", "proposal": "p2"}
    called = app.call("approve", {}, env=ENV)
    assert called.ok and called.data == {"effect": "would_update", "proposal": "p1"}
    assert called.to_json()["meta"]["dry_run"] is True  # type: ignore[index]
    called = app.call("approve", {"proposal": "p3", "yes": True}, env=ENV)
    assert called.ok and called.data == {"effect": "updated", "proposal": "p3"}
    assert applied == ["p1", "p2", "p3"]


def test_exec_dry_run_wins_over_a_passed_confirmation() -> None:
    applied: list[str] = []
    line = '{"_cmd": "approve", "yes": true}\n'
    code, [envelope] = run(approve_app(applied), ["exec", "--dry-run"], stdin=line)
    assert code == 0, envelope
    assert envelope["data"] == {"effect": "would_update", "proposal": "p1"}
    assert meta(envelope)["dry_run"] is True
    assert applied == []


def test_a_preview_is_not_recorded_under_its_idempotency_key(tmp_path: Path) -> None:
    applied: list[str] = []
    app = approve_app(applied)
    env = {"CF_STATE_DIR": str(tmp_path)}
    code, [preview] = run(app, ["approve", "--idempotency-key", "k1"], env=env)
    assert code == 0 and meta(preview)["dry_run"] is True
    code, [ran] = run(app, ["approve", "--yes", "--idempotency-key", "k1"], env=env)
    assert code == 0 and ran["data"]["effect"] == "updated"  # type: ignore[index]
    code, [replayed] = run(app, ["approve", "--yes", "--idempotency-key", "k1"], env=env)
    assert code == 0 and applied == ["p1"]
    assert replayed["data"]["effect"] == "noop"  # type: ignore[index]


def test_the_manifest_says_the_command_previews_unless_confirmed() -> None:
    app = approve_app([])
    manifest = app.manifest()
    entry = manifest["commands"]["approve"]["flags"]["yes"]  # type: ignore[index]
    assert entry["type"] == "boolean" and entry["default"] is False
    assert "previews" in entry["description"] and "would_*" in entry["description"]
    spec_validator("manifest-response").validate(manifest)
    assert "Previews unless --yes" in render(app)["SKILL-approve.md"]


def register(args_type: type, danger_level: str = "mutating", **extra: object) -> None:
    app = App("cf", version="1.0.0")

    def handler(args: object, ctx: Ctx) -> dict[str, object]:
        return {"effect": "noop", "would_affect": None}

    handler.__annotations__["args"] = args_type
    app.command("x", description="x", danger_level=danger_level, exit_codes=(), **extra)(handler)  # type: ignore[arg-type]


def test_a_confirmation_belongs_to_a_mutating_command() -> None:
    with pytest.raises(RegistrationError, match="safe command changes nothing"):
        register(ApproveArgs, "safe")
    with pytest.raises(RegistrationError, match="--confirm-destructive"):
        register(ApproveArgs, "destructive")


@dataclass(frozen=True, slots=True)
class BothArgs:
    dry_run: bool = Flag(default=False, description="Preview")
    yes: bool = Flag(confirm=True, description="Apply")


@dataclass(frozen=True, slots=True)
class MarkedArgs:
    check: bool = Flag(default=False, dry_run=True, description="Preview")
    yes: bool = Flag(confirm=True, description="Apply")


@dataclass(frozen=True, slots=True)
class TwoArgs:
    yes: bool = Flag(confirm=True, description="Apply")
    go: bool = Flag(confirm=True, description="Apply too")


@dataclass(frozen=True, slots=True)
class NumberArgs:
    yes: int = Flag(confirm=True, description="Apply")


@pytest.mark.parametrize(
    ("args_type", "message"),
    [
        (BothArgs, "second dry-run switch"),
        (MarkedArgs, "second dry-run switch"),
        (TwoArgs, "one confirmation switch"),
        (NumberArgs, "boolean"),
    ],
)
def test_one_switch_per_command(args_type: type, message: str) -> None:
    with pytest.raises(RegistrationError, match=message):
        register(args_type)


def test_a_confirmation_is_a_boolean_off_by_default() -> None:
    with pytest.raises(RegistrationError, match="default is False"):
        Flag(confirm=True, default=True, description="Apply")
    with pytest.raises(RegistrationError, match="opposite switches"):
        Flag(confirm=True, dry_run=True, description="Apply")
    with pytest.raises(RegistrationError, match="default is False"):
        Flag(confirm=True, default="", description="Apply")
