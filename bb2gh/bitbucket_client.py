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
