"""State tracking for migration progress."""

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

STATE_FILE = "state.json"


class State:
    """Tracks migration state in a JSON file."""

    def __init__(self, work_dir):
        self.path = os.path.join(work_dir, STATE_FILE)
        self._data = self._load()

    def _load(self):
        if os.path.exists(self.path):
            with open(self.path) as f:
                return json.load(f)
        return {"repos": {}}

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self._data, f, indent=2)

    def _now(self):
        return datetime.now(timezone.utc).isoformat()

    def mark_migrated(self, project_key, repo_slug, gh_org=None, gh_repo_name=None,
                      has_submodules=False, submodules_remapped=False,
                      has_lfs=False, warnings=None):
        """Record that a repo has been migrated.

        Args:
            project_key: Bitbucket project key.
            repo_slug: Bitbucket repo slug.
            gh_org: GitHub organization the repo was migrated to.
            gh_repo_name: GitHub repository name.
            has_submodules: Whether the repo has .gitmodules.
            submodules_remapped: Whether submodule URLs were remapped.
            has_lfs: Whether large files were converted to LFS.
            warnings: List of warning strings.
        """
        key = f"{project_key}/{repo_slug}"
        self._data["repos"][key] = {
            "project_key": project_key,
            "repo_slug": repo_slug,
            "gh_org": gh_org,
            "gh_repo_name": gh_repo_name or repo_slug,
            "status": "migrated",
            "migrated_at": self._now(),
            "last_sync": self._now(),
            "has_submodules": has_submodules,
            "submodules_remapped": submodules_remapped,
            "has_lfs": has_lfs,
            "warnings": warnings or [],
            "pr_mappings": {},
        }
        self._save()
        logger.info("Marked %s as migrated -> %s/%s", key, gh_org, gh_repo_name)

    def record_failure(self, project_key, repo_slug, error_message,
                       gh_org=None, gh_repo_name=None):
        """Record that a repo failed to migrate."""
        key = f"{project_key}/{repo_slug}"
        self._data["repos"][key] = {
            "project_key": project_key,
            "repo_slug": repo_slug,
            "gh_org": gh_org,
            "gh_repo_name": gh_repo_name,
            "status": "failed",
            "failed_at": self._now(),
            "error": error_message,
        }
        self._save()

    def update_sync_time(self, project_key, repo_slug):
        """Update the last sync timestamp for a repo."""
        key = f"{project_key}/{repo_slug}"
        if key in self._data["repos"]:
            self._data["repos"][key]["last_sync"] = self._now()
            self._save()

    def record_pr_mapping(self, project_key, repo_slug, bb_pr_id, gh_pr_number):
        """Record the mapping between a Bitbucket PR and GitHub PR."""
        key = f"{project_key}/{repo_slug}"
        if key in self._data["repos"]:
            self._data["repos"][key]["pr_mappings"][str(bb_pr_id)] = gh_pr_number
            self._save()

    def get_migrated_repos(self):
        """Return list of (project_key, repo_slug) for all migrated repos."""
        result = []
        for entry in self._data["repos"].values():
            if entry["status"] == "migrated":
                result.append((entry["project_key"], entry["repo_slug"]))
        return result

    def get_github_target(self, project_key, repo_slug):
        """Get the GitHub org and repo name for a migrated repo.

        Returns:
            (gh_org, gh_repo_name) tuple, or (None, None) if not found.
        """
        key = f"{project_key}/{repo_slug}"
        entry = self._data["repos"].get(key, {})
        return entry.get("gh_org"), entry.get("gh_repo_name")

    def reset_repo(self, project_key, repo_slug):
        """Remove a repo from state so it will be re-migrated on next run."""
        key = f"{project_key}/{repo_slug}"
        if key in self._data["repos"]:
            del self._data["repos"][key]
            self._save()
            return True
        return False

    def is_migrated(self, project_key, repo_slug):
        """Check if a repo has been migrated."""
        key = f"{project_key}/{repo_slug}"
        return key in self._data["repos"]

    def is_pr_migrated(self, project_key, repo_slug, bb_pr_id):
        """Check if a specific PR has already been migrated."""
        key = f"{project_key}/{repo_slug}"
        repo_state = self._data["repos"].get(key, {})
        return str(bb_pr_id) in repo_state.get("pr_mappings", {})
