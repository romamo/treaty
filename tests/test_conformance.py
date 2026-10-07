import importlib.util
import json
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import SPEC_DIR, needs_posix_signals, needs_sh_launcher, spec_validator

from examples.tutorial import todo_exit_codes
from treaty import App, Arg, Ctx, Flag, NoArgs
from treaty._profile import (
    STREAM_SECONDS,
    TIMEOUT_SECONDS,
    argument_order_for,
    build_profile,
    probes_for,
)

KIT = SPEC_DIR / "conformance" / "run.py"
PROFILE = Path(__file__).resolve().parents[1] / "conformance" / "deployctl.json"
VENV_PYTHON = Path(sys.executable)
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


STREAM_CHECKS = frozenset({"stream_contract", "stream_sigint"})
TICKS = Path(__file__).resolve().parent / "fixture_ticks_app.py"


def run_kit(profile: Path, *only: str) -> dict:
    if not KIT.is_file():
        pytest.skip(f"conformance kit not found at {KIT}; set TREATY_SPEC_DIR")
    argv = ["uv", "run", "--project", str(SPEC_DIR), str(KIT), str(profile)]
    if only:
        argv += ["--only", *only]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    report: dict = json.loads(result.stdout)["data"]
    return report


def only_streams_skipped(report: dict) -> None:
    """An app with no stream: level 3 is incomplete only because the kit's stream checks
    have no probe to run"""
    assert report["levels"] == {"level_1": "pass", "level_2": "pass", "level_3": "incomplete"}
    skipped = {c["id"] for c in report["checks"] if c["status"] == "skip"}
    assert skipped == STREAM_CHECKS
    assert all(c["status"] == "pass" for c in report["checks"] if c["id"] not in skipped)


@needs_sh_launcher
def test_example_cli_passes_conformance_kit() -> None:
    if not VENV_PYTHON.is_file():
        pytest.skip("launcher needs the project venv")
    only_streams_skipped(run_kit(PROFILE))


@needs_posix_signals
def test_the_issues_stream_profile_passes_the_kits_stream_checks(tmp_path: Path) -> None:
    """#389: item lines, one terminal line, and SIGINT's CANCELLED line with data.partial"""
    profile = {
        "schema_version": "1.0",
        "tool": "ticks 0.1.0",
        "command": [sys.executable, str(TICKS)],
        "timeout_seconds": 10,
        "manifest": ["manifest"],
        "probes": [
            {"name": "count", "argv": ["count"], "kind": "stream", "deadline_seconds": 15},
            {
                "name": "forever sigint",
                "argv": ["forever"],
                "kind": "stream",
                "deadline_seconds": 15,
                "signal": "INT",
                "after_lines": 2,
            },
        ],
    }
    path = tmp_path / "ticks.json"
    path.write_text(json.dumps(profile))
    report = run_kit(path, *sorted(STREAM_CHECKS))
    assert {c["id"]: c["status"] for c in report["checks"]} == dict.fromkeys(STREAM_CHECKS, "pass")
    assert all(c["runs_checked"] > 0 for c in report["checks"])


@needs_posix_signals
def test_a_generated_profile_runs_the_kits_stream_checks(tmp_path: Path) -> None:
    """``treaty conformance`` probes a safe stream as a stream, and the endless one with
    SIGINT, so the checks no longer skip and none waits for a stream that never ends"""
    from fixture_ticks_app import app

    probes = probes_for(app)
    path = tmp_path / "ticks.json"
    path.write_text(json.dumps(build_profile(app, [sys.executable, str(TICKS)], probes)))
    report = run_kit(path)
    assert report["levels"] == {"level_1": "pass", "level_2": "pass", "level_3": "pass"}, report
    assert report["summary"]["failed"] == 0 and report["summary"]["skipped"] == 0


def test_an_endless_stream_gets_the_sigint_probe_and_no_stream_probe() -> None:
    from fixture_ticks_app import app

    probes = {p.name: p.to_json() for p in probes_for(app)}
    assert probes["count stream"] == {
        "name": "count stream",
        "argv": ["count"],
        "kind": "stream",
        "deadline_seconds": TIMEOUT_SECONDS,
    }
    # forever is endless=True: only a signal ends it, so it takes the SIGINT probe, though
    # count comes first, and no probe waits for its end
    assert probes["forever SIGINT"] == {
        "name": "forever SIGINT",
        "argv": ["forever"],
        "kind": "stream",
        "deadline_seconds": TIMEOUT_SECONDS,
        "signal": "INT",
        "after_lines": 1,
    }
    assert "forever stream" not in probes and "count SIGINT" not in probes
    # The read probes stay: --no-stream ends in one envelope
    assert probes["count"]["kind"] == probes["forever"]["kind"] == "read"
    assert "--no-stream" in probes["forever"]["argv"]


