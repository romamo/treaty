"""A small argparse CLI for treaty scaffold-from, built by a function as most are"""

import argparse
from pathlib import Path


def resource_id(text: str) -> str:
    return text


def cmd_show(args: argparse.Namespace) -> None:
    pass


def cmd_restart(args: argparse.Namespace) -> None:
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cloudfall", description="Run a small cloud")
    parser.add_argument("--project", type=Path, default=Path("."), help="Project directory")
    parser.add_argument("-q", "--quiet", action="store_true", help="Say less")
    sub = parser.add_subparsers(dest="command", required=True)

    inventory = sub.add_parser("inventory", help="What runs where")
    inventory_sub = inventory.add_subparsers(dest="inventory_command", required=True)
    show = inventory_sub.add_parser("show", aliases=["ls"], help="Show the inventory")
    show.add_argument("host", nargs="?", help="Only this host")
    show.add_argument("--tag", action="append", default=[], help="Tags to match")
    show.add_argument("--depth", type=int, default=1, help="How deep to look")
    show.add_argument("--fields", help="Columns to show")
    show.set_defaults(func=cmd_show)

    restart = sub.add_parser("restart", help="Restart services")
    restart.add_argument("services", nargs="+", type=resource_id, help="Services to restart")
    restart.add_argument("--mode", choices=["soft", "hard"], default="soft", help="How")
    restart.add_argument("--no-wait", dest="wait", action="store_false", help="Return at once")
    restart.add_argument("--retries", type=float, required=True, help="Attempts")
    restart.add_argument("--yes", action="store_true", help="Do not ask")
    restart.set_defaults(func=cmd_restart)
    return parser


parser = build_parser()


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)
