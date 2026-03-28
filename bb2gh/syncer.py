"""Continuous sync from Bitbucket to GitHub."""

import logging
import os
import signal
import subprocess
import time

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
    """Remove hidden refs that can't be pushed to GitHub."""
    try:
        output = _run_git(["show-ref"], cwd=bare_repo_path)
    except subprocess.CalledProcessError:
        return

    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        ref = parts[1]
        if "/pull/" in ref or "/merge-request" in ref:
            try:
                _run_git(["update-ref", "-d", ref], cwd=bare_repo_path)
            except subprocess.CalledProcessError:
                pass


class Syncer:
    """Continuously syncs migrated repos from Bitbucket to GitHub."""

    def __init__(self, config):
        self.config = config
        self.state = State(config.work_dir)
        self._running = True

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum, frame):
        logger.info("Received signal %d, shutting down gracefully...", signum)
        self._running = False

    def run(self):
        """Run the sync loop."""
        logger.info(
            "Starting continuous sync (interval: %ds)", self.config.sync_interval
        )

        while self._running:
            self._sync_all()
            self._sleep(self.config.sync_interval)

        logger.info("Syncer stopped.")

    def _sleep(self, seconds):
        """Interruptible sleep."""
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(1, end - time.time()))

    def _sync_all(self):
        """Sync all migrated repos."""
        repos = self.state.get_migrated_repos()
        if not repos:
            logger.warning("No migrated repos found. Run 'bb2gh migrate' first.")
            return

        synced = 0
        failed = 0

        for project_key, repo_slug in repos:
            if not self._running:
                break
            try:
                self._sync_repo(project_key, repo_slug)
                synced += 1
            except Exception:
                logger.exception("Failed to sync %s/%s", project_key, repo_slug)
                failed += 1

        logger.info("Sync cycle complete: %d synced, %d failed", synced, failed)

    def _sync_repo(self, project_key, repo_slug):
        """Sync a single repo: fetch from Bitbucket, push to GitHub."""
        bare_path = os.path.join(
            self.config.work_dir, f"{project_key}__{repo_slug}.git"
        )

        if not os.path.exists(bare_path):
            logger.error("Bare repo not found: %s", bare_path)
            return

        # Look up the GitHub target from state (set during migration)
        gh_org, gh_repo_name = self.state.get_github_target(project_key, repo_slug)
        target_label = f"{gh_org}/{gh_repo_name}" if gh_org else "github"

        start = time.time()

        # Fetch from Bitbucket (origin)
        _run_git(["fetch", "origin", "--prune"], cwd=bare_path)

        # Clean hidden refs before pushing
        _clean_hidden_refs(bare_path)

        # Push to GitHub
        _run_git(["push", "github", "--mirror"], cwd=bare_path)

        elapsed = time.time() - start
        self.state.update_sync_time(project_key, repo_slug)
        logger.info(
            "Synced %s/%s -> %s in %.1fs",
            project_key, repo_slug, target_label, elapsed,
        )