def test_without_an_endless_stream_the_first_safe_stream_gets_the_sigint_probe() -> None:
    app = App("pair", version="1.0.0")

    for path in ("first", "second"):

        @app.command(path, description=path, streaming=True, danger_level="safe", exit_codes=())
        def stream(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, int]]:
            yield {"n": 1}

    probes = {p.name: p.to_json() for p in probes_for(app)}
    assert probes["first SIGINT"]["signal"] == "INT" and "second SIGINT" not in probes
    assert probes["first stream"]["kind"] == probes["second stream"]["kind"] == "stream"


def stream_app() -> App:
    app = App("streamctl", version="1.0.0")

    @app.command(
        "watch",
        description="Watch prices",
        streaming=True,
        has_network_io=True,
        danger_level="safe",
        exit_codes=(),
        examples=[("Watch for a minute", "streamctl watch --timeout 60 --no-stream")],
    )
    def watch(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, int]]:
        yield {"price": 1}

    @app.command(
        "trade",
        description="Trade in a loop",
        streaming=True,
        has_network_io=True,
        danger_level="mutating",
        exit_codes=(),
    )
    def trade(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, int]]:
        yield {"filled": 1}

    @app.command(
        "tail",
        description="Tail the local price log",
        streaming=True,
        danger_level="safe",
        exit_codes=(),
        examples=[("Tail for a minute", "streamctl tail --timeout 60 --no-stream")],
    )
    def tail(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, int]]:
        yield {"price": 1}

    return app


def test_streaming_commands_keep_the_probes_that_end_in_one_envelope() -> None:
    probes = {p.name: p for p in probes_for(stream_app())}
    # Invalid input exits 2 before the stream starts, whatever the danger level (#349)
    assert probes["watch --proxy socks5"].kind == probes["trade --proxy socks5"].kind == "invalid"
    # A read is bounded: --no-stream makes --timeout a deadline for the whole stream;
    # the example's own --timeout and --no-stream are replaced, not repeated
    assert probes["tail"].kind == "read"
    assert probes["tail"].argv == ("tail", "--no-stream", "--timeout", str(STREAM_SECONDS))
    assert 0 < STREAM_SECONDS < TIMEOUT_SECONDS
    # Nothing runs a mutating stream for real, nor a network one, whose reads go out (#390)
    assert "trade" not in probes and "watch" not in probes
    assert "trade stream" not in probes
    assert probes["tail stream"].argv == ("tail",)


def test_a_network_stream_gets_no_stream_or_sigint_probe() -> None:
    # Each kit run of either would make its real, perhaps paid, requests, as a read probe
    # would (#390): the SIGINT probe goes to the first stream the kit may run (#389)
    probes = {p.name: p for p in probes_for(stream_app())}
    assert "watch stream" not in probes and "watch SIGINT" not in probes
    assert probes["tail SIGINT"].sigint_after == 1


def test_a_network_endless_stream_does_not_take_the_sigint_probe() -> None:
    app = App("netctl", version="1.0.0")

    @app.command("follow", description="Follow a remote feed", streaming=True, endless=True,
                 has_network_io=True, danger_level="safe", exit_codes=())  # fmt: skip
    def follow(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, int]]:
        yield {"n": 1}

    @app.command("tail", description="Tail a local log", streaming=True, danger_level="safe",
                 exit_codes=())  # fmt: skip
    def tail(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, int]]:
        yield {"n": 1}

    names = [p.name for p in probes_for(app)]
    assert not any(n.startswith("follow") and n != "follow --proxy socks5" for n in names)
    assert "tail stream" in names and "tail SIGINT" in names


def test_a_streaming_read_probe_profile_validates_against_the_spec() -> None:
    app = stream_app()
    profile = build_profile(app, ["streamctl"], probes_for(app))
    assert list(spec_validator("conformance-profile").iter_errors(profile)) == []


