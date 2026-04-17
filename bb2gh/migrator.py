"""Bulk migration of repositories from Bitbucket Server to GitHub Enterprise."""

import logging
import os
import subprocess

from .bitbucket_client import BitbucketClient
from .github_client import GithubClient
from .state import State
from .submodules import remap_submodules_in_bare_repo

logger = logging.getLogger(__name__)


def _redact(text):
    """Remove tokens/passwords from URLs in log output."""
    import re
    return re.sub(r"(https?://)[^@/]+@", r"\1***@", text)


def _run_git(args, cwd=None, quiet=False):
    """Run a git command and return stdout."""
    cmd = ["git"] + args
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        if not quiet:
            logger.error("git %s failed: %s", args[0], _redact(result.stderr.strip()))
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


def _migrate_lfs(bare_path, threshold):
    """Convert files above threshold to Git LFS in all branches.

    git lfs migrate import requires a working tree, so we clone the
    bare repo to a temp directory, create local branches for all remotes,
    run LFS migration, then fetch the rewritten refs back.
    """
    import shutil
    import tempfile

    logger.info("Running LFS migration (threshold: %s) in %s", threshold, bare_path)
    tmp_dir = tempfile.mkdtemp(suffix=".lfs-migrate")
    try:
        work_path = os.path.join(tmp_dir, "work")
        _run_git(["clone", bare_path, work_path])

        # Create local branches for ALL remote branches so LFS rewrites them all
        branches_output = _run_git(["branch", "-r"], cwd=work_path)
        for line in branches_output.splitlines():
            branch = line.strip()
            if "HEAD" in branch or not branch.startswith("origin/"):
                continue
            local_name = branch.replace("origin/", "", 1)
            try:
                _run_git(["branch", "--track", local_name, branch], cwd=work_path, quiet=True)
            except subprocess.CalledProcessError:
                pass  # Already exists (default branch)

        _run_git(["lfs", "install"], cwd=work_path)
        _run_git(
            ["lfs", "migrate", "import", "--everything",
             f"--above={threshold}", "--yes"],
            cwd=work_path,
        )

        # Fetch rewritten branches and tags back into the bare repo
        _run_git(["remote", "add", "lfs-source", work_path], cwd=bare_path)
        _run_git(["fetch", "lfs-source", "--force",
                  "+refs/heads/*:refs/heads/*",
                  "+refs/tags/*:refs/tags/*"], cwd=bare_path)
        _run_git(["remote", "remove", "lfs-source"], cwd=bare_path)

        # Copy LFS objects into the bare repo
        lfs_src = os.path.join(work_path, ".git", "lfs")
        lfs_dst = os.path.join(bare_path, "lfs")
        if os.path.exists(lfs_src):
            if os.path.exists(lfs_dst):
                shutil.rmtree(lfs_dst)
            shutil.copytree(lfs_src, lfs_dst)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    logger.info("LFS migration complete for %s", bare_path)


def migrate_repos(config):
    """Run the full bulk migration.

    For each repo in Bitbucket:
    1. Resolve the target GitHub org and repo name via config mapping
    2. Create the repo on GitHub
    3. Bare-clone from Bitbucket via SSH
    4. Clean hidden refs
    5. Push --mirror to GitHub
    6. Record in state
    """
    bb = BitbucketClient(config.bb_base_url, config.bb_token, verify_ssl=config.bb_verify_ssl)
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

            if not config.should_migrate_repo(project_key, repo_slug):
                logger.info(
                    "Skipping %s/%s (filtered out by include/exclude_repos)",
                    project_key, repo_slug,
                )
                total_skipped += 1
                continue

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
    # Resolve target GitHub org and repo name
    gh_org, gh_repo_name = config.resolve_target(project_key, repo_slug)
    logger.info(
        "Migrating %s/%s -> %s/%s ...",
        project_key, repo_slug, gh_org, gh_repo_name,
    )

    # 1. Create repo on GitHub (in the resolved org)
    import re
    description = repo.get("description", "") or f"Migrated from Bitbucket: {project_key}/{repo_slug}"
    description = re.sub(r"[\x00-\x1f\x7f]", " ", description).strip()[:350]
    gh.create_repo(gh_repo_name, description=description, private=True, org_name=gh_org)

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

    # 4. Remap submodule URLs from Bitbucket to GitHub
    remap_submodules_in_bare_repo(bare_path, config)

    # 5. Migrate large files to LFS if enabled
    if config.lfs_enabled:
        _migrate_lfs(bare_path, config.lfs_threshold)

    # 6. Add GitHub remote and push
    gh_clone_url = gh.get_clone_url(gh_repo_name, org_name=gh_org)

    # Remove existing github remote if present, then add
    try:
        _run_git(["remote", "remove", "github"], cwd=bare_path, quiet=True)
    except subprocess.CalledProcessError:
        pass  # Remote didn't exist

    _run_git(["remote", "add", "github", gh_clone_url], cwd=bare_path)
    _run_git(["push", "--mirror", "github"], cwd=bare_path)

    # Push LFS objects separately (mirror push only sends git objects)
    if config.lfs_enabled:
        try:
            _run_git(["lfs", "push", "--all", "github"], cwd=bare_path)
        except subprocess.CalledProcessError:
            logger.warning("LFS push failed for %s/%s (LFS may not be enabled on GitHub)", gh_org, gh_repo_name)

    # 7. Set default branch on GitHub to match Bitbucket's HEAD
    try:
        head_ref = _run_git(["symbolic-ref", "HEAD"], cwd=bare_path)
        default_branch = head_ref.replace("refs/heads/", "")
        gh.set_default_branch(gh_repo_name, default_branch, org_name=gh_org)
    except Exception:
        logger.warning("Could not set default branch for %s/%s", gh_org, gh_repo_name)

    # 7. Record in state (includes the resolved GitHub org and repo name)
    state.mark_migrated(project_key, repo_slug, gh_org=gh_org, gh_repo_name=gh_repo_name)
    logger.info("Successfully migrated %s/%s -> %s/%s", project_key, repo_slug, gh_org, gh_repo_name)
