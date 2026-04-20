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


def _has_large_blobs(bare_path, threshold):
    """Check if a bare repo has any reachable blobs above the threshold.

    Only checks blobs reachable from refs (what push --mirror would send).
    Uses a shell pipeline for reliable streaming on large repos.
    """
    import glob

    t = threshold.lower().strip()
    if t.endswith("mb"):
        threshold_bytes = int(t[:-2]) * 1024 * 1024
    elif t.endswith("gb"):
        threshold_bytes = int(t[:-2]) * 1024 * 1024 * 1024
    elif t.endswith("kb"):
        threshold_bytes = int(t[:-2]) * 1024
    else:
        threshold_bytes = int(t)

    # Shell pipeline: list reachable objects, strip paths, check sizes, stop at first match
    cmd = (
        "git rev-list --objects --all"
        " | cut -d' ' -f1"
        " | git cat-file --batch-check='%(objecttype) %(objectsize)'"
        f" | awk '$1 == \"blob\" && $2 > {threshold_bytes} {{print; exit}}'"
    )
    proc = subprocess.Popen(
        cmd, shell=True, cwd=bare_path,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    line = proc.stdout.readline()
    proc.terminate()
    proc.wait()
    return len(line.strip()) > 0


def _migrate_lfs(bare_path, threshold):
    """Convert files above threshold to Git LFS in all branches.

    git lfs migrate import requires a working tree, so we clone the
    bare repo to a temp directory, create local branches for all remotes,
    run LFS migration, then fetch the rewritten refs back.
    """
    import shutil
    import tempfile

    if not _has_large_blobs(bare_path, threshold):
        logger.info("LFS: no files above %s in %s, skipping", threshold, os.path.basename(bare_path))
        return False

    logger.info("LFS: large files detected, migrating (threshold: %s) in %s", threshold, bare_path)
    tmp_dir = tempfile.mkdtemp(suffix=".lfs-migrate")
    try:
        work_path = os.path.join(tmp_dir, "work")
        # Skip LFS smudge during clone — repo may already have LFS pointers
        # pointing to the original BB LFS server
        env_no_lfs = {
            "GIT_LFS_SKIP_SMUDGE": "1",
        }
        cmd = ["git", "clone", bare_path, work_path]
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False,
            env={**os.environ, **env_no_lfs},
        )
        if result.returncode != 0:
            logger.error("git clone failed: %s", result.stderr.strip())
            raise subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr
            )

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
        try:
            _run_git(
                ["lfs", "migrate", "import", "--everything",
                 f"--above={threshold}", "--yes"],
                cwd=work_path,
            )
        except subprocess.CalledProcessError as e:
            # LFS migrate may fail on post-rewrite checkout (unborn branch, etc.)
            # but the rewrite itself completed. Check stderr for this case.
            if "Could not checkout" in (e.stderr or "") and "Rewriting commits" in (e.stderr or ""):
                logger.warning("LFS rewrite completed but checkout failed (harmless)")
            else:
                raise

        # Fetch rewritten branches and tags back into the bare repo
        _run_git(["remote", "add", "lfs-source", work_path], cwd=bare_path)
        _run_git(["fetch", "lfs-source", "--force",
                  "+refs/heads/*:refs/heads/*",
                  "+refs/tags/*:refs/tags/*"], cwd=bare_path)
        _run_git(["remote", "remove", "lfs-source"], cwd=bare_path)

        # Check if LFS actually tracked any files (not just empty dirs)
        lfs_objects_dir = os.path.join(work_path, ".git", "lfs", "objects")
        has_lfs_objects = False
        if os.path.exists(lfs_objects_dir):
            for dirpath, dirnames, filenames in os.walk(lfs_objects_dir):
                if filenames:
                    has_lfs_objects = True
                    break

        # Copy LFS objects into the bare repo only if real objects exist
        if has_lfs_objects:
            lfs_dst = os.path.join(bare_path, "lfs")
            if os.path.exists(lfs_dst):
                shutil.rmtree(lfs_dst)
            shutil.copytree(os.path.join(work_path, ".git", "lfs"), lfs_dst)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if has_lfs_objects:
        logger.info("LFS migration converted files in %s", bare_path)
    else:
        logger.info("LFS migration: no files above threshold in %s", bare_path)
    return has_lfs_objects


