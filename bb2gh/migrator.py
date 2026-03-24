"""Bulk migration of repositories from Bitbucket Server to GitHub Enterprise."""

import logging
import os
import subprocess

from .bitbucket_client import BitbucketClient
from .github_client import GithubClient
from .state import State

logger = logging.getLogger(__name__)


def _run_git(args, cwd=None):
    """Run a git command and return stdout."""
    cmd = ["git"] + args
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        logger.error("git %s failed: %s", args[0], result.stderr.strip())
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )
    return result.stdout.strip()


def _clean_hidden_refs(bare_repo_path):
    """Remove hidden refs (refs/pull/*, refs/merge-requests/*) that can't be pushed."""
    try:
        output = _run_git(["show-ref"], cwd=bare_repo_path)
    except subprocess.CalledProcessError:
        return  # No refs to clean

    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        ref = parts[1]
        if "/pull/" in ref or "/merge-request" in ref:
            try:
                _run_git(["update-ref", "-d", ref], cwd=bare_repo_path)
                logger.debug("Deleted hidden ref: %s", ref)
            except subprocess.CalledProcessError:
                logger.warning("Failed to delete ref: %s", ref)


def migrate_repos(config):
    """Run the full bulk migration.

    For each repo in Bitbucket:
    1. Create the repo on GitHub
    2. Bare-clone from Bitbucket via SSH
    3. Clean hidden refs
    4. Push --mirror to GitHub
    5. Record in state
    """
    bb = BitbucketClient(config.bb_base_url, config.bb_token)
    gh = GithubClient(config.gh_base_url, config.gh_token, config.gh_org)
    state = State(config.work_dir)

    os.makedirs(config.work_dir, exist_ok=True)

    projects = config.bb_projects or [p["key"] for p in bb.list_projects()]

    total_migrated = 0
    total_skipped = 0
    total_failed = 0

    for project_key in projects:
        logger.info("Processing project: %s", project_key)
        repos = bb.list_repos(project_key)

        for repo in repos:
            repo_slug = repo["slug"]
            repo_name = repo.get("name", repo_slug)

            if state.is_migrated(project_key, repo_slug):
                logger.info("Skipping already migrated: %s/%s", project_key, repo_slug)
                total_skipped += 1
                continue

            try:
                _migrate_single_repo(
                    config, bb, gh, state, project_key, repo_slug, repo_name, repo
                )
                total_migrated += 1
            except Exception:
                logger.exception("Failed to migrate %s/%s", project_key, repo_slug)
                total_failed += 1

    logger.info(
        "Migration complete: %d migrated, %d skipped, %d failed",
        total_migrated, total_skipped, total_failed,
    )
    return total_migrated, total_skipped, total_failed


def _migrate_single_repo(config, bb, gh, state, project_key, repo_slug, repo_name, repo):
    """Migrate a single repository."""
    logger.info("Migrating %s/%s ...", project_key, repo_slug)

    # 1. Create repo on GitHub
    description = repo.get("description", "") or f"Migrated from Bitbucket: {project_key}/{repo_slug}"
    gh.create_repo(repo_slug, description=description, private=True)

    # 2. Bare clone from Bitbucket
    bare_path = os.path.join(config.work_dir, f"{project_key}__{repo_slug}.git")

    if os.path.exists(bare_path):
        # Already cloned, fetch latest
        logger.info("Bare clone exists, fetching latest: %s", bare_path)
        _run_git(["fetch", "origin", "--prune"], cwd=bare_path)
    else:
        clone_url = bb.get_repo_clone_url(repo, protocol="ssh")
        if not clone_url:
            # Fallback: construct SSH URL from config
            clone_url = f"{config.bb_ssh_url}/{project_key.lower()}/{repo_slug}.git"

        logger.info("Cloning %s -> %s", clone_url, bare_path)
        _run_git(["clone", "--bare", clone_url, bare_path])

    # 3. Clean hidden refs
    _clean_hidden_refs(bare_path)

    # 4. Add GitHub remote and push
    gh_clone_url = gh.get_clone_url(repo_slug)

    # Remove existing github remote if present, then add
    try:
        _run_git(["remote", "remove", "github"], cwd=bare_path)
    except subprocess.CalledProcessError:
        pass  # Remote didn't exist

    _run_git(["remote", "add", "github", gh_clone_url], cwd=bare_path)
    _run_git(["push", "--mirror", "github"], cwd=bare_path)

    # 5. Record in state
    state.mark_migrated(project_key, repo_slug)
    logger.info("Successfully migrated %s/%s", project_key, repo_slug)
