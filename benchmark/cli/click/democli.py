# /// script
# requires-python = ">=3.12"
# dependencies = ["click>=8.1"]
# ///
"""democli built with click the way a competent developer would write it.

Text output, click's own usage errors, exit 1 on runtime failures, and
click.confirm before destructive work.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _fixture as fx  # noqa: E402


def note_cell(note: str, width: int = 24) -> str:
    """Text tables need a fixed width: long notes are shortened, empty ones shown as a dash."""
    if not note:
        return "-"
    return note if len(note) <= width else note[: width - 1] + "\u2026"


@click.group()
@click.version_option("2.1.0", prog_name="democli")
def cli() -> None:
    """Demo deployment tool."""


@cli.group()
def deployments() -> None:
    """Manage deployments."""


@deployments.command("list")
@click.option("--limit", default=5, show_default=True, help="Items per page")
@click.option("--page", default=1, show_default=True, help="Page number")
def list_(limit: int, page: int) -> None:
    """List deployments."""
    try:
        items, pages = fx.page(limit, page)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{'ID':<14} {'VERSION':<8} {'ENV':<11} {'STATUS':<8} {'CREATED':<21} NOTE")
    for item in items:
        click.echo(
            f"{item['id']:<14} {item['version']:<8} {item['env']:<11} {item['status']:<8} "
            f"{item['created_at']:<21} {note_cell(item['note'])}"
        )
    click.echo(f"\nPage {page} of {pages} ({len(fx.all_deployments())} deployments total)")
    if page < pages:
        click.echo(f"Use --page {page + 1} to see the next page")


@deployments.command()
@click.option("--filter", "filter_expr", required=True, help="Selection expression such as env=staging")
@click.option("--dry-run", is_flag=True, help="Preview without deleting")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt")
def delete(filter_expr: str, dry_run: bool, yes: bool) -> None:
    """Delete deployments matching a filter."""
    try:
        targets = fx.matching_stale(filter_expr)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if dry_run:
        click.echo(f"Would delete {len(targets)} deployments:")
        for t in targets:
            click.echo(f"  {t['id']}  ({t['env']}, {t['age_days']} days old)")
        return
    if not yes:
        click.confirm(f"Delete {len(targets)} deployments?", abort=True)
    for t in targets:
        click.echo(f"Deleted {t['id']}")
    click.echo(f"{len(targets)} deployments deleted")


@cli.command()
@click.option("--version", required=True, help="Version to deploy")
@click.option("--env", required=True, type=click.Choice(fx.ENVS), help="Target environment")
@click.option("--idempotency-key", help="Duplicate calls with the same key return the original result")
def deploy(version: str, env: str, idempotency_key: str | None) -> None:
    """Deploy an application version."""
    if env == "production":
        try:
            record = fx.deploy_production(version, idempotency_key)
        except fx.ResponseLost as exc:
            raise click.ClickException(
                "connection reset while reading the response; the deployment may or may not "
                "have been created. Check `deployments list` before retrying."
            ) from exc
        click.echo(f"Deployed {version} to production ({record['id']})")
        return
    if not fx.acquire_deploy_lock():
        raise click.ClickException(
            f"another deployment is in progress (lock held by {fx.LOCK_HOLDER}). "
            f"Wait {fx.LOCK_RETRY_MS // 1000}s and try again."
        )
    click.echo(f"Deployed {version} to {env} (deploy-new-001)")


@cli.group()
def health() -> None:
    """Service health commands."""


@health.command()
@click.option("--deep", is_flag=True, help="Also probe the CDN edge (slow)")
def check(deep: bool) -> None:
    """Run health checks against all services."""
    click.echo(f"warning: health cache is stale ({fx.CACHE_AGE_HOURS}h old)", err=True)
    click.echo("api-server ... ok")
    click.echo("database ... ok")
    click.echo(f"registry ... FAILED (credential expired at {fx.REGISTRY_EXPIRED_AT})", err=True)
    checked = 3
    if deep:
        click.echo(f"cdn ... {fx.probe_cdn(None)}")
        checked = 4
    click.echo(f"{checked} services checked, 1 failed")
    sys.exit(1)


if __name__ == "__main__":
    cli()
