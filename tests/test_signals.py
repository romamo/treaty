import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import needs_posix_signals, spec_validator

pytestmark = needs_posix_signals

SLOWCTL = Path(__file__).resolve().parents[1] / "examples" / "slowctl.py"


def start(*argv: str) -> subprocess.Popen[str]:
    """slowctl, once its handler runs: a signal before that meets Python's default action"""
    proc = subprocess.Popen(
        [sys.executable, str(SLOWCTL), *argv, "--verbose"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stderr is not None
    line = proc.stderr.readline()  # the handler's "fetching" line; blocks until it is written
    assert "fetching" in line, line + proc.stderr.read()
    return proc


@pytest.mark.parametrize(
    ("sig", "code", "name"),
    [(signal.SIGTERM, 143, "SIGTERM"), (signal.SIGINT, 130, "SIGINT")],
)
def test_signal_produces_cancelled_envelope(sig: signal.Signals, code: int, name: str) -> None:
    proc = start("fetch", "--seconds", "10", "--timeout", "0")
    proc.send_signal(sig)
    out, err = proc.communicate(timeout=5)
    assert proc.returncode == code, err
    envelope = json.loads(out)
    spec_validator("response-envelope").validate(envelope)
    assert envelope["error"]["code"] == "CANCELLED"
    assert envelope["error"]["context"]["signal"] == name
    assert envelope["error"]["retryable"] is False
    assert envelope["meta"]["exit_code"] == code and envelope["meta"]["partial"] is True
    assert "cleanup: releasing resources" in err


def test_signal_during_timeout_wait_is_still_cancelled() -> None:
    proc = start("fetch", "--seconds", "10", "--timeout", "30")
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=5)
    assert proc.returncode == 143 and json.loads(out)["error"]["code"] == "CANCELLED"


def test_second_signal_during_cleanup_exits_without_double_write() -> None:
    proc = start("fetch", "--seconds", "10", "--timeout", "0", "--cleanup-seconds", "5")
    proc.send_signal(signal.SIGTERM)
    time.sleep(0.5)  # now inside the cleanup hook
    proc.send_signal(signal.SIGTERM)
    started = time.monotonic()
    out, err = proc.communicate(timeout=5)
    assert time.monotonic() - started < 3, "second SIGTERM did not exit immediately"
    assert proc.returncode == 143
    assert out.count("\n") <= 1 and out.count('"ok"') <= 1, out
