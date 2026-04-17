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


@cli.command("reset-submodules")
@click.option("--dry-run", is_flag=True, help="Show what would be reset without changing state.")
@click.pass_context
def reset_submodules(ctx, dry_run):
    """Reset migrated repos that have .gitmodules so they get re-migrated.

    Scans bare clones for repos containing .gitmodules, removes them from
    state.json, so the next 'bb2gh migrate' run re-processes them (with
    submodule URL remapping).
    """
    import os
    import subprocess

    config = ctx.obj["config"]
    from .state import State
    state = State(config.work_dir)

    repos = state.get_migrated_repos()
    if not repos:
        click.echo("No migrated repos found.")
        return

    reset_count = 0
    for project_key, repo_slug in repos:
        bare_path = os.path.join(config.work_dir, f"{project_key}__{repo_slug}.git")
        if not os.path.exists(bare_path):
            continue

        result = subprocess.run(
            ["git", "show", "HEAD:.gitmodules"],
            cwd=bare_path, capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            continue

        if dry_run:
            click.echo(f"[DRY RUN] Would reset: {project_key}/{repo_slug}")
        else:
            state.reset_repo(project_key, repo_slug)
            click.echo(f"Reset: {project_key}/{repo_slug}")
        reset_count += 1

    click.echo(f"\n{'Would reset' if dry_run else 'Reset'} {reset_count} repos with submodules.")
    if not dry_run and reset_count > 0:
        click.echo("Run 'bb2gh migrate' to re-migrate them.")


@cli.command("reset-lfs")
@click.option("--dry-run", is_flag=True, help="Show what would be reset without changing state.")
@click.pass_context
def reset_lfs(ctx, dry_run):
    """Reset migrated repos that were LFS-migrated so they get re-processed.

    Finds repos where LFS migration previously ran (have lfs/ directory
    or .gitattributes with LFS patterns), removes them from state.json
    so the next 'bb2gh migrate' re-fetches from Bitbucket and re-runs
    LFS with the current threshold.
    """
    import os
    import subprocess

    config = ctx.obj["config"]
    from .state import State
    state = State(config.work_dir)

    repos = state.get_migrated_repos()
    if not repos:
        click.echo("No migrated repos found.")
        return

    reset_count = 0
    for project_key, repo_slug in repos:
        bare_path = os.path.join(config.work_dir, f"{project_key}__{repo_slug}.git")
        if not os.path.exists(bare_path):
            continue

        # Check for LFS indicators
        has_lfs_dir = os.path.exists(os.path.join(bare_path, "lfs", "objects"))

        has_lfs_attrs = False
        result = subprocess.run(
            ["git", "show", "HEAD:.gitattributes"],
            cwd=bare_path, capture_output=True, text=True, check=False,
        )
        if result.returncode == 0 and "filter=lfs" in result.stdout:
            has_lfs_attrs = True

        if not has_lfs_dir and not has_lfs_attrs:
            continue

        if dry_run:
            click.echo(f"[DRY RUN] Would reset: {project_key}/{repo_slug}")
        else:
            state.reset_repo(project_key, repo_slug)
            click.echo(f"Reset: {project_key}/{repo_slug}")
        reset_count += 1

    click.echo(f"\n{'Would reset' if dry_run else 'Reset'} {reset_count} repos with previous LFS migration.")
    if not dry_run and reset_count > 0:
        click.echo("Run 'bb2gh migrate' to re-migrate them with the current LFS threshold.")


if __name__ == "__main__":
    cli()
