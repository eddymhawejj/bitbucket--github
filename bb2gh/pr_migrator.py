"""Migrate pull requests from Bitbucket Server to GitHub Enterprise.

Open PRs are migrated as GitHub PRs (they have a live branch).
Closed PRs (MERGED, DECLINED) are migrated as closed GitHub Issues with
all their comments preserved. Everything lives in one place — the issue
tracker — so PR history is searchable alongside code review discussions.
"""

import logging
import random
import time

from github import GithubException

from .bitbucket_client import BitbucketClient
from .github_client import GithubClient
from .state import State

logger = logging.getLogger(__name__)


class Throttler:
    """Rate-limits API calls with a fixed delay and retry-on-rate-limit backoff."""

    def __init__(self, api_delay=0.5, pr_delay=3.0, max_retries=5):
        self.api_delay = api_delay
        self.pr_delay = pr_delay
        self.max_retries = max_retries
        self._last_call = 0.0

    def wait_api(self):
        if self.api_delay <= 0:
            return
        elapsed = time.monotonic() - self._last_call
        remaining = self.api_delay - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_call = time.monotonic()

    def wait_between_prs(self):
        if self.pr_delay > 0:
            time.sleep(self.pr_delay)

    def call(self, fn, *args, **kwargs):
        for attempt in range(self.max_retries + 1):
            self.wait_api()
            try:
                return fn(*args, **kwargs)
            except GithubException as e:
                if not self._is_rate_limited(e) or attempt >= self.max_retries:
                    raise
                delay = self._retry_delay(e, attempt)
                logger.warning(
                    "GitHub rate limit hit (status=%s), sleeping %.1fs (attempt %d/%d)",
                    e.status, delay, attempt + 1, self.max_retries,
                )
                time.sleep(delay)

    @staticmethod
    def _is_rate_limited(e):
        if e.status == 429:
            return True
        if e.status == 403:
            msg = str(e).lower()
            return "rate limit" in msg or "abuse" in msg or "secondary" in msg
        return False

    @staticmethod
    def _retry_delay(e, attempt):
        headers = getattr(e, "headers", {}) or {}
        retry_after = headers.get("Retry-After") or headers.get("retry-after")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return min(60.0, (2 ** attempt) + random.random())


def _format_pr_body(pr, config, closed_state=None):
    """Format the GitHub PR/issue body with migration metadata."""
    bb_url = config.bb_base_url
    project = pr["toRef"]["repository"]["project"]["key"]
    repo = pr["toRef"]["repository"]["slug"]
    pr_id = pr["id"]
    author = pr["author"]["user"].get("displayName", pr["author"]["user"].get("name", "Unknown"))
    created = pr.get("createdDate", "")
    head_branch = pr["fromRef"]["displayId"]
    base_branch = pr["toRef"]["displayId"]

    header = (
        f"> **Migrated from Bitbucket**\n"
        f"> Source: [{project}/{repo} PR #{pr_id}]"
        f"({bb_url}/projects/{project}/repos/{repo}/pull-requests/{pr_id})\n"
        f"> Original author: **{author}**\n"
        f"> Branch: `{head_branch}` → `{base_branch}`\n"
    )
    if created:
        header += f"> Created: {created}\n"
    if closed_state:
        header += f"> Original status: **{closed_state}**\n"

    description = pr.get("description", "") or ""
    return f"{header}\n---\n\n{description}"


def _format_comment(activity, config):
    """Format a Bitbucket comment for GitHub."""
    comment = activity.get("comment", {})
    user = comment.get("author", {})
    display_name = user.get("displayName", user.get("name", "Unknown"))
    text = comment.get("text", "")
    created = comment.get("createdDate", "")

    header = f"**{display_name}** commented"
    if created:
        header += f" (originally at {created})"
    header += ":"

    return f"{header}\n\n{text}"


