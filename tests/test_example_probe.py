"""``Example(probe=...)``: the argv a generated conformance probe runs, or False to keep the
example out of the profile, so a profile with fixtures regenerates without a CONFLICT (#392)"""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from examples.tutorial import todo_exit_codes
from treaty import App, Arg, Ctx, Example, Flag, NoArgs
from treaty._audit import audit
from treaty._errors import RegistrationError
from treaty._profile import argument_order_for, build_profile, probes_for, write_profile

TUTORIAL_PROFILE = (
    Path(__file__).resolve().parents[1] / "examples" / "tutorial" / "conformance" / "todo.json"
)


@dataclass(frozen=True, slots=True)
class Doc:
    path: str = Arg(description="The document to profile")


@dataclass(frozen=True, slots=True)
class Count:
    n: int = Flag(default=1, description="How many")


@dataclass(frozen=True, slots=True)
class Wipe:
    keep: int = Flag(default=0, description="How many to keep")
    dry_run: bool = Flag(default=False, description="Preview")


def demo_app(*, probed: bool) -> App:
    """The same commands, with ``probe=`` on their examples or without"""
    app = App("demo", version="1.0.0")

    def given(description: str, command: str, probe: str | bool | None) -> Example:
        if not probed or probe is None:
            return Example(description, command)
        assert probe is False or isinstance(probe, str)
        return Example(description, command, probe=probe)

    @app.command(
        "profile",
        description="Profile a document",
        danger_level="safe",
        exit_codes=(),
        examples=[
            given(
                "Profile a lease",
                "demo profile lease.pdf",
                "demo profile conformance/fixtures/lease.pdf --format json",
            )
        ],
    )
    def profile(args: Doc, ctx: Ctx) -> dict[str, str]:
        return {"path": args.path}

    @app.command(
        "count",
        description="Count things",
        danger_level="safe",
        exit_codes=(),
        examples=[
            given("Count once", "demo count", False),
            given("Count twice", "demo count --n 2", None),
        ],
    )
    def count(args: Count, ctx: Ctx) -> dict[str, int]:
        return {"n": args.n}

    @app.command(
        "ping",
        description="Ping a host",
        danger_level="safe",
        exit_codes=(),
        examples=[given("Ping", "demo ping", False)],
    )
    def ping(args: NoArgs, ctx: Ctx) -> dict[str, bool]:
        return {"ok": True}

    @app.command(
        "fetch",
        description="Fetch a page",
        danger_level="safe",
        exit_codes=(),
        has_network_io=True,
        examples=[given("Fetch", "demo fetch https://example.com", "demo fetch stub.html")],
    )
    def fetch(args: Doc, ctx: Ctx) -> dict[str, str]:
        return {"path": args.path}

    @app.command(
        "wipe",
        description="Wipe old items",
        danger_level="destructive",
        exit_codes=(),
        examples=[
            given(
                "Keep three",
                "demo wipe --keep 3 --confirm-destructive",
                "demo wipe --keep 1 --confirm-destructive",
            )
        ],
    )
    def wipe(args: Wipe, ctx: Ctx) -> dict[str, bool]:
        return {"dry_run": args.dry_run}

    @app.command(
        "ingest",
        description="Hand argv to a tool",
        danger_level="safe",
        exit_codes=(),
        passthrough=True,
        examples=[
            given(
                "Extract",
                "demo ingest extract statement.csv",
                "demo --format json ingest extract fixtures/statement.csv",
            )
        ],
    )
    def ingest(args: NoArgs, ctx: Ctx) -> int:
        return 0

    return app


def probes(app: App) -> dict[str, dict[str, object]]:
    profile = build_profile(app, ["demo"], probes_for(app))
    listed = profile["probes"]
    assert isinstance(listed, list)
    return {p["name"]: p for p in listed}


def test_a_probe_argv_replaces_the_example_command_in_the_profile() -> None:
    got = probes(demo_app(probed=True))
    # Its globals go, as an example's do: the kit moves --format itself
    assert got["profile"]["argv"] == ["profile", "conformance/fixtures/lease.pdf"]
    assert got["fetch"]["argv"] == ["fetch", "stub.html"]
    # A destructive probe is never confirmed: the kit adds the dry-run flag (#373)
    assert got["wipe"] == {
        "name": "wipe",
        "argv": ["wipe", "--keep", "1"],
        "kind": "destructive",
        "dry_run_flag": "--dry-run",
    }
    # A passthrough command gets no probe, a probe= of its own included (#386)
    assert not [name for name in got if name.startswith("ingest")]
    order = argument_order_for(demo_app(probed=True))
    assert order is not None and order["command_path"] == ["count"]


def test_probe_false_keeps_an_example_out_of_the_profile() -> None:
    got = probes(demo_app(probed=True))
    # The next example stands in for one left out
    assert got["count"]["argv"] == ["count", "--n", "2"]
    # A command whose every example is left out gets no probe, not even its bare path
    assert not [name for name in got if name.startswith("ping")]
    assert "ping" in probes(demo_app(probed=False))


def test_an_app_without_probe_generates_the_same_profile(tmp_path: Path) -> None:
    """Additive: Example objects without probe= write what (description, command) pairs do,
    and the committed tutorial profile is unchanged byte for byte"""
    app = todo_exit_codes.app
    write_profile(
        build_profile(app, ["./todo"], probes_for(app), beside_profile=True), tmp_path / "a"
    )
    assert (tmp_path / "a").read_bytes() == TUTORIAL_PROFILE.read_bytes()

    paired = App("demo", version="1.0.0")
    wrapped = App("demo", version="1.0.0")
    for target, examples in (
        (paired, [("Count twice", "demo count --n 2")]),
        (wrapped, [Example("Count twice", "demo count --n 2")]),
    ):

        @target.command(
            "count", description="Count", danger_level="safe", exit_codes=(), examples=examples
        )
        def count(args: Count, ctx: Ctx) -> dict[str, int]:
            return {"n": args.n}

    want = json.dumps(build_profile(paired, ["demo"], probes_for(paired)))
    assert json.dumps(build_profile(wrapped, ["demo"], probes_for(wrapped))) == want
    # The manifest keeps an example's description and command only
    (command,) = [c for p, c in wrapped.commands.items() if p.value == "count"]
    assert command.examples[0].to_json() == {
        "description": "Count twice",
        "command": "demo count --n 2",
    }


