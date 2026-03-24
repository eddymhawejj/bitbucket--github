"""Configuration loading and validation."""

import os
import yaml


class Config:
    """Loads and validates migration configuration from a YAML file."""

    def __init__(self, path="config.yaml"):
        with open(path) as f:
            raw = yaml.safe_load(f)

        self._validate(raw)
        self._raw = raw

        # Bitbucket settings
        bb = raw["bitbucket"]
        self.bb_base_url = bb["base_url"].rstrip("/")
        self.bb_token = bb.get("token") or os.environ.get("BB_TOKEN", "")
        self.bb_ssh_url = bb["ssh_url"].rstrip("/")
        self.bb_projects = bb.get("projects")  # None means all projects

        # GitHub settings
        gh = raw["github"]
        self.gh_base_url = gh["base_url"].rstrip("/")
        self.gh_token = gh.get("token") or os.environ.get("GH_TOKEN", "")
        self.gh_org = gh["org"]

        # Sync settings
        sync = raw.get("sync", {})
        self.sync_interval = sync.get("interval_seconds", 60)
        self.work_dir = sync.get("work_dir", "/data/mirror")

        # User mapping (Bitbucket username -> GitHub username)
        self.user_mapping = raw.get("user_mapping", {})

    @staticmethod
    def _validate(raw):
        for section in ("bitbucket", "github"):
            if section not in raw:
                raise ValueError(f"Missing required config section: {section}")

        bb = raw["bitbucket"]
        for key in ("base_url", "ssh_url"):
            if key not in bb:
                raise ValueError(f"Missing required bitbucket config: {key}")

        gh = raw["github"]
        for key in ("base_url", "org"):
            if key not in gh:
                raise ValueError(f"Missing required github config: {key}")
