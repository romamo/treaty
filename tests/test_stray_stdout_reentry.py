"""Two ways printed text escaped the stray-stdout redaction (#263): under ``--debug`` a log
handler writing to ``sys.stdout`` traced its own trace record until ``RecursionError``,
and a handler thread abandoned at its timeout wrote to descriptor 1 as the process
exited, after ``App.main`` had given stdout back."""

import io
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass

from treaty import App, Ctx, Flag

SECRET = "hunter2-s3cret"


@dataclass(frozen=True, slots=True)
class Login:
    api_token: str = Flag(default="", description="API token", secret=True)


def logging_app() -> App:
    app = App("loud", version="1.0.0")

    @app.command("show", description="Show", danger_level="safe", exit_codes=())
    def show(args: Login, ctx: Ctx) -> dict[str, bool]:
        # sys.stdout is the run's stand-in here: the handler writes every record to it
        handler = logging.StreamHandler(sys.stdout)
        logging.getLogger().addHandler(handler)
        try:
            logging.getLogger("loud.lib").warning("token %s", args.api_token)
            print("printed", args.api_token)
        finally:
            logging.getLogger().removeHandler(handler)
        return {"ok": True}

    return app


def test_a_stdout_log_handler_under_debug_writes_each_line_once() -> None:
    out, err = io.StringIO(), io.StringIO()
    code = logging_app().run(
        ["show", "--api-token-from-env", "TOKEN", "--debug"],
        stdin=io.StringIO(""),
        stdout=out,
        stderr=err,
        env={"TOKEN": SECRET},
        isatty=False,
    )
    assert code == 0, err.getvalue()[-2000:]
    assert json.loads(out.getvalue())["data"] == {"ok": True}
    assert SECRET not in err.getvalue()
    lines = err.getvalue().splitlines()
    traced = [json.loads(line) for line in lines if line.startswith("{")]
    writes = [line["fields"]["text"] for line in traced if line["message"] == "stdout write"]
    # What the handler wrote and what was printed, each traced once
    assert writes == ["token [REDACTED]", "printed [REDACTED]"]
    # The handler's copies of those two trace records went out as written, not traced
    assert lines.count("stdout write") == 2


APP = """
import atexit, logging, os, sys, threading
from dataclasses import dataclass
from treaty import App, Ctx, Flag

app = App("late", version="1.0.0")
EXITING, WROTE = threading.Event(), threading.Event()


@dataclass(frozen=True, slots=True)
class Login:
    api_token: str = Flag(description="API token", secret=True)


@app.command("slow", description="Outlive the timeout", danger_level="safe", exit_codes=(),
             timeout=1)
def slow(args: Login, ctx: Ctx) -> dict[str, bool]:
    # Abandoned at its timeout, it writes once the process is exiting
    EXITING.wait(30)
    os.write(1, ("fd1 " + args.api_token + "\\n").encode())
    print("print", args.api_token, flush=True)
    sys.stderr.write("stderr " + args.api_token + "\\n")
    logging.getLogger("late.lib").warning("log %s", args.api_token)
    WROTE.set()
    return {"ok": True}


def exiting():
    EXITING.set()
    WROTE.wait(30)
    # Until the thread is gone: as it ends, its redaction and the pipe are released
    for thread in threading.enumerate():
        if thread.name == "treaty-handler":
            thread.join(30)


# Registered before main() installs its interceptor, so it runs after that one's exit hook
atexit.register(exiting)
app.main()
"""


def test_a_held_thread_writing_as_the_process_exits_is_redacted() -> None:
    env = {**os.environ, "LATE_TOKEN": SECRET, "PYTHONUTF8": "1"}
    for name in ("FORCE_COLOR", "TREATY_FORMAT"):
        env.pop(name, None)
    proc = subprocess.run(
        [sys.executable, "-", "slow", "--api-token-from-env", "LATE_TOKEN"],
        input=APP.encode(),
        capture_output=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert SECRET.encode() not in proc.stdout + proc.stderr
    [envelope] = proc.stdout.decode().splitlines()
    assert json.loads(envelope)["error"]["code"] == "TIMEOUT"
    err = proc.stderr.decode().replace("\r\n", "\n")
    for line in ("fd1 [REDACTED]", "print [REDACTED]", "stderr [REDACTED]", "log [REDACTED]"):
        assert line in err.splitlines(), err