def _map_reviewers(pr, config):
    """Map Bitbucket reviewer usernames to GitHub usernames."""
    reviewers = []
    for reviewer in pr.get("reviewers", []):
        bb_username = reviewer["user"].get("name", "")
        gh_username = config.user_mapping.get(bb_username, bb_username)
        if gh_username:
            reviewers.append(gh_username)
    return reviewers


def _iter_comment_activities(activities):
    """Yield COMMENTED activities in chronological order (oldest first)."""
    comments = [a for a in activities if a.get("action") == "COMMENTED" and "comment" in a]
    comments.sort(key=lambda a: a.get("comment", {}).get("createdDate") or a.get("createdDate", 0))
    return comments


def migrate_pull_requests(
    config, dry_run=False, include_closed=False, closed_only=False,
    only_repos=None, throttler=None,
):
    """Migrate pull requests from Bitbucket to GitHub.

    Args:
        config: Config object.
        dry_run: If True, log what would be done without making changes.
        include_closed: If True, also migrate merged/declined PRs (as issues).
        closed_only: If True, migrate ONLY merged/declined PRs. Implies include_closed.
        only_repos: Optional set of "PROJECT/SLUG" strings to filter repos.
        throttler: Optional Throttler instance. Defaults are read from config.
    """
    if closed_only:
        include_closed = True

    if throttler is None:
        throttler = Throttler(
            api_delay=getattr(config, "pr_api_delay", 0.5),
            pr_delay=getattr(config, "pr_pr_delay", 3.0),
            max_retries=getattr(config, "pr_max_retries", 5),
        )

    bb = BitbucketClient(config.bb_base_url, config.bb_token, verify_ssl=config.bb_verify_ssl)
    gh = GithubClient(config.gh_base_url, config.gh_token, config.gh_org)
    state = State(config.work_dir)

    migrated_repos = state.get_migrated_repos()
    if not migrated_repos:
        logger.warning("No migrated repos found. Run 'bb2gh migrate' first.")
        return 0, 0, 0

    total_migrated = 0
    total_skipped = 0
    total_failed = 0

    for project_key, repo_slug in migrated_repos:
        repo_key = f"{project_key}/{repo_slug}"
        if only_repos and repo_key not in only_repos:
            continue

        gh_org, gh_repo_name = state.get_github_target(project_key, repo_slug)
        if not gh_org or not gh_repo_name:
            gh_org, gh_repo_name = config.resolve_target(project_key, repo_slug)

        logger.info("Processing PRs for %s/%s -> %s/%s", project_key, repo_slug, gh_org, gh_repo_name)

        prs = []

        if not closed_only:
            try:
                prs = bb.list_pull_requests(project_key, repo_slug, state="OPEN")
            except Exception:
                logger.exception("Failed to list open PRs for %s/%s", project_key, repo_slug)
                continue

        if include_closed:
            try:
                merged = bb.list_pull_requests(project_key, repo_slug, state="MERGED")
                declined = bb.list_pull_requests(project_key, repo_slug, state="DECLINED")
                prs.extend(merged)
                prs.extend(declined)
            except Exception:
                logger.exception("Failed to list closed PRs for %s/%s", project_key, repo_slug)

        for pr in prs:
            pr_id = pr["id"]
            title = pr["title"]
            pr_state = pr.get("state", "OPEN")

            if state.is_pr_migrated(project_key, repo_slug, pr_id):
                logger.info("Skipping already migrated PR #%d: %s", pr_id, title)
                total_skipped += 1
                continue

            if dry_run:
                target = "PR" if pr_state == "OPEN" else "closed Issue"
                logger.info(
                    "[DRY RUN] Would migrate PR #%d [%s] as %s: %s -> %s/%s",
                    pr_id, pr_state, target, title, gh_org, gh_repo_name,
                )
                total_migrated += 1
                continue

            try:
                if pr_state == "OPEN":
                    _migrate_open_pr(
                        config, bb, gh, state, project_key, repo_slug,
                        gh_org, gh_repo_name, pr, throttler,
                    )
                else:
                    _migrate_closed_pr_as_issue(
                        config, bb, gh, state, project_key, repo_slug,
                        gh_org, gh_repo_name, pr, throttler,
                    )
                total_migrated += 1
                throttler.wait_between_prs()
            except Exception:
                logger.exception(
                    "Failed to migrate PR #%d in %s/%s", pr_id, project_key, repo_slug
                )
                total_failed += 1

    logger.info(
        "PR migration complete: %d migrated, %d skipped, %d failed",
        total_migrated, total_skipped, total_failed,
    )
    return total_migrated, total_skipped, total_failed


