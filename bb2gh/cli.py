"""CLI entry point for bb2gh migration tool."""

import logging
import sys

import click

from .config import Config
from .migrator import migrate_repos
from .pr_migrator import migrate_pull_requests
from .syncer import Syncer


def _setup_logging(verbose):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


@click.group()
@click.option("--config", "config_path", default="config.yaml", help="Path to config file.")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
@click.pass_context
def cli(ctx, config_path, verbose):
    """bb2gh - Bitbucket Server to GitHub Enterprise migration tool."""
    _setup_logging(verbose)
    ctx.ensure_object(dict)
    try:
        ctx.obj["config"] = Config(config_path)
    except Exception as e:
        click.echo(f"Error loading config: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.pass_context
def migrate(ctx):
    """Bulk migrate all repositories from Bitbucket to GitHub.

    Clones repos via SSH, creates them on GitHub, and pushes all
    branches, tags, and history.
    """
    config = ctx.obj["config"]
    migrated, skipped, failed = migrate_repos(config)
    click.echo(f"\nMigration complete: {migrated} migrated, {skipped} skipped, {failed} failed")
    if failed > 0:
        sys.exit(1)


@cli.command()
@click.pass_context
def sync(ctx):
    """Continuously sync repos from Bitbucket to GitHub.

    Fetches changes from Bitbucket and pushes to GitHub every N seconds
    (configured via sync.interval_seconds). Runs until interrupted.
    """
    config = ctx.obj["config"]
    syncer = Syncer(config)
    syncer.run()


@cli.command("migrate-prs")
@click.option("--dry-run", is_flag=True, help="Log what would be done without making changes.")
@click.pass_context
def migrate_prs(ctx, dry_run):
    """Migrate open pull requests from Bitbucket to GitHub.

    Creates matching PRs on GitHub with title, description, comments,
    and reviewer assignments.
    """
    config = ctx.obj["config"]
    migrated, skipped, failed = migrate_pull_requests(config, dry_run=dry_run)
    click.echo(f"\nPR migration complete: {migrated} migrated, {skipped} skipped, {failed} failed")
    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    cli()
