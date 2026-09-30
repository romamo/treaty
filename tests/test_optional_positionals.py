"""Optional positionals: ``Arg(default=...)`` (#57)"""

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest
from conftest import spec_validator

from treaty import App, Arg, Ctx, Flag, RegistrationError, RequiresOne
from treaty._audit import audit
from treaty._command import Command
from treaty._manifest import payload_schema
from treaty._mcp import tool_entries
from treaty._skills import examples


def run(app: App, argv: list[str]) -> tuple[int, dict[str, Any]]:
    out = io.StringIO()
    code = app.run(argv, stdout=out, stderr=io.StringIO(), env={}, isatty=False)
    envelope = json.loads(out.getvalue())
    spec_validator("response-envelope").validate(envelope)
    return code, envelope


def help_text(app: App, argv: list[str]) -> str:
    err = io.StringIO()
    code = app.run([*argv, "--help"], stdout=io.StringIO(), stderr=err, env={}, isatty=False)
    assert code == 0
    return err.getvalue()


def command(app: App, path: str) -> Command:
    return next(c for p, c in app.commands.items() if p.value == path)


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolveArgs:
    query: str | None = Arg(default=None, description="Instrument query or identifier")
    figi: str | None = Flag(default=None, description="Resolve by FIGI instead")


def registry() -> App:
    app = App("reg", version="1.0.0", description="Instrument registry")

    @app.command(
        "resolve",
        description="Resolve an instrument",
        danger_level="safe",
        exit_codes=(),
        supports_raw_payload=True,
        requires=[RequiresOne(("query", "figi"))],
    )
    def resolve(args: ResolveArgs, ctx: Ctx) -> dict[str, str | None]:
        return {"query": args.query, "figi": args.figi}

    return app


@pytest.mark.parametrize(
    ("argv", "data"),
    [
        (["AAPL"], {"query": "AAPL", "figi": None}),
        (["--figi", "BBG000B9XRY4"], {"query": None, "figi": "BBG000B9XRY4"}),
        (["--", "--weird"], {"query": "--weird", "figi": None}),
    ],
)
def test_an_optional_positional_may_be_given_or_left_out(
    argv: list[str], data: dict[str, str | None]
) -> None:
    code, envelope = run(registry(), ["resolve", *argv])
    assert code == 0, envelope
    assert envelope["data"] == data


def test_an_extra_argument_after_an_optional_positional_is_still_an_error() -> None:
    code, envelope = run(registry(), ["resolve", "AAPL", "MSFT"])
    assert code == 2
    assert envelope["error"]["context"]["argument"] == "MSFT"


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        ([], "Pass exactly one of --query or --figi"),
        (["AAPL", "--figi", "BBG000B9XRY4"], "--query and --figi are mutually exclusive"),
    ],
)
def test_exactly_one_of_the_positional_and_the_flag(argv: list[str], message: str) -> None:
    code, envelope = run(registry(), ["resolve", *argv])
    assert code == 2
    assert envelope["error"]["phase"] == "validation"
    assert message in envelope["error"]["message"]


def test_the_documented_pattern_draws_no_conditional_rules_advice() -> None:
    [result] = [r for r in audit(registry(), "reg", limit=3).rules if r.id == "conditional-rules"]
    assert list(result.findings) == []


def test_the_manifest_schema_and_help_say_it_is_optional() -> None:
    app = registry()
    manifest = app.manifest()
    spec_validator("manifest-response").validate(manifest)
    entry = manifest["commands"]["resolve"]
    assert entry["positionals"] == [
        {
            "name": "query",
            "type": "string",
            "required": False,
            "description": "Instrument query or identifier",
        }
    ]
    assert entry["flags"]["query"]["required"] is False
    code, envelope = run(app, ["resolve", "--schema"])
    assert code == 0
    data = envelope["data"]
    assert data["positionals"] == entry["positionals"]
    assert "query" not in data["parameters"].get("required", [])
    assert "reg resolve [query] [flags]" in help_text(app, ["resolve"])


def test_json_callers_may_omit_it() -> None:
    app = registry()
    code, envelope = run(app, ["resolve", "--raw-payload", '{"figi": "BBG000B9XRY4"}'])
    assert (code, envelope["data"]["query"]) == (0, None)
    code, envelope = run(app, ["resolve", "--raw-payload", '{"query": "AAPL"}'])
    assert (code, envelope["data"]["query"]) == (0, "AAPL")
    called = app.call("resolve", {"figi": "BBG000B9XRY4"})
    assert called.ok and called.data == {"query": None, "figi": "BBG000B9XRY4"}
    assert "required" not in payload_schema(command(app, "resolve"))
    [tool] = [t for t in tool_entries(app) if t.name == "resolve"]
    assert "query" not in tool.input_schema.get("required", [])


def test_skills_leave_it_out_of_the_minimal_call_and_completion_counts_it() -> None:
    app = registry()
    entry = app.manifest()["commands"]["resolve"]
    resolve = command(app, "resolve")
    assert examples("reg", resolve.path, entry)[0] == "reg resolve"
    out = io.StringIO()
    app.run(["completion", "bash", "--format", "plain"], stdout=out, env={}, isatty=False)
    resolve_node = out.getvalue().split("    resolve)\n", 1)[1].split(";;", 1)[0]
    assert "slots=1" in resolve_node


