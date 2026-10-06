import importlib.util
import json
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import SPEC_DIR, needs_sh_launcher, spec_validator

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


@needs_sh_launcher
def test_example_cli_passes_conformance_kit() -> None:
    if not KIT.is_file():
        pytest.skip(f"conformance kit not found at {KIT}; set TREATY_SPEC_DIR")
    if not VENV_PYTHON.is_file():
        pytest.skip("launcher needs the project venv")
    result = subprocess.run(
        ["uv", "run", "--project", str(SPEC_DIR), str(KIT), str(PROFILE)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)["data"]
    assert report["levels"] == {"level_1": "pass", "level_2": "pass", "level_3": "pass"}
    assert report["summary"]["failed"] == 0 and report["summary"]["skipped"] == 0


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

    return app


def test_streaming_commands_keep_the_probes_that_end_in_one_envelope() -> None:
    probes = {p.name: p for p in probes_for(stream_app())}
    # Invalid input exits 2 before the stream starts, whatever the danger level (#349)
    assert probes["watch --proxy socks5"].kind == probes["trade --proxy socks5"].kind == "invalid"
    # A read is bounded: --no-stream makes --timeout a deadline for the whole stream;
    # the example's own --timeout and --no-stream are replaced, not repeated
    assert probes["watch"].kind == "read"
    assert probes["watch"].argv == ("watch", "--no-stream", "--timeout", str(STREAM_SECONDS))
    assert 0 < STREAM_SECONDS < TIMEOUT_SECONDS
    # Nothing runs a mutating stream for real
    assert "trade" not in probes


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