def _migrate_open_pr(config, bb, gh, state, project_key, repo_slug,
                     gh_org, gh_repo_name, pr, throttler):
    """Migrate an open PR as a GitHub PR."""
    pr_id = pr["id"]
    title = pr["title"]
    head_branch = pr["fromRef"]["displayId"]
    base_branch = pr["toRef"]["displayId"]

    logger.info("Migrating open PR #%d: %s (%s -> %s)", pr_id, title, head_branch, base_branch)

    body = _format_pr_body(pr, config)
    gh_pr = throttler.call(
        gh.create_pull_request,
        repo_name=gh_repo_name, title=title, body=body,
        head=head_branch, base=base_branch, org_name=gh_org,
    )

    activities = bb.get_pr_activities(project_key, repo_slug, pr_id)
    for activity in _iter_comment_activities(activities):
        comment_body = _format_comment(activity, config)
        throttler.call(
            gh.add_pr_comment, gh_repo_name, gh_pr.number, comment_body, org_name=gh_org,
        )

    reviewers = _map_reviewers(pr, config)
    if reviewers:
        throttler.call(
            gh.add_pr_reviewers, gh_repo_name, gh_pr.number, reviewers, org_name=gh_org,
        )

    state.record_pr_mapping(project_key, repo_slug, pr_id, gh_pr.number)
    logger.info("Migrated open PR #%d -> GitHub PR #%d", pr_id, gh_pr.number)


def _migrate_closed_pr_as_issue(config, bb, gh, state, project_key, repo_slug,
                                gh_org, gh_repo_name, pr, throttler):
    """Migrate a closed (merged/declined) PR as a closed GitHub Issue.

    Everything lives in one place — the issue tracker — so PR history is
    searchable alongside other issues. No branch recreation, no repo access
    needed, works even when the source branch has been GC'd.
    """
    pr_id = pr["id"]
    title = pr["title"]
    pr_state = pr.get("state", "UNKNOWN")

    logger.info("Migrating %s PR #%d as issue: %s", pr_state, pr_id, title)

    body = _format_pr_body(pr, config, closed_state=pr_state)

    repo = throttler.call(gh.get_repo, gh_repo_name, org_name=gh_org)

    labels = ["migrated-pr", pr_state.lower()]
    try:
        issue = throttler.call(
            repo.create_issue,
            title=f"[{pr_state} PR #{pr_id}] {title}",
            body=body,
            labels=labels,
        )
    except GithubException as e:
        # Labels don't exist yet — create without labels
        if e.status in (404, 422):
            logger.debug("Labels not found on %s, creating issue without labels", gh_repo_name)
            issue = throttler.call(
                repo.create_issue,
                title=f"[{pr_state} PR #{pr_id}] {title}",
                body=body,
            )
        else:
            raise

    activities = bb.get_pr_activities(project_key, repo_slug, pr_id)
    for activity in _iter_comment_activities(activities):
        comment_body = _format_comment(activity, config)
        throttler.call(issue.create_comment, comment_body)

    throttler.call(issue.edit, state="closed")

    state.record_pr_mapping(project_key, repo_slug, pr_id, issue.number)
    logger.info("Migrated %s PR #%d -> GitHub Issue #%d (closed)", pr_state, pr_id, issue.number)
