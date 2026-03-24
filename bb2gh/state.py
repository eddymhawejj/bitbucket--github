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

    def mark_migrated(self, project_key, repo_slug):
        """Record that a repo has been migrated."""
        key = f"{project_key}/{repo_slug}"
        self._data["repos"][key] = {
            "project_key": project_key,
            "repo_slug": repo_slug,
            "status": "migrated",
            "migrated_at": self._now(),
            "last_sync": self._now(),
            "pr_mappings": {},
        }
        self._save()
        logger.info("Marked %s as migrated", key)

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

    def is_migrated(self, project_key, repo_slug):
        """Check if a repo has been migrated."""
        key = f"{project_key}/{repo_slug}"
        return key in self._data["repos"]

    def is_pr_migrated(self, project_key, repo_slug, bb_pr_id):
        """Check if a specific PR has already been migrated."""
        key = f"{project_key}/{repo_slug}"
        repo_state = self._data["repos"].get(key, {})
        return str(bb_pr_id) in repo_state.get("pr_mappings", {})
