"""idempotent=True: a command whose repeat leaves the same state, so the retryable audit
rule passes its retryable codes and the manifest says so, on any danger level (#210, #226)"""

import io
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import spec_validator

from treaty import Affects, App, Ctx, Exit, Flag, NoArgs
from treaty._audit import audit
from treaty._command import IDEMPOTENT_NOTE


@dataclass(frozen=True, slots=True)
class DryRun:
    dry_run: bool = Flag(default=False, description="Preview only")


@dataclass(frozen=True, slots=True)
class Observed:
    effect: str
    run: int


@dataclass(frozen=True, slots=True)
class Removed:
    effect: str
    run: int
    would_affect: Affects | None = None


def make_app(
    state: Path, *, idempotent: bool, danger_level: str = "mutating"
) -> tuple[App, list[int]]:
    calls: list[int] = []
    app = App("cloudctl", version="1.0.0", state_dir=state)
    app.exit_code(
        "INCOMPLETE",
        79,
        description="Some hosts gave no snapshot",
        retryable=True,
        side_effects="none",
        suggestion="run it again",
    )

    register = app.command(
        "observe",
        description="Snapshot every host",
        danger_level=danger_level,
        exit_codes=["INCOMPLETE"],
        idempotent=idempotent,
    )

    def snapshot() -> Observed:
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise Exit.INCOMPLETE("one host gave no snapshot")
        return Observed("updated", len(calls))

    if danger_level == "destructive":  # REQ-C-004: a destructive command takes --dry-run

        @register
        def observe_destructive(args: DryRun, ctx: Ctx) -> Removed:
            done = snapshot()
            return Removed(done.effect, done.run)

    else:

        @register
        def observe(args: NoArgs, ctx: Ctx) -> Observed:
            return snapshot()

    return app, calls


def retryable_findings(app: App) -> list[tuple[str, str]]:
    rule = next(r for r in audit(app, "x:app", limit=3).rules if r.id == "retryable")
    return [(f.command, f.fix) for f in rule.findings]


def test_a_retryable_code_on_a_mutating_command_names_idempotent_in_its_fix(tmp_path) -> None:
    app, _ = make_app(tmp_path, idempotent=False)
    [(command, fix)] = retryable_findings(app)
    assert command == "observe"
    assert fix.startswith("idempotent=True if a repeat of observe leaves the same state")
    assert "INCOMPLETE with retryable=False" in fix


def test_an_idempotent_command_passes_the_retryable_rule(tmp_path) -> None:
    app, _ = make_app(tmp_path, idempotent=True)
    assert retryable_findings(app) == []


def test_the_manifest_description_says_the_command_is_idempotent(tmp_path) -> None:
    app, _ = make_app(tmp_path, idempotent=True)
    manifest = app.manifest()
    spec_validator("manifest-response").validate(manifest)
    assert manifest["commands"]["observe"]["description"] == (
        f"Snapshot every host. {IDEMPOTENT_NOTE}"
    )
    plain, _ = make_app(tmp_path, idempotent=False)
    assert plain.manifest()["commands"]["observe"]["description"] == "Snapshot every host"


def run(app: App, argv: list[str]) -> tuple[int, dict]:
    out = io.StringIO()
    code = app.run(argv, stdin=io.StringIO(), stdout=out, stderr=io.StringIO(), env={})
    return code, json.loads(out.getvalue().splitlines()[0])


def test_idempotent_leaves_idempotency_key_replay_as_it_was(tmp_path) -> None:
    """The declaration changes no run: a key still replays the first result"""
    app, calls = make_app(tmp_path, idempotent=True)
    code, first = run(app, ["observe", "--idempotency-key", "k1"])
    assert code == 79 and first["error"]["code"] == "INCOMPLETE"
    code, second = run(app, ["observe", "--idempotency-key", "k2"])
    assert code == 0 and second["data"] == {"effect": "updated", "run": 2}
    code, replay = run(app, ["observe", "--idempotency-key", "k2"])
    assert code == 0 and replay["data"]["run"] == 2
    assert calls == [1, 2]


DANGER_LEVELS = pytest.mark.parametrize("danger_level", ["safe", "mutating", "destructive"])


@DANGER_LEVELS
def test_idempotent_registers_on_any_danger_level(tmp_path, danger_level: str) -> None:
    """REQ-C-002: registration succeeds for any danger_level and leaves every declared
    ExitCodeEntry unchanged; on a safe command it is redundant and accepted silently"""
    app, _ = make_app(tmp_path, idempotent=True, danger_level=danger_level)
    plain, _ = make_app(tmp_path, idempotent=False, danger_level=danger_level)
    command = app.manifest()["commands"]["observe"]
    assert command["danger_level"] == danger_level
    assert command["exit_codes"] == plain.manifest()["commands"]["observe"]["exit_codes"]
    assert retryable_findings(app) == []


@DANGER_LEVELS
def test_the_manifest_says_idempotent_on_any_danger_level(tmp_path, danger_level: str) -> None:
    """A command registered with idempotent shows it in the manifest, whatever its level"""
    app, _ = make_app(tmp_path, idempotent=True, danger_level=danger_level)
    manifest = app.manifest()
    spec_validator("manifest-response").validate(manifest)
    assert manifest["commands"]["observe"]["description"].endswith(IDEMPOTENT_NOTE)


def test_a_retryable_code_on_a_destructive_command_names_idempotent_in_its_fix(
    tmp_path,
) -> None:
    """An idempotent destructive command passes the rule as a mutating one does, so the fix
    offers the declaration there too"""
    app, _ = make_app(tmp_path, idempotent=False, danger_level="destructive")
    [(command, fix)] = retryable_findings(app)
    assert command == "observe"
    assert fix.startswith("idempotent=True if a repeat of observe leaves the same state")
    assert (
        retryable_findings(make_app(tmp_path, idempotent=True, danger_level="destructive")[0]) == []
    )
