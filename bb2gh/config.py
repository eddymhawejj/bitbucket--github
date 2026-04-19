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
        self.bb_ssh_hostnames = bb.get("ssh_hostnames", [])
        self.bb_projects = bb.get("projects")  # None means all projects
        self.bb_verify_ssl = bb.get("verify_ssl", True)

        # GitHub settings
        gh = raw["github"]
        self.gh_base_url = gh["base_url"].rstrip("/")
        self.gh_token = gh.get("token") or os.environ.get("GH_TOKEN", "")
        self.gh_org = gh["org"]
        self.gh_ssh_host = gh.get("ssh_host", "")
        self.gh_ssh_url = gh.get("ssh_url", "")

        # Sync settings
        sync = raw.get("sync", {})
        self.sync_interval = sync.get("interval_seconds", 60)
        self.work_dir = sync.get("work_dir", "/data/mirror")
        self.migrate_delay = sync.get("migrate_delay_seconds", 2)
        self.sync_exclude_projects = set(
            p.upper() for p in sync.get("exclude_projects", [])
        )

        # User mapping (Bitbucket username -> GitHub username)
        self.user_mapping = raw.get("user_mapping", {})

        # LFS settings
        lfs = raw.get("lfs", {})
        self.lfs_enabled = lfs.get("enabled", False)
        self.lfs_threshold = lfs.get("threshold", "100mb")

        # Repository mapping (Bitbucket project/repo -> GitHub org/repo)
        rm = raw.get("repo_mapping", {})
        self._repo_mapping = rm
        self._name_template = rm.get("name_template", "{slug}")
        self._project_mappings = rm.get("projects", {})

    def resolve_target(self, project_key, repo_slug):
        """Resolve a Bitbucket project/repo to a GitHub org and repo name.

        Lookup order:
        1. Explicit per-repo override in repo_mapping.projects.<KEY>.repos.<slug>.github_name
        2. Per-project name_template override in repo_mapping.projects.<KEY>.name_template
        3. Global name_template from repo_mapping.name_template (default: "{slug}")

        For the org:
        1. Per-project github_org in repo_mapping.projects.<KEY>.github_org
        2. Global github.org

        Returns:
            (github_org, github_repo_name) tuple
        """
        project_conf = self._project_mappings.get(project_key, {})

        # Resolve org
        gh_org = project_conf.get("github_org", self.gh_org)

        # Resolve repo name: check explicit per-repo override first
        repos_conf = project_conf.get("repos", {})
        if repo_slug in repos_conf:
            repo_conf = repos_conf[repo_slug]
            gh_repo = repo_conf.get("github_name", repo_slug)
        else:
            # Use per-project template, falling back to global template
            template = project_conf.get("name_template", self._name_template)
            gh_repo = template.format(
                project=project_key,
                project_lower=project_key.lower(),
                slug=repo_slug,
            )

        return gh_org, gh_repo

    def should_migrate_repo(self, project_key, repo_slug):
        """Check if a repo should be migrated based on include/exclude lists.

        Resolution:
        - If `include_repos` is set for the project, the repo is migrated only
          if it is in that list (allowlist).
        - Otherwise, the repo is migrated unless it is in `exclude_repos`
          (denylist).
        - Projects with no repo filter config migrate all repos.

        Returns:
            True if the repo should be migrated.
        """
        project_conf = self._project_mappings.get(project_key, {})
        include = project_conf.get("include_repos")
        exclude = project_conf.get("exclude_repos", [])

        if include is not None:
            return repo_slug in include
        return repo_slug not in exclude

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
