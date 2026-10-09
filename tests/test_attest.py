"""A confirmation only a person at a terminal gives: ``requires_person=True`` and
``ctx.attest`` (#424). No flag answers it, ``--yes`` included."""

import io
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Literal

import pytest
from conftest import spec_validator

from treaty import App, Arg, Attestation, Background, Ctx, NoArgs, RegistrationError
from treaty._agents_md import check, render_file
from treaty._manifest import PERSON_RUNS
from treaty._prompt import Prompter
from treaty._skills import render
from treaty._tools import tool_entries


class Terminal(io.StringIO):
    """Stands in for a terminal on stdin, with the person's typed lines"""

    def isatty(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class ApproveArgs:
    decision: str = Arg(description="Decision id")


@dataclass(frozen=True, slots=True)
class Approved:
    decision: str
    channel: Literal["terminal"]
    at: datetime
    effect: str


def make_app(applied: list[str]) -> App:
    app = App("opsctl", version="1.0.0")

    @app.command(
        "decisions.approve",
        description="Run a decision an agent proposed",
        danger_level="mutating",
        exit_codes=(),
        requires_person=True,
    )
    def approve(args: ApproveArgs, ctx: Ctx) -> Approved:
        try:
            given = ctx.attest(f"Type {args.decision} to approve it", expected=args.decision)
        except Exception:  # noqa: BLE001 - the stray catch-all the refusal must survive
            given = Attestation(channel="terminal", at=datetime.now(UTC))
        applied.append(args.decision)
        return Approved(args.decision, given.channel, given.at, "updated")

    @app.command("decisions.list", description="List decisions", danger_level="safe", exit_codes=())
    def listed(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    return app


def run(
    argv: list[str], *, stdin: IO[str] | None = None, isatty: bool = False
) -> tuple[int, dict[str, Any], str, list[str]]:
    applied: list[str] = []
    out, err = io.StringIO(), io.StringIO()
    code = make_app(applied).run(
        [*argv, "--format", "json"],
        stdin=stdin if stdin is not None else io.StringIO(),
        stdout=out,
        stderr=err,
        env={},
        isatty=isatty,
    )
    return code, json.loads(out.getvalue()), err.getvalue(), applied


def test_a_person_at_a_terminal_types_the_text_back() -> None:
    before = datetime.now(UTC)
    code, env, err, applied = run(
        ["decisions", "approve", "d-7"], stdin=Terminal("d-7\n"), isatty=True
    )
    assert code == 0 and applied == ["d-7"]
    assert "Type d-7 to approve it: " in err
    assert env["data"]["channel"] == "terminal"
    assert before <= datetime.fromisoformat(env["data"]["at"]) <= datetime.now(UTC)


def test_off_a_terminal_it_needs_a_person_and_names_no_flag() -> None:
    code, env, _, applied = run(["decisions", "approve", "d-7"])
    error = env["error"]
    assert code == 4 and applied == []
    assert error["code"] == "PERSON_REQUIRED" and error["retryable"] is False
    assert "person" in error["suggestion"] and "--" not in error["suggestion"]
    assert error["context"] == {"prompt": "Type d-7 to approve it", "expected": "d-7"}


@pytest.mark.parametrize("isatty", [False, True])
def test_yes_does_not_answer_it(isatty: bool) -> None:
    stdin = Terminal("") if isatty else None
    code, env, _, applied = run(
        ["decisions", "approve", "d-7", "--yes"], stdin=stdin, isatty=isatty
    )
    assert code == 4 and applied == []
    assert env["error"]["code"] == ("ATTESTATION_MISMATCH" if isatty else "PERSON_REQUIRED")


def test_non_interactive_on_a_terminal_needs_a_person() -> None:
    code, env, _, applied = run(
        ["decisions", "approve", "d-7", "--non-interactive"], stdin=Terminal("d-7\n"), isatty=True
    )
    assert code == 4 and env["error"]["code"] == "PERSON_REQUIRED" and applied == []


@pytest.mark.parametrize("typed", ["y\n", "\n", "d-8\n", ""])
def test_any_other_answer_runs_nothing(typed: str) -> None:
    code, env, _, applied = run(["decisions", "approve", "d-7"], stdin=Terminal(typed), isatty=True)
    assert code == 4 and applied == []
    assert env["error"]["code"] == "ATTESTATION_MISMATCH" and env["error"]["retryable"] is False


def test_surrounding_spaces_are_not_a_mismatch() -> None:
    code, _, _, applied = run(
        ["decisions", "approve", "d-7"], stdin=Terminal("  d-7 \r\n"), isatty=True
    )
    assert code == 0 and applied == ["d-7"]


def test_app_call_and_exec_lines_need_a_person() -> None:
    applied: list[str] = []
    envelope = make_app(applied).call("decisions.approve", {"decision": "d-7", "yes": True})
    assert envelope.exit_code == 4 and applied == []
    assert envelope.error is not None and envelope.error.code == "PERSON_REQUIRED"


def test_attest_without_the_declaration_is_refused_at_registration() -> None:
    app = App("opsctl", version="1.0.0")
    with pytest.raises(RegistrationError, match="requires_person=True"):

        @app.command("go", description="Go", danger_level="safe", exit_codes=())
        def go(args: NoArgs, ctx: Ctx) -> dict[str, str]:
            ctx.attest("Type go", expected="go")
            return {}


def prompter(*, person: bool, typed: str = "go\n") -> Prompter:
    return Prompter(
        command="go",
        declared=True,
        editor_alternatives=(),
        flags=frozenset(),
        interactive=True,
        assume_yes=True,
        stdin=Terminal(typed),
        stderr=io.StringIO(),
        env={},
        person=person,
    )


def test_attest_the_source_scan_missed_is_refused_when_called() -> None:
    with pytest.raises(RegistrationError, match="requires_person=True"):
        prompter(person=False).attest("Type go", "go")


@pytest.mark.parametrize("expected", ["", "  ", "a\nb"])
def test_the_expected_text_is_one_line_and_not_blank(expected: str) -> None:
    with pytest.raises(RegistrationError, match="one line, not blank"):
        prompter(person=True).attest("Type it", expected)


def test_assume_yes_is_no_answer() -> None:
    assert prompter(person=True).attest("Type go", "go").channel == "terminal"


def test_the_declaration_implies_interactive_and_no_mcp_tool() -> None:
    app = make_app([])
    built = app.manifest()
    spec_validator("manifest-response").validate(built)
    entry = built["commands"]["decisions.approve"]
    assert entry["description"].endswith(f"{PERSON_RUNS} (not an MCP tool)")
    assert entry["interactive"] is True and entry["mcp"] is False
    assert "requires_person" not in entry  # CommandEntry has no such key
    assert "requires_person" not in built["commands"]["decisions.list"]
    tools = [e.name for e in tool_entries(app)]
    assert "decisions_list" in tools and "decisions_approve" not in tools


def test_schema_and_help_say_a_person_runs_it() -> None:
    app = make_app([])
    out = io.StringIO()
    argv = ["decisions", "approve", "--schema"]
    assert app.run(argv, stdout=out, stderr=io.StringIO(), env={}, isatty=False) == 0
    schema = json.loads(out.getvalue())["data"]
    assert schema["requires_person"] is True
    assert "except the one a person types" in schema["flags"]["yes"]["description"]
    out = io.StringIO()
    argv = ["decisions", "approve", "--help", "--format", "plain"]
    assert app.run(argv, stdout=out, stderr=io.StringIO(), env={}, isatty=False) == 0
    assert "A person runs this at a terminal" in out.getvalue()


def test_the_skill_file_and_agents_md_say_hand_it_to_a_person() -> None:
    app = make_app([])
    skill = render(app)["SKILL-decisions-approve.md"]
    assert "hand the command to a person" in skill
    agents = render_file(app, None, "opsctl:app", "opsctl")
    assert "`opsctl decisions approve` needs a person" in agents
    assert check(app, Path("AGENTS.md"), agents, agents_md=True) == []


@dataclass(frozen=True, slots=True)
class Stepped:
    effect: str


def test_a_person_only_command_cannot_be_resumed_past_its_confirmation() -> None:
    # #426: --resume-from two skipped the attest inside step one, so a flag answered it
    app = App("opsctl", version="1.0.0")
    with pytest.raises(RegistrationError) as raised:

        @app.command("go", description="Go", danger_level="mutating", exit_codes=(),
                     requires_person=True, steps=("one", "two"), resumable=True)  # fmt: skip
        def go(args: NoArgs, ctx: Ctx) -> Stepped:
            if ctx.step("one"):
                ctx.attest("Type go", expected="go")
            ctx.step("two")
            return Stepped("updated")

    message = str(raised.value)
    assert "requires_person=True" in message and "resumable=True" in message
    assert "--resume-from" in message and "confirmation" in message


def test_a_person_only_command_may_still_have_steps() -> None:
    app = App("opsctl", version="1.0.0")

    @app.command("go", description="Go", danger_level="mutating", exit_codes=(),
                 requires_person=True, steps=("one", "two"))  # fmt: skip
    def go(args: NoArgs, ctx: Ctx) -> Stepped:
        if ctx.step("one"):
            ctx.attest("Type go", expected="go")
        ctx.step("two")
        return Stepped("updated")

    out = io.StringIO()
    argv = ["go", "--resume-from", "two", "--format", "json"]
    code = app.run(argv, stdin=io.StringIO(), stdout=out, stderr=io.StringIO(), env={})
    assert code == 2  # no --resume-from without resumable=True


SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


@dataclass(frozen=True, slots=True)
class Watching:
    background_pid: int
    cleanup_command: str
    effect: str


def test_a_background_command_needs_a_person_before_it_spawns(tmp_path: Path) -> None:
    # background= detaches only what ctx.spawn starts; ctx.attest runs in the foreground
    app = App("opsctl", version="1.0.0", state_dir=tmp_path)
    spawned: list[int] = []

    @app.command(
        "watch",
        description="Start a watcher a person approves",
        danger_level="mutating",
        exit_codes=(),
        requires_person=True,
        background=Background("opsctl unwatch", max_lifetime_seconds=60),
    )
    def watch(args: NoArgs, ctx: Ctx) -> Watching:
        ctx.attest("Type watch to start it", expected="watch")
        started = ctx.spawn(SLEEPER)
        spawned.append(started.pid)
        return Watching(started.pid, f"opsctl unwatch --pid {started.pid}", "created")

    @app.command("unwatch", description="Stop it", danger_level="safe", exit_codes=())
    def unwatch(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    out = io.StringIO()
    code = app.run(
        ["watch", "--yes", "--format", "json"],
        stdin=io.StringIO(),
        stdout=out,
        stderr=io.StringIO(),
        env={},
        isatty=False,
    )
    assert code == 4 and json.loads(out.getvalue())["error"]["code"] == "PERSON_REQUIRED"
    assert spawned == [] and not (tmp_path / "background").exists()


def test_a_passthrough_command_cannot_require_a_person() -> None:
    app = App("opsctl", version="1.0.0")
    with pytest.raises(RegistrationError, match="requires_person=True"):

        @app.command("git", description="Run git", danger_level="mutating", exit_codes=(),
                     passthrough=True, requires_person=True)  # fmt: skip
        def git(args: NoArgs, ctx: Ctx) -> int:
            return 0
