"""Flag(dry_run=True): a command's dry-run switch under its own name, such as a wrapped
tool's --check or --noop (#63)."""

import io
import json
from dataclasses import dataclass

import pytest

from treaty import Affects, App, Arg, Ctx, Flag, RegistrationError
from treaty._profile import probes_for
from treaty._skills import render

APPLIED: list[str] = []


@dataclass(frozen=True, slots=True)
class RunArgs:
    check: bool = Flag(default=False, dry_run=True, description="Ansible check mode")


@dataclass(frozen=True, slots=True)
class UnmarkedRunArgs:
    check: bool = Flag(default=False, description="Ansible check mode")


@dataclass(frozen=True, slots=True)
class Played:
    effect: str


@dataclass(frozen=True, slots=True)
class Revert:
    target: str = Arg(description="What to revert")
    noop: bool = Flag(default=False, dry_run=True, description="Puppet noop mode")


@dataclass(frozen=True, slots=True)
class Reverted:
    effect: str
    target: str
    would_affect: Affects | None = None


def play_app(*, marked: bool) -> App:
    app = App("checks", version="1.0.0")
    register = app.command("play", description="Converge", danger_level="mutating", exit_codes=())

    if marked:

        @register
        def play(args: RunArgs, _ctx: Ctx) -> Played:
            return Played("would_update" if args.check else "updated")

    else:

        @register
        def play_unmarked(args: UnmarkedRunArgs, _ctx: Ctx) -> Played:
            return Played("would_update" if args.check else "updated")

    return app


def revert_app(*, safe_default: bool = False) -> App:
    app = App("puppet", version="1.0.0")

    @app.command(
        "revert",
        description="Revert a resource",
        danger_level="destructive",
        exit_codes=(),
        safe_default=safe_default,
        examples=[("Preview a revert", "puppet revert web --noop")],
    )
    def revert(args: Revert, _ctx: Ctx) -> Reverted:
        if args.noop:
            preview = Affects(f"Reverts {args.target}", (args.target,), 1)
            return Reverted("would_update", args.target, preview)
        APPLIED.append(args.target)
        return Reverted("updated", args.target)

    return app


def run(app: App, argv: list[str], stdin: str = "") -> tuple[int, dict[str, object]]:
    APPLIED.clear()
    out = io.StringIO()
    code = app.run(
        argv, stdin=io.StringIO(stdin), stdout=out, stderr=io.StringIO(), env={}, isatty=False
    )
    envelope = json.loads(out.getvalue().splitlines()[-1])
    assert isinstance(envelope, dict)
    return code, envelope


def test_issue_repro_marked_check_may_return_a_would_effect() -> None:
    code, env = run(play_app(marked=True), ["play", "--check"])
    assert code == 0 and env["data"] == {"effect": "would_update"}
    code, env = run(play_app(marked=True), ["play"])
    assert code == 0 and env["data"] == {"effect": "updated"}


def test_unmarked_check_is_not_a_dry_run() -> None:
    code, env = run(play_app(marked=False), ["play", "--check"])
    assert code == 1 and env["error"]["code"] == "INVALID_EFFECT"  # type: ignore[index]


def test_marked_dry_run_off_rejects_a_would_effect() -> None:
    @dataclass(frozen=True, slots=True)
    class Bad:
        check: bool = Flag(default=False, dry_run=True, description="Check mode")

    app = App("checks", version="1.0.0")

    @app.command("play", description="Converge", danger_level="mutating", exit_codes=())
    def play(args: Bad, _ctx: Ctx) -> Played:
        return Played("would_update")

    code, env = run(app, ["play"])
    assert code == 1 and env["error"]["code"] == "INVALID_EFFECT"  # type: ignore[index]


def test_two_marked_fields_fail_registration() -> None:
    @dataclass(frozen=True, slots=True)
    class Two:
        check: bool = Flag(default=False, dry_run=True, description="Check mode")
        noop: bool = Flag(default=False, dry_run=True, description="Noop mode")

    app = App("x", version="1.0.0")
    with pytest.raises(RegistrationError, match=r"dry_run=True on \['check', 'noop'\]"):

        @app.command("play", description="Converge", danger_level="mutating", exit_codes=())
        def play(args: Two, _ctx: Ctx) -> Played:
            return Played("updated")


def test_a_marked_non_boolean_fails_registration() -> None:
    @dataclass(frozen=True, slots=True)
    class Text:
        check: str = Flag(default="no", dry_run=True, description="Check mode")

    app = App("x", version="1.0.0")
    with pytest.raises(RegistrationError, match=r"Text.check: dry_run=True marks a boolean"):

        @app.command("play", description="Converge", danger_level="mutating", exit_codes=())
        def play(args: Text, _ctx: Ctx) -> Played:
            return Played("updated")


