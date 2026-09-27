"""An entry module as the scaffold writes it, for REQ-F-060: stdout is intercepted before
a library that prints on import is loaded; runnable as a tool."""

from treaty import intercept_stdout

intercept_stdout()

import json  # noqa: E402 - imported after the interception, as a library would be
import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

print("initialized")  # a library announcing itself on import

from treaty import App, Ctx, NoArgs  # noqa: E402

app = App("noisyctl", version="1.0.0")


@app.command("quiet", description="Print nothing", danger_level="safe", exit_codes=())
def quiet(args: NoArgs, ctx: Ctx) -> dict[str, str]:
    return {"status": "ok"}


@app.command("native", description="Write to descriptor 1", danger_level="safe", exit_codes=())
def native(args: NoArgs, ctx: Ctx) -> dict[str, str]:
    os.write(1, b"from C code\n")
    subprocess.run([sys.executable, "-c", "print('from a child')"], check=True)
    return {"status": "ok"}


@app.command("json", description="Print JSON by mistake", danger_level="safe", exit_codes=())
def json_(args: NoArgs, ctx: Ctx) -> dict[str, str]:
    os.write(1, json.dumps({"status": "ok"}).encode() + b"\n")
    return {"status": "ok"}


if __name__ == "__main__":
    app.main()