def slowctl() -> App:
    spec = importlib.util.spec_from_file_location("slowctl_probe", EXAMPLES / "slowctl.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app: App = module.app
    return app


def test_an_endless_stream_read_probe_ends_with_one_envelope_within_the_kit_limit() -> None:
    [probe] = [p for p in probes_for(slowctl()) if p.name == "serve"]
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(EXAMPLES / "slowctl.py"), *probe.argv, "--format", "json"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )
    assert time.monotonic() - started < TIMEOUT_SECONDS
    [line] = result.stdout.splitlines()
    envelope = json.loads(line)
    assert envelope["error"]["code"] == "TIMEOUT" and envelope["meta"]["partial"] is True
    assert envelope["data"][0]["event"] == "listening"
    assert result.returncode == envelope["meta"]["exit_code"]
    assert result.returncode in range(14)  # within the kit's exit code table


@dataclass(frozen=True, slots=True)
class Wipe:
    dry_run: bool = Flag(default=False, description="Preview")


@dataclass(frozen=True, slots=True)
class Prune:
    keep: int = Flag(default=1, description="Items to keep")
    dry_run: bool = Flag(default=False, description="Preview")


def destructive_app(*, with_option: bool) -> App:
    app = App("wipectl", version="1.0.0")

    @app.command(
        "wipe",
        description="Wipe everything",
        danger_level="destructive",
        exit_codes=(),
        examples=[("Wipe", "wipectl wipe --dry-run")],
    )
    def wipe(args: Wipe, ctx: Ctx) -> dict[str, bool]:
        return {"dry_run": args.dry_run}

    if with_option:

        @app.command(
            "prune",
            description="Prune old items",
            danger_level="destructive",
            exit_codes=(),
            examples=[("Keep three", "wipectl prune --keep 3 --confirm-destructive")],
        )
        def prune(args: Prune, ctx: Ctx) -> dict[str, bool]:
            return {"dry_run": args.dry_run}

    return app


def profile_argvs(app: App) -> list[list[str]]:
    """The argument_order run and every probe's argv"""
    profile = build_profile(app, [app.name], probes_for(app))
    order, probes = profile["argument_order"], profile["probes"]
    assert isinstance(order, dict) and isinstance(probes, list)
    return [[*order["command_path"], *order["local_args"]], *(p["argv"] for p in probes)]


@pytest.mark.parametrize(
    "app",
    [todo_exit_codes.app, destructive_app(with_option=False), destructive_app(with_option=True)],
    ids=["tutorial", "dry-run only", "with an option"],
)
def test_no_probe_runs_a_destructive_command_confirmed(app: App) -> None:
    """A handler that ignored its dry-run flag would apply for real on the kit's machine (#373)"""
    for argv in profile_argvs(app):
        assert "--confirm-destructive" not in argv, argv


def test_argument_order_skips_a_destructive_example_with_only_its_dry_run_flag() -> None:
    order = argument_order_for(destructive_app(with_option=False))
    assert order is not None and order["command_path"] == ["manifest"]
    order = argument_order_for(destructive_app(with_option=True))
    assert order is not None and order["command_path"] == ["prune"]
    assert order["local_args"] == ["--keep", "3", "--dry-run"]


@dataclass(frozen=True, slots=True)
class PdfArgs:
    pdf: Path = Arg(description="PDF to read")
    pages: int = Flag(default=1, description="Pages to read")


def output_file_app(example: str) -> App:
    app = App("pdfctl", version="1.0.0")

    @app.command(
        "profile",
        description="Profile a PDF",
        examples=[("Profile a PDF and save it", example)],
        danger_level="safe",
        exit_codes=(),
        output_file=True,
    )
    def profile(args: PdfArgs, ctx: Ctx) -> dict[str, int]:
        return {"pages": args.pages}

    return app


@pytest.mark.parametrize(
    "example",
    [
        "pdfctl profile lease.pdf --output bundle.json --pages 2",
        "pdfctl profile lease.pdf --output=bundle.json --pages 2",
        "pdfctl profile --output bundle.json lease.pdf --pages 2",
    ],
    ids=["spaced", "inline", "before the positional"],
)
def test_no_probe_writes_the_output_file_of_an_example(example: str) -> None:
    """Every kit run of a probe with --output would write the file (#391)"""
    app = output_file_app(example)
    probes = {p.name: p for p in probes_for(app)}
    assert probes["profile"].argv == ("profile", "lease.pdf", "--pages", "2")
    assert probes["unknown flag"].argv == ("profile", "lease.pdf", "--pages", "2", "--no-such-flag")
    order = argument_order_for(app)
    assert order is not None
    assert order["command_path"] == ["profile", "lease.pdf"]
    assert order["local_args"] == ["--pages", "2"]


