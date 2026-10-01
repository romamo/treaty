"""A passthrough command (#35): ``ingest`` hands its argv to argparse, as ibkr-converter's
hands it to beangulp, and prints its result to stdout, as the tool it wraps does"""

import argparse
import os
import subprocess
import sys
import time

from treaty import App, Ctx, NoArgs

app = App("ledger", version="1.0.0", description="Keep a ledger")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledger ingest")
    sub = parser.add_subparsers(dest="action", required=True)
    extract = sub.add_parser("extract", help="Print the entries of a statement")
    extract.add_argument("statement")
    extract.add_argument("--output", "-o", default=None)
    sub.add_parser("native", help="Write to descriptor 1, as a child or C code would")
    fail = sub.add_parser("fail", help="Exit with a code")
    fail.add_argument("code", type=int)
    sub.add_parser("echo", help="Print the arguments as received")
    sub.add_parser("sleep", help="Say ready, then wait for a signal")
    return parser


@app.command(
    "ingest",
    description="Import statements with the ingest tool's own arguments",
    danger_level="mutating",
    exit_codes=(),
    passthrough=True,
    help_command=("extract", "--help"),
)
def ingest(args: NoArgs, ctx: Ctx) -> int:
    if ctx.argv_rest[:1] == ("echo",):
        print(repr(ctx.argv_rest))
        return 0
    options = _parser().parse_args(ctx.argv_rest)
    if options.action == "extract":
        print(f"entries of {options.statement}")
        if options.output is not None:
            print(f"written to {options.output}")
        return 0
    if options.action == "native":
        os.write(1, b"from C code\n")
        subprocess.run([sys.executable, "-c", "print('from a child')"], check=True)
        return 0
    if options.action == "sleep":
        print("ready", flush=True)
        time.sleep(30)
        return 0
    return int(options.code)


if __name__ == "__main__":
    app.main()
