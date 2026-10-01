"""idempotent=True: a mutating command whose repeat leaves the same state, so the
retryable audit rule passes its retryable codes and the manifest says so (#210)"""

import io
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import spec_validator

from treaty import App, Ctx, Exit, NoArgs, RegistrationError
from treaty._audit import audit
from treaty._command import IDEMPOTENT_NOTE


@dataclass(frozen=True, slots=True)
class Observed:
    effect: str
    run: int


def make_app(state: Path, *, idempotent: bool) -> tuple[App, list[int]]:
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

    @app.command(
        "observe",
        description="Snapshot every host",
        danger_level="mutating",
        exit_codes=["INCOMPLETE"],
        idempotent=idempotent,
    )
    def observe(args: NoArgs, ctx: Ctx) -> Observed:
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise Exit.INCOMPLETE("one host gave no snapshot")
        return Observed("updated", len(calls))

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


@pytest.mark.parametrize(
    ("danger_level", "message"),
    [
        ("safe", "a safe command changes nothing"),
        ("destructive", "a destructive command's repeat after a partial run"),
    ],
)
def test_idempotent_is_refused_off_a_mutating_command(danger_level: str, message: str) -> None:
    app = App("x", version="1.0.0")
    with pytest.raises(RegistrationError, match=message):

        @app.command(
            "go", description="Go", danger_level=danger_level, exit_codes=(), idempotent=True
        )
        def go(args: NoArgs, ctx: Ctx) -> dict[str, str]:
            return {}
