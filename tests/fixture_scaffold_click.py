"""A small click CLI for treaty scaffold-from"""

from pathlib import Path

import click


@click.group(help="Manage deployments")
@click.option("--config", type=click.Path(path_type=Path), help="Settings file")
@click.option("-v", "--verbose", count=True, help="Say more")
def cli(config: Path | None, verbose: int) -> None:
    pass


@cli.group(help="Releases of a service")
def release() -> None:
    pass


@release.command("rollback", help="Roll a service back to an earlier release")
@click.argument("service")
@click.option("--to", "target", required=True, help="Release to roll back to")
@click.option("--replicas", type=click.IntRange(1, 10), default=2, help="How many replicas")
@click.option("--region", "regions", multiple=True, help="Regions to roll back")
@click.option("--strategy", type=click.Choice(["rolling", "recreate"]), default="rolling")
@click.option("--dry-run/--no-dry-run", default=False, help="Only print the plan")
@click.option("--api-token", envvar="DEPLOY_TOKEN", help="Token for the API")
@click.pass_context
def rollback(
    ctx: click.Context,
    service: str,
    target: str,
    replicas: int,
    regions: tuple[str, ...],
    strategy: str,
    dry_run: bool,
    api_token: str | None,
) -> None:
    pass


@cli.command("status")
@click.argument("services", nargs=-1)
def status(services: tuple[str, ...]) -> None:
    """Show the state of each service.

    Lists every service when none is named.
    """
