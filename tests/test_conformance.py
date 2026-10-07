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
from treaty import App, Ctx, Flag, NoArgs
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
    assert "trade" not in probes and "trade stream" not in probes
    assert probes["watch stream"].argv == ("watch",)


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
