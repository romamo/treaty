"""A stand-in for an agentyper app, which treaty does not depend on: calling it runs the
CLI, and its private ``_build_parser`` method builds the argparse parser it runs (#334)"""

import argparse
import sys

from treaty import App


class Agentyper:
    def __init__(self, name: str) -> None:
        self.name = name

    def __call__(self) -> None:
        self._build_parser().parse_args()
        sys.exit(0)

    def _build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog=self.name, description="Keep the books")
        sub = parser.add_subparsers(dest="command", required=True)
        balance = sub.add_parser("balance", help="Show the balance of an account")
        balance.add_argument("account", help="Account name")
        balance.add_argument("--currency", default="EUR", help="Report currency")
        return parser


class Holder:
    """Objects one attribute further down: the stand-in, and a treaty App for the commands
    that load one, which read the same dotted target"""

    def __init__(self) -> None:
        self.app = app
        self.treaty_app = App("books", version="1.0.0", description="Keep the books")


app = Agentyper("bean")
holder = Holder()
nothing = None