def migrate_repos(config, only_repos=None):
    """Run the full bulk migration.

    Args:
        config: Config object.
        only_repos: Optional set of "PROJECT/SLUG" strings to migrate.
                    If provided, only these repos are processed.
    """
    bb = BitbucketClient(config.bb_base_url, config.bb_token, verify_ssl=config.bb_verify_ssl)
    gh = GithubClient(config.gh_base_url, config.gh_token, config.gh_org)
    state = State(config.work_dir)

    os.makedirs(config.work_dir, exist_ok=True)

    if only_repos:
        # Extract unique project keys from the repo list
        projects = list({r.split("/")[0] for r in only_repos})
    else:
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

            if only_repos and f"{project_key}/{repo_slug}" not in only_repos:
                continue

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
            except Exception as e:
                logger.exception("Failed to migrate %s/%s", project_key, repo_slug)
                gh_org, gh_repo_name = config.resolve_target(project_key, repo_slug)
                state.record_failure(
                    project_key, repo_slug, str(e),
                    gh_org=gh_org, gh_repo_name=gh_repo_name,
                )
                total_failed += 1

            # Throttle between repos to avoid SSH/API rate limits
            import time
            time.sleep(config.migrate_delay)

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
        _run_git(["fetch", "origin", "--prune",
                  "+refs/heads/*:refs/heads/*",
                  "+refs/tags/*:refs/tags/*"], cwd=bare_path)
    else:
        clone_url = bb.get_repo_clone_url(repo, protocol="ssh")
        if not clone_url:
            # Fallback: construct SSH URL from config
            clone_url = f"{config.bb_ssh_url}/{project_key.lower()}/{repo_slug}.git"

        logger.info("Cloning %s -> %s", clone_url, bare_path)
        _run_git(["clone", "--bare", clone_url, bare_path])

    # 3. Clean hidden refs
    _clean_hidden_refs(bare_path)

    # Track migration details
    warnings = []

    # 4. Remap submodule URLs from Bitbucket to GitHub
    submodules_remapped = remap_submodules_in_bare_repo(bare_path, config)
    has_submodules = submodules_remapped > 0
    # Check if repo has .gitmodules but remap returned 0 (skipped due to unresolvable URLs)
    try:
        _run_git(["show", "HEAD:.gitmodules"], cwd=bare_path, quiet=True)
        has_submodules = True
        if submodules_remapped == 0:
            warnings.append("Has .gitmodules but submodule URLs could not be fully remapped")
    except subprocess.CalledProcessError:
        pass

    # 5. Migrate large files to LFS if enabled
    has_lfs = False
    if config.lfs_enabled:
        has_lfs = _migrate_lfs(bare_path, config.lfs_threshold)

    # 6. Add GitHub remote and push
    gh_clone_url = gh.get_clone_url(gh_repo_name, org_name=gh_org, ssh_url=config.gh_ssh_url or None)

    # Remove existing github remote if present, then add
    try:
        _run_git(["remote", "remove", "github"], cwd=bare_path, quiet=True)
    except subprocess.CalledProcessError:
        pass  # Remote didn't exist

    _run_git(["remote", "add", "github", gh_clone_url], cwd=bare_path)
    _run_git(["push", "--mirror", "github"], cwd=bare_path)

    # Push LFS objects separately — only if LFS actually converted files
    if has_lfs:
        try:
            _run_git(["lfs", "push", "--all", "github"], cwd=bare_path)
        except subprocess.CalledProcessError:
            logger.warning("LFS push failed for %s/%s", gh_org, gh_repo_name)
            warnings.append("LFS push failed")

    # 7. Set default branch on GitHub to match Bitbucket's HEAD
    try:
        head_ref = _run_git(["symbolic-ref", "HEAD"], cwd=bare_path)
        default_branch = head_ref.replace("refs/heads/", "")
        gh.set_default_branch(gh_repo_name, default_branch, org_name=gh_org)
    except Exception:
        logger.warning("Could not set default branch for %s/%s", gh_org, gh_repo_name)
        warnings.append("Could not set default branch")

    # 8. Record in state
    state.mark_migrated(
        project_key, repo_slug, gh_org=gh_org, gh_repo_name=gh_repo_name,
        has_submodules=has_submodules, submodules_remapped=submodules_remapped > 0,
        has_lfs=has_lfs, warnings=warnings,
    )
    logger.info("Successfully migrated %s/%s -> %s/%s", project_key, repo_slug, gh_org, gh_repo_name)