@dataclass(frozen=True, slots=True)
class CopyArgs:
    source: str = Arg(description="What to copy")
    target: Path = Arg(default=".", description="Where to put it")
    mode: Literal["fast", "safe"] = Arg(default="safe", description="How to copy")


def copier() -> App:
    app = App("cp", version="1.0.0")

    @app.command("copy", description="Copy a file", danger_level="safe", exit_codes=())
    def copy(args: CopyArgs, ctx: Ctx) -> dict[str, str]:
        return {"source": args.source, "target": str(args.target), "mode": args.mode}

    return app


@pytest.mark.parametrize(
    ("argv", "data"),
    [
        (["a"], {"source": "a", "target": ".", "mode": "safe"}),
        (["a", "b"], {"source": "a", "target": "b", "mode": "safe"}),
        (["a", "b", "fast"], {"source": "a", "target": "b", "mode": "fast"}),
    ],
)
def test_optional_positionals_after_a_required_one_fill_in_order(
    argv: list[str], data: dict[str, str]
) -> None:
    code, envelope = run(copier(), ["copy", *argv])
    assert code == 0, envelope
    assert envelope["data"] == data


def test_a_default_is_published_on_the_flag_entry() -> None:
    app = copier()
    manifest = app.manifest()
    spec_validator("manifest-response").validate(manifest)
    entry = manifest["commands"]["copy"]
    assert [p["required"] for p in entry["positionals"]] == [True, False, False]
    assert entry["flags"]["mode"]["default"] == "safe"
    assert entry["flags"]["target"]["default"] == "."
    schema = payload_schema(command(app, "copy"))
    assert schema["required"] == ["source"]
    assert "cp copy <source> [target] [mode] [flags]" in help_text(app, ["copy"])


def test_a_required_positional_after_an_optional_one_is_refused() -> None:
    @dataclass(frozen=True, slots=True, kw_only=True)
    class Backwards:
        first: str | None = Arg(default=None, description="First")
        second: str = Arg(description="Second")

    app = App("t", version="1.0.0")
    with pytest.raises(RegistrationError, match="second: a required positional cannot follow"):

        @app.command("go", description="Go", danger_level="safe", exit_codes=())
        def go(args: Backwards, ctx: Ctx) -> dict[str, str]:
            return {}


@dataclass(frozen=True, slots=True)
class RunArgs:
    first: str | None = Arg(default=None, description="First word")
    rest: tuple[str, ...] = Arg(default=(), description="Every other word")


def runner() -> App:
    app = App("r", version="1.0.0")

    @app.command("say", description="Say words", danger_level="safe", exit_codes=())
    def say(args: RunArgs, ctx: Ctx) -> dict[str, object]:
        return {"first": args.first, "rest": list(args.rest)}

    return app


@pytest.mark.parametrize(
    ("argv", "data"),
    [
        ([], {"first": None, "rest": []}),
        (["a"], {"first": "a", "rest": []}),
        (["a", "b", "c"], {"first": "a", "rest": ["b", "c"]}),
    ],
)
def test_an_optional_positional_before_an_optional_variadic_takes_the_first_value(
    argv: list[str], data: dict[str, object]
) -> None:
    code, envelope = run(runner(), ["say", *argv])
    assert code == 0, envelope
    assert envelope["data"] == data


def test_a_required_variadic_after_an_optional_positional_is_refused() -> None:
    @dataclass(frozen=True, slots=True, kw_only=True)
    class Greedy:
        first: str | None = Arg(default=None, description="First word")
        rest: tuple[str, ...] = Arg(description="Every other word")

    app = App("t", version="1.0.0")
    with pytest.raises(RegistrationError, match="rest: a required positional cannot follow"):

        @app.command("go", description="Go", danger_level="safe", exit_codes=())
        def go(args: Greedy, ctx: Ctx) -> dict[str, str]:
            return {}


@dataclass(frozen=True, slots=True)
class BadPattern:
    slug: str = Arg(default="Not A Slug", description="Slug", pattern="[a-z-]+")


@dataclass(frozen=True, slots=True)
class BadType:
    count: int = Arg(default="three", description="How many")


@dataclass(frozen=True, slots=True)
class NotOptional:
    name: str = Arg(default=None, description="Name")


def test_a_default_that_breaks_the_fields_pattern_is_refused() -> None:
    app = App("t", version="1.0.0")
    with pytest.raises(RegistrationError, match="default 'Not A Slug'"):

        @app.command("go", description="Go", danger_level="safe", exit_codes=())
        def go(args: BadPattern, ctx: Ctx) -> dict[str, str]:
            return {}


def test_a_default_of_another_type_is_refused() -> None:
    app = App("t", version="1.0.0")
    with pytest.raises(RegistrationError, match="default 'three' does not match"):

        @app.command("go", description="Go", danger_level="safe", exit_codes=())
        def go(args: BadType, ctx: Ctx) -> dict[str, str]:
            return {}


def test_a_none_default_needs_an_optional_type() -> None:
    app = App("t", version="1.0.0")
    with pytest.raises(RegistrationError, match="default None does not match"):

        @app.command("go", description="Go", danger_level="safe", exit_codes=())
        def go(args: NotOptional, ctx: Ctx) -> dict[str, str]:
            return {}


def test_a_mutable_default_is_refused() -> None:
    with pytest.raises(RegistrationError, match="mutable defaults"):
        Arg(default=[], description="Words")
