"""democli built with argparse the way a competent developer would write it.

Text output, argparse's own usage errors, exit 1 on every runtime failure, and a
confirmation prompt before destructive work.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _fixture as fx  # noqa: E402


def note_cell(note: str, width: int = 24) -> str:
    """Text tables need a fixed width: long notes are shortened, empty ones shown as a dash."""
    if not note:
        return "-"
    return note if len(note) <= width else note[: width - 1] + "\u2026"


def cmd_list(args: argparse.Namespace) -> int:
    try:
        items, pages = fx.page(args.limit, args.page)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{'ID':<14} {'VERSION':<8} {'ENV':<11} {'STATUS':<8} {'CREATED':<21} NOTE")
    for item in items:
        print(
            f"{item['id']:<14} {item['version']:<8} {item['env']:<11} {item['status']:<8} "
            f"{item['created_at']:<21} {note_cell(item['note'])}"
        )
    print(f"\nPage {args.page} of {pages} ({len(fx.all_deployments())} deployments total)")
    if args.page < pages:
        print(f"Use --page {args.page + 1} to see the next page")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    try:
        targets = fx.matching_stale(args.filter)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"Would delete {len(targets)} deployments:")
        for t in targets:
            print(f"  {t['id']}  ({t['env']}, {t['age_days']} days old)")
        return 0
    if not args.yes:
        try:
            answer = input(f"Delete {len(targets)} deployments? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted.", file=sys.stderr)
            return 1
    for t in targets:
        print(f"Deleted {t['id']}")
    print(f"{len(targets)} deployments deleted")
    return 0


def cmd_deploy(args: argparse.Namespace) -> int:
    if args.env == "production":
        try:
            record = fx.deploy_production(args.version, args.idempotency_key)
        except fx.ResponseLost:
            print(
                "error: connection reset while reading the response; the deployment may or may "
                "not have been created. Check `deployments list` before retrying.",
                file=sys.stderr,
            )
            return 1
        print(f"Deployed {args.version} to production ({record['id']})")
        return 0
    if not fx.acquire_deploy_lock():
        print(
            f"error: another deployment is in progress (lock held by {fx.LOCK_HOLDER}). "
            f"Wait {fx.LOCK_RETRY_MS // 1000}s and try again.",
            file=sys.stderr,
        )
        return 1
    print(f"Deployed {args.version} to {args.env} (deploy-new-001)")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    print(f"warning: health cache is stale ({fx.CACHE_AGE_HOURS}h old)", file=sys.stderr)
    print("api-server ... ok")
    print("database ... ok")
    print(f"registry ... FAILED (credential expired at {fx.REGISTRY_EXPIRED_AT})", file=sys.stderr)
    checked = 3
    if args.deep:
        print(f"cdn ... {fx.probe_cdn(None)}")
        checked = 4
    print(f"{checked} services checked, 1 failed")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="democli", description="Demo deployment tool")
    parser.add_argument("--version", action="version", version="democli 2.1.0")
    sub = parser.add_subparsers(dest="command", required=True)

    deployments = sub.add_parser("deployments", help="Manage deployments")
    dsub = deployments.add_subparsers(dest="subcommand", required=True)
    lst = dsub.add_parser("list", help="List deployments")
    lst.add_argument("--limit", type=int, default=5, help="Items per page (default 5)")
    lst.add_argument("--page", type=int, default=1, help="Page number (default 1)")
    lst.set_defaults(func=cmd_list)
    delete = dsub.add_parser("delete", help="Delete deployments matching a filter")
    delete.add_argument("--filter", required=True, help="Selection expression such as env=staging")
    delete.add_argument("--dry-run", action="store_true", help="Preview without deleting")
    delete.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    delete.set_defaults(func=cmd_delete)

    deploy = sub.add_parser("deploy", help="Deploy an application version")
    deploy.add_argument("--version", required=True, help="Version to deploy")
    deploy.add_argument("--env", required=True, choices=fx.ENVS, help="Target environment")
    deploy.add_argument("--idempotency-key", help="Duplicate calls with the same key return the original result")
    deploy.set_defaults(func=cmd_deploy)

    health = sub.add_parser("health", help="Service health commands")
    hsub = health.add_subparsers(dest="subcommand", required=True)
    check = hsub.add_parser("check", help="Run health checks against all services")
    check.add_argument("--deep", action="store_true", help="Also probe the CDN edge (slow)")
    check.set_defaults(func=cmd_health)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