@pytest.mark.parametrize("marker", ["no", 1, None])
def test_a_non_bool_marker_fails_at_flag(marker: object) -> None:
    """A truthy string such as "no" would otherwise mark the field silently"""
    with pytest.raises(RegistrationError, match=r"dry_run is True or False"):
        Flag(default=False, dry_run=marker, description="Force")  # type: ignore[arg-type]


def test_a_marked_field_beside_a_dry_run_field_fails_registration() -> None:
    @dataclass(frozen=True, slots=True)
    class Both:
        check: bool = Flag(default=False, dry_run=True, description="Check mode")
        dry_run: bool = Flag(default=False, description="Preview only")

    app = App("x", version="1.0.0")
    with pytest.raises(RegistrationError, match="a field named dry_run is also declared"):

        @app.command("play", description="Converge", danger_level="mutating", exit_codes=())
        def play(args: Both, _ctx: Ctx) -> Played:
            return Played("updated")


def test_dry_run_field_may_mark_itself() -> None:
    @dataclass(frozen=True, slots=True)
    class Self:
        dry_run: bool = Flag(default=False, dry_run=True, description="Preview only")

    app = App("x", version="1.0.0")

    @app.command("play", description="Converge", danger_level="mutating", exit_codes=())
    def play(args: Self, _ctx: Ctx) -> Played:
        return Played("would_update" if args.dry_run else "updated")

    code, env = run(app, ["play", "--dry-run"])
    assert code == 0 and env["data"] == {"effect": "would_update"}


def test_destructive_command_with_noop_needs_no_dry_run_field() -> None:
    code, env = run(revert_app(), ["revert", "web", "--noop"])
    assert code == 0 and env["data"]["effect"] == "would_update"  # type: ignore[index]
    assert APPLIED == []
    assert run(revert_app(), ["revert", "web", "--dry-run"])[0] == 2


def test_destructive_unconfirmed_run_previews_through_noop() -> None:
    code, env = run(revert_app(), ["revert", "web"])
    assert code == 2 and env["error"]["code"] == "CONFIRMATION_REQUIRED"  # type: ignore[index]
    assert env["data"]["would_affect"]["summary"] == "Reverts web"  # type: ignore[index]
    assert APPLIED == []


def test_destructive_confirmed_run_applies() -> None:
    code, env = run(revert_app(), ["revert", "web", "--confirm-destructive"])
    assert code == 0 and env["data"]["effect"] == "updated"  # type: ignore[index]
    assert APPLIED == ["web"]


def test_exec_dry_run_turns_on_the_marked_flag() -> None:
    line = json.dumps({"_cmd": "revert", "target": "web"}) + "\n"
    code, env = run(revert_app(), ["exec", "--dry-run"], stdin=line)
    assert code == 0 and env["data"]["effect"] == "would_update"  # type: ignore[index]
    assert APPLIED == []


def test_safe_default_sets_meta_dry_run_through_the_marked_flag() -> None:
    code, env = run(revert_app(safe_default=True), ["revert", "web"])
    assert code == 0 and env["meta"]["dry_run"] is True and APPLIED == []  # type: ignore[index]
    code, env = run(revert_app(safe_default=True), ["revert", "web", "--live", "--noop"])
    assert code == 0 and env["meta"]["dry_run"] is True and APPLIED == []  # type: ignore[index]
    code, env = run(revert_app(safe_default=True), ["revert", "web", "--live"])
    assert code == 0 and env["meta"]["dry_run"] is False and APPLIED == ["web"]  # type: ignore[index]


def test_a_destructive_command_without_a_switch_names_the_marker() -> None:
    @dataclass(frozen=True, slots=True)
    class Plain:
        noop: bool = Flag(default=False, description="Noop mode")

    app = App("x", version="1.0.0")
    with pytest.raises(RegistrationError, match=r"Flag\(dry_run=True\) \(REQ-C-004\)"):

        @app.command("revert", description="Revert", danger_level="destructive", exit_codes=())
        def revert(args: Plain, _ctx: Ctx) -> Reverted:
            return Reverted("updated", "x")


def test_conformance_probe_and_skill_use_the_marked_flag() -> None:
    app = revert_app()
    probe = next(p for p in probes_for(app) if p.kind == "destructive")
    assert probe.argv == ("revert", "web") and probe.dry_run_flag == "--noop"
    skill = render(app)["SKILL-revert.md"]
    assert "run with --noop first" in skill and "--dry-run" not in skill


def test_manifest_lists_the_marked_flag_under_its_own_name() -> None:
    manifest = revert_app().manifest()
    flags = manifest["commands"]["revert"]["flags"]  # type: ignore[index]
    assert "noop" in flags and "dry-run" not in flags
