import importlib.util
import json
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import SPEC_DIR, needs_sh_launcher, spec_validator

from treaty import App, Ctx, NoArgs
from treaty._profile import STREAM_SECONDS, TIMEOUT_SECONDS, build_profile, probes_for

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