def test_an_example_whose_only_option_is_output_leaves_argument_order_to_the_manifest() -> None:
    app = output_file_app("pdfctl profile lease.pdf --output bundle.json")
    order = argument_order_for(app)
    assert order is not None and order["command_path"] == ["manifest"]
    for argv in profile_argvs(app):
        assert not any(tok.startswith("--output") for tok in argv), argv


def network_app(calls: list[str], *, with_local: bool) -> App:
    app = App("pdfctl", version="1.0.0")

    @app.command(
        "summarize",
        description="Summarize a PDF with a paid remote API",
        examples=[("Summarize two pages", "pdfctl summarize lease.pdf --pages 2")],
        danger_level="safe",
        exit_codes=(),
        has_network_io=True,
    )
    def summarize(args: PdfArgs, ctx: Ctx) -> dict[str, int]:
        calls.append("summarize")
        return {"pages": args.pages}

    if with_local:

        @app.command(
            "tally",
            description="Count a PDF's pages",
            examples=[("Tally two pages", "pdfctl tally lease.pdf --pages 2")],
            danger_level="safe",
            exit_codes=(),
        )
        def tally(args: PdfArgs, ctx: Ctx) -> dict[str, int]:
            return {"pages": args.pages}

    return app


@pytest.mark.parametrize("with_local", [True, False], ids=["with a local command", "alone"])
def test_no_probe_runs_a_network_command(with_local: bool) -> None:
    """Each kit run of a read probe made the command's real, perhaps paid, requests (#390)"""
    calls: list[str] = []
    app = network_app(calls, with_local=with_local)
    probes = {p.name: p for p in probes_for(app)}
    assert "summarize" not in probes
    assert probes["summarize --proxy socks5"].kind == "invalid"
    # The unknown flag still goes on the first command's example, never the bare version,
    # and exits 2 before the handler runs
    unknown = probes["unknown flag"].argv
    assert unknown == ("summarize", "lease.pdf", "--pages", "2", "--no-such-flag")
    for probe in probes.values():
        if probe.kind == "invalid" and probe.argv[0] == "summarize":
            assert app.run(list(probe.argv), env={}, isatty=False) == 2, probe
    assert calls == []
    # argument_order runs the command: the next example, or the built-in manifest
    order = argument_order_for(app)
    assert order is not None
    assert order["command_path"] == (["tally", "lease.pdf"] if with_local else ["manifest"])


@dataclass(frozen=True, slots=True)
class FetchArgs:
    names: list[str] = Arg(description="Remote objects to fetch")


def test_an_invalid_probe_of_an_example_with_a_separator_exits_before_the_network() -> None:
    """After ``--`` an added flag is a positional: the probe ran the command (#390)"""
    calls: list[list[str]] = []
    app = App("objctl", version="1.0.0")

    @app.command(
        "fetch",
        description="Fetch remote objects",
        examples=[("Fetch an object named with a dash", "objctl fetch -- -weird")],
        danger_level="safe",
        exit_codes=(),
        has_network_io=True,
    )
    def fetch(args: FetchArgs, ctx: Ctx) -> dict[str, list[str]]:
        calls.append(args.names)
        return {"names": args.names}

    invalid = [p for p in probes_for(app) if p.kind == "invalid" and p.argv[0] == "fetch"]
    assert {p.name for p in invalid} == {"fetch --proxy socks5", "unknown flag"}
    for probe in invalid:
        assert probe.argv[-2:] == ("--", "-weird"), probe
        assert app.run(list(probe.argv), env={}, isatty=False) == 2, probe
    assert calls == []


@dataclass(frozen=True, slots=True)
class PurgeArgs:
    bucket: str = Flag(default="logs", description="Remote bucket to purge")
    dry_run: bool = Flag(default=False, description="Preview only")


def test_argument_order_does_not_run_a_safe_default_network_command() -> None:
    """Unconfirmed, a safe_default command previews, a read with its real requests: it has
    no read probe, so argument_order must not run it either (#390)"""
    app = App("s3ctl", version="1.0.0")

    @app.command(
        "purge",
        description="Purge a remote bucket",
        examples=[("Preview a purge", "s3ctl purge --bucket logs")],
        danger_level="destructive",
        safe_default=True,
        exit_codes=(),
        has_network_io=True,
    )
    def purge(args: PurgeArgs, ctx: Ctx) -> dict[str, str]:
        return {"bucket": args.bucket}

    assert "purge" not in {p.name for p in probes_for(app)}
    order = argument_order_for(app)
    assert order is not None and order["command_path"] == ["manifest"]
