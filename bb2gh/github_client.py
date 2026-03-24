"""GitHub Enterprise API client wrapper."""

import logging
from github import Github, GithubException

logger = logging.getLogger(__name__)


class GithubClient:
    """Wrapper around PyGithub for GitHub Enterprise operations."""

    def __init__(self, base_url, token, org_name):
        self.gh = Github(base_url=base_url, login_or_token=token)
        self.org = self.gh.get_organization(org_name)
        self.org_name = org_name

    def create_repo(self, name, description="", private=True):
        """Create a repository in the organization.

        Returns the repo object. If the repo already exists, returns the existing one.
        """
        try:
            repo = self.org.create_repo(
                name=name,
                description=description,
                private=private,
                auto_init=False,
            )
            logger.info("Created GitHub repo: %s/%s", self.org_name, name)
            return repo
        except GithubException as e:
            if e.status == 422:  # Already exists
                logger.info("GitHub repo already exists: %s/%s", self.org_name, name)
                return self.org.get_repo(name)
            raise

    def get_repo(self, name):
        """Get an existing repository."""
        return self.org.get_repo(name)

    def create_pull_request(self, repo_name, title, body, head, base):
        """Create a pull request on a GitHub repository.

        Args:
            repo_name: Repository name.
            title: PR title.
            body: PR body/description (markdown).
            head: Source branch name.
            base: Target branch name.

        Returns the created PR object.
        """
        repo = self.org.get_repo(repo_name)
        pr = repo.create_pull(title=title, body=body, head=head, base=base)
        logger.info("Created PR #%d on %s: %s", pr.number, repo_name, title)
        return pr

    def add_pr_comment(self, repo_name, pr_number, body):
        """Add a comment to a pull request."""
        repo = self.org.get_repo(repo_name)
        pr = repo.get_pull(pr_number)
        comment = pr.create_issue_comment(body)
        return comment

    def add_pr_reviewers(self, repo_name, pr_number, reviewers):
        """Request reviewers on a pull request.

        Args:
            reviewers: List of GitHub usernames.
        """
        if not reviewers:
            return
        repo = self.org.get_repo(repo_name)
        pr = repo.get_pull(pr_number)
        try:
            pr.create_review_request(reviewers=reviewers)
            logger.info("Added reviewers to PR #%d: %s", pr_number, reviewers)
        except GithubException as e:
            logger.warning(
                "Failed to add reviewers to PR #%d: %s", pr_number, e
            )

    def get_clone_url(self, repo_name):
        """Get the HTTPS clone URL for a repo."""
        repo = self.org.get_repo(repo_name)
        return repo.clone_url
