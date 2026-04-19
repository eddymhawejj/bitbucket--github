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


@cli.command("reset")
@click.option("--project", multiple=True, help="Reset all repos in this project (can repeat).")
@click.option("--repo", multiple=True, help="Reset a specific PROJECT/SLUG (can repeat).")
@click.option("--all", "reset_all", is_flag=True, help="Reset ALL migrated repos.")
@click.option("--dry-run", is_flag=True, help="Show what would be reset without changing state.")
@click.pass_context
def reset(ctx, project, repo, reset_all, dry_run):
    """Reset repos in state.json so they get re-migrated.

    Examples:
      bb2gh reset --project UPSTREAM
      bb2gh reset --repo UPSTREAM/embeddedsw --repo UPSTREAM/git
      bb2gh reset --all
    """
    config = ctx.obj["config"]
    from .state import State
    state = State(config.work_dir)

    repos = state.get_all_repos()
    if not repos:
        click.echo("No repos found in state.")
        return
    repos_set = set(repo)

    reset_count = 0
    for project_key, repo_slug in repos:
        should_reset = False
        if reset_all:
            should_reset = True
        elif project_key in projects_set or project_key.upper() in projects_set:
            should_reset = True
        elif f"{project_key}/{repo_slug}" in repos_set:
            should_reset = True

        if not should_reset:
            continue

        if dry_run:
            gh_org, gh_repo = state.get_github_target(project_key, repo_slug)
            click.echo(f"[DRY RUN] Would reset: {project_key}/{repo_slug} (was -> {gh_org}/{gh_repo})")
        else:
            state.reset_repo(project_key, repo_slug)
            click.echo(f"Reset: {project_key}/{repo_slug}")
        reset_count += 1

    if reset_count == 0:
        click.echo("No matching repos found in state.")
    else:
        click.echo(f"\n{'Would reset' if dry_run else 'Reset'} {reset_count} repos.")
        if not dry_run:
            click.echo("Run 'bb2gh migrate' to re-migrate them.")


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

    repos = state.get_all_repos()
    if not repos:
        click.echo("No repos found in state.")
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

    repos = state.get_all_repos()
    if not repos:
        click.echo("No repos found in state.")
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


@cli.command()
@click.option("--format", "fmt", type=click.Choice(["text", "csv"]), default="text",
              help="Output format.")
@click.option("--output", "output_file", default=None, help="Write report to file.")
@click.pass_context
def report(ctx, fmt, output_file):
    """Generate a migration report from state.json.

    Shows migrated repos, failed repos, submodule status, LFS status,
    and any warnings.
    """
    import json

    config = ctx.obj["config"]
    from .state import State
    state = State(config.work_dir)

    data = state._data.get("repos", {})
    if not data:
        click.echo("No migration data found.")
        return

    migrated = []
    failed = []
    with_submodules = []
    submodules_not_remapped = []
    with_lfs = []
    with_warnings = []

    for key, entry in sorted(data.items()):
        status = entry.get("status", "unknown")
        if status == "migrated":
            migrated.append(entry)
        elif status == "failed":
            failed.append(entry)

        if entry.get("has_submodules"):
            with_submodules.append(entry)
            if not entry.get("submodules_remapped"):
                submodules_not_remapped.append(entry)
        if entry.get("has_lfs"):
            with_lfs.append(entry)
        if entry.get("warnings"):
            with_warnings.append(entry)

    lines = []

    if fmt == "text":
        lines.append("=" * 70)
        lines.append("MIGRATION REPORT")
        lines.append("=" * 70)
        lines.append("")
        lines.append(f"Total repos in state:     {len(data)}")
        lines.append(f"  Migrated:               {len(migrated)}")
        lines.append(f"  Failed:                 {len(failed)}")
        lines.append(f"  With submodules:        {len(with_submodules)}")
        lines.append(f"    Remapped:             {len(with_submodules) - len(submodules_not_remapped)}")
        lines.append(f"    Not remapped:         {len(submodules_not_remapped)}")
        lines.append(f"  With LFS:               {len(with_lfs)}")
        lines.append(f"  With warnings:          {len(with_warnings)}")

        if failed:
            lines.append("")
            lines.append("-" * 70)
            lines.append("FAILED REPOS")
            lines.append("-" * 70)
            for entry in failed:
                lines.append(f"  {entry['project_key']}/{entry['repo_slug']}")
                lines.append(f"    Target: {entry.get('gh_org', '?')}/{entry.get('gh_repo_name', '?')}")
                lines.append(f"    Error:  {entry.get('error', 'unknown')[:200]}")

        if submodules_not_remapped:
            lines.append("")
            lines.append("-" * 70)
            lines.append("SUBMODULES NOT REMAPPED")
            lines.append("-" * 70)
            for entry in submodules_not_remapped:
                lines.append(f"  {entry['project_key']}/{entry['repo_slug']} -> {entry.get('gh_org')}/{entry.get('gh_repo_name')}")

        if with_lfs:
            lines.append("")
            lines.append("-" * 70)
            lines.append("REPOS WITH LFS")
            lines.append("-" * 70)
            for entry in with_lfs:
                lines.append(f"  {entry['project_key']}/{entry['repo_slug']} -> {entry.get('gh_org')}/{entry.get('gh_repo_name')}")

        if with_warnings:
            lines.append("")
            lines.append("-" * 70)
            lines.append("WARNINGS")
            lines.append("-" * 70)
            for entry in with_warnings:
                for w in entry.get("warnings", []):
                    lines.append(f"  {entry['project_key']}/{entry['repo_slug']}: {w}")

        if migrated:
            lines.append("")
            lines.append("-" * 70)
            lines.append("ALL MIGRATED REPOS")
            lines.append("-" * 70)
            for entry in migrated:
                flags = []
                if entry.get("has_submodules"):
                    flags.append("submodules")
                if entry.get("has_lfs"):
                    flags.append("LFS")
                if entry.get("warnings"):
                    flags.append("warnings")
                flag_str = f" [{', '.join(flags)}]" if flags else ""
                lines.append(
                    f"  {entry['project_key']}/{entry['repo_slug']} "
                    f"-> {entry.get('gh_org')}/{entry.get('gh_repo_name')}{flag_str}"
                )

    elif fmt == "csv":
        lines.append("status,bb_project,bb_repo,gh_org,gh_repo,has_submodules,submodules_remapped,has_lfs,warnings,error")
        for key, entry in sorted(data.items()):
            warnings_str = "; ".join(entry.get("warnings", []))
            error_str = entry.get("error", "").replace(",", " ")[:200]
            lines.append(
                f"{entry.get('status', 'unknown')},"
                f"{entry.get('project_key', '')},"
                f"{entry.get('repo_slug', '')},"
                f"{entry.get('gh_org', '')},"
                f"{entry.get('gh_repo_name', '')},"
                f"{entry.get('has_submodules', False)},"
                f"{entry.get('submodules_remapped', False)},"
                f"{entry.get('has_lfs', False)},"
                f"\"{warnings_str}\","
                f"\"{error_str}\""
            )

    output = "\n".join(lines)

    if output_file:
        with open(output_file, "w") as f:
            f.write(output + "\n")
        click.echo(f"Report written to {output_file}")
    else:
        click.echo(output)


if __name__ == "__main__":
    cli()
