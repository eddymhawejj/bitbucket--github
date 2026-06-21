"""Bitbucket Server REST API client."""

import logging
import requests

logger = logging.getLogger(__name__)


class BitbucketClient:
    """Client for Bitbucket Server (Data Center) REST API v1.0."""

    def __init__(self, base_url, token, verify_ssl=True):
        self.base_url = base_url.rstrip("/")
        self.api_url = f"{self.base_url}/rest/api/1.0"
        self.session = requests.Session()
        self.session.verify = verify_ssl
        if not verify_ssl:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def _paginate(self, url, params=None):
        """Iterate through all pages of a Bitbucket paginated endpoint."""
        params = dict(params or {})
        params.setdefault("limit", 25)

        while True:
            resp = self.session.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

            yield from data.get("values", [])

            if data.get("isLastPage", True):
                break
            params["start"] = data["nextPageStart"]

    def list_projects(self):
        """List all projects on the Bitbucket instance."""
        url = f"{self.api_url}/projects"
        return list(self._paginate(url))

    def resolve_project_key(self, project_key):
        """Resolve a project key, following Bitbucket aliases for renamed projects.

        Returns the current/canonical project key, or None if not found.
        """
        url = f"{self.api_url}/projects/{project_key}"
        try:
            resp = self.session.get(url)
            if resp.status_code == 200:
                return resp.json().get("key")
        except Exception:
            pass
        return None

    def resolve_repo_location(self, project_key, repo_slug):
        """Resolve a repo's current project and slug, following moves/aliases.

        When a repo is moved from one project to another, Bitbucket keeps
        the old URL alive. This method returns the current (project_key, slug).
        """
        url = f"{self.api_url}/projects/{project_key}/repos/{repo_slug}"
        try:
            resp = self.session.get(url)
            if resp.status_code == 200:
                data = resp.json()
                real_project = data.get("project", {}).get("key")
                real_slug = data.get("slug")
                if real_project:
                    return real_project, real_slug or repo_slug
        except Exception:
            pass
        return None, None

    def list_repos(self, project_key):
        """List all repositories in a project."""
        url = f"{self.api_url}/projects/{project_key}/repos"
        return list(self._paginate(url))

    def list_pull_requests(self, project_key, repo_slug, state="OPEN"):
        """List pull requests for a repository.

        Args:
            state: OPEN, DECLINED, MERGED, or ALL
        """
        url = f"{self.api_url}/projects/{project_key}/repos/{repo_slug}/pull-requests"
        return list(self._paginate(url, params={"state": state}))

    def get_pull_request(self, project_key, repo_slug, pr_id):
        """Get a single pull request with full details (including merge properties)."""
        url = (
            f"{self.api_url}/projects/{project_key}/repos/{repo_slug}"
            f"/pull-requests/{pr_id}"
        )
        resp = self.session.get(url)
        resp.raise_for_status()
        return resp.json()

    def get_merge_commit(self, project_key, repo_slug, pr_id):
        """Extract the merge/squash commit SHA for a merged PR.

        Bitbucket Server stores this in properties.mergeCommit on the PR object.
        Returns the SHA string, or None if not available.
        """
        try:
            pr = self.get_pull_request(project_key, repo_slug, pr_id)
            merge_commit = pr.get("properties", {}).get("mergeCommit", {})
            sha = merge_commit.get("id") or merge_commit.get("displayId")
            if sha:
                return sha
        except Exception:
            logger.debug("Could not fetch merge commit from PR properties for PR #%d", pr_id)
        return None

    def get_pr_activities(self, project_key, repo_slug, pr_id):
        """Get activities (comments, approvals, etc.) for a pull request."""
        url = (
            f"{self.api_url}/projects/{project_key}/repos/{repo_slug}"
            f"/pull-requests/{pr_id}/activities"
        )
        return list(self._paginate(url))

    def get_pr_diff(self, project_key, repo_slug, pr_id):
        """Get the diff for a pull request."""
        url = (
            f"{self.api_url}/projects/{project_key}/repos/{repo_slug}"
            f"/pull-requests/{pr_id}/diff"
        )
        resp = self.session.get(url)
        resp.raise_for_status()
        return resp.json()

    def get_repo_clone_url(self, repo, protocol="ssh"):
        """Extract clone URL from a repo object.

        Args:
            repo: Repo dict from the Bitbucket API.
            protocol: 'ssh' or 'http'.
        """
        for link in repo.get("links", {}).get("clone", []):
            if link.get("name") == protocol:
                return link["href"]
        return None