def register(probe: object, *, examples: object = None) -> None:
    app = App("demo", version="1.0.0")

    @app.command("count", description="Count", danger_level="safe", exit_codes=())
    def count(args: Count, ctx: Ctx) -> dict[str, int]:
        return {"n": args.n}

    given = examples if examples is not None else [Example("Count", "demo count", probe=probe)]  # type: ignore[arg-type]

    @app.command("show", description="Show", danger_level="safe", exit_codes=(), examples=given)  # type: ignore[arg-type]
    def show(args: Count, ctx: Ctx) -> dict[str, int]:
        return {"n": args.n}


@pytest.mark.parametrize(
    ("probe", "message"),
    [
        ("other show --n 2", "starts with 'other'; a probe starts with the app's name, 'demo'"),
        ("demo count --n 2", "does not run 'show'"),
        ("demo --format json", "does not run 'show'"),
        ("demo show 'unbalanced", "is not a valid shell command"),
        ("", "probe= is empty"),
        ("   ", "probe= is empty"),
        (True, "probe= takes the probe's argv as a string, or False"),
        (["demo", "show"], "probe= takes the probe's argv as a string, or False"),
    ],
)
def test_a_malformed_probe_is_refused_at_registration(probe: object, message: str) -> None:
    with pytest.raises(RegistrationError, match=message.replace("(", r"\(")):
        register(probe)


def test_a_probe_with_globals_before_the_path_is_accepted() -> None:
    register("demo --format json show --n 2")


def test_a_single_example_object_is_refused_with_the_list_to_write() -> None:
    with pytest.raises(RegistrationError, match=r"write examples=\[Example\("):
        register(None, examples=Example("Show", "demo show"))


def test_the_audit_checks_a_probe_as_it_checks_an_example() -> None:
    app = App("demo", version="1.0.0")

    @app.command(
        "show",
        description="Show",
        danger_level="safe",
        exit_codes=(),
        examples=[Example("Show two", "demo show --n 2", probe="demo show --n many")],
    )
    def show(args: Count, ctx: Ctx) -> dict[str, int]:
        return {"n": args.n}

    errors = [
        f for r in audit(app, "x:app", limit=3).rules if r.id == "describe" for f in r.findings
    ]
    assert [f.message.split(" does not parse")[0] for f in errors] == [
        "the probe 'demo show --n many' of example 'demo show --n 2'"
    ]


@dataclass(frozen=True, slots=True)
class Pages:
    path: str = Arg(description="The document")
    pages: int = Flag(default=1, description="Pages to read")


@pytest.mark.parametrize(
    "probe",
    [
        "demo profile fixtures/lease.pdf --output bundle.json --pages 2",
        "demo profile fixtures/lease.pdf --output=bundle.json --pages 2",
    ],
    ids=["spaced", "inline"],
)
def test_a_probe_of_an_output_file_command_writes_no_file(probe: str) -> None:
    """An explicit probe= loses --output as an example does: every kit run would write the
    file (#391, #392)"""
    app = App("demo", version="1.0.0")

    @app.command(
        "profile",
        description="Profile a document and save it",
        danger_level="safe",
        exit_codes=(),
        output_file=True,
        examples=[Example("Profile", "demo profile lease.pdf --output out.json", probe=probe)],
    )
    def profile(args: Pages, ctx: Ctx) -> dict[str, int]:
        return {"pages": args.pages}

    got = {p.name: p.argv for p in probes_for(app)}
    assert got["profile"] == ("profile", "fixtures/lease.pdf", "--pages", "2")
    assert got["unknown flag"] == (*got["profile"], "--no-such-flag")
    order = argument_order_for(app)
    assert order is not None and order["local_args"] == ["--pages", "2"]


def network_app(given: Example) -> App:
    app = App("demo", version="1.0.0")

    @app.command(
        "summarize",
        description="Summarize a document with a remote API",
        danger_level="safe",
        exit_codes=(),
        has_network_io=True,
        examples=[given],
    )
    def summarize(args: Pages, ctx: Ctx) -> dict[str, int]:
        return {"pages": args.pages}

    return app


def test_a_probe_gives_a_network_command_back_its_read_probe() -> None:
    """The author's probe= points the command at a local stub, so its read runs and it may
    give argument_order; without probe= neither runs the real requests (#390, #392)"""
    stubbed = network_app(
        Example(
            "Summarize",
            "demo summarize lease.pdf --pages 2",
            probe="demo summarize stub.pdf --pages 3",
        )
    )
    got = {p.name: p for p in probes_for(stubbed)}
    assert got["summarize"].kind == "read"
    assert got["summarize"].argv == ("summarize", "stub.pdf", "--pages", "3")
    order = argument_order_for(stubbed)
    assert order is not None and order["command_path"] == ["summarize", "stub.pdf"]

    real = network_app(Example("Summarize", "demo summarize lease.pdf --pages 2"))
    assert "summarize" not in {p.name for p in probes_for(real)}
    order = argument_order_for(real)
    assert order is not None and order["command_path"] == ["manifest"]
