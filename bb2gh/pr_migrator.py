"""Migrate pull requests from Bitbucket Server to GitHub Enterprise."""

import logging

from .bitbucket_client import BitbucketClient
from .github_client import GithubClient
from .state import State

logger = logging.getLogger(__name__)


def _format_pr_body(pr, config):
    """Format the GitHub PR body with migration metadata."""
    bb_url = config.bb_base_url
    project = pr["toRef"]["repository"]["project"]["key"]
    repo = pr["toRef"]["repository"]["slug"]
    pr_id = pr["id"]
    author = pr["author"]["user"].get("displayName", pr["author"]["user"].get("name", "Unknown"))
    created = pr.get("createdDate", "")

    # Build metadata header
    header = (
        f"> **Migrated from Bitbucket**\n"
        f"> Source: [{project}/{repo} PR #{pr_id}]"
        f"({bb_url}/projects/{project}/repos/{repo}/pull-requests/{pr_id})\n"
        f"> Original author: **{author}**\n"
    )
    if created:
        header += f"> Created: {created}\n"

    description = pr.get("description", "") or ""
    return f"{header}\n---\n\n{description}"


def _format_comment(activity, config):
    """Format a Bitbucket comment as a GitHub PR comment."""
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


def migrate_pull_requests(config, dry_run=False):
    """Migrate open pull requests from Bitbucket to GitHub.

    Args:
        config: Config object.
        dry_run: If True, log what would be done without making changes.
    """
    bb = BitbucketClient(config.bb_base_url, config.bb_token)
    gh = GithubClient(config.gh_base_url, config.gh_token, config.gh_org)
    state = State(config.work_dir)

    migrated_repos = state.get_migrated_repos()
    if not migrated_repos:
        logger.warning("No migrated repos found. Run 'bb2gh migrate' first.")
        return

    total_migrated = 0
    total_skipped = 0
    total_failed = 0

    for project_key, repo_slug in migrated_repos:
        logger.info("Processing PRs for %s/%s", project_key, repo_slug)

        try:
            open_prs = bb.list_pull_requests(project_key, repo_slug, state="OPEN")
        except Exception:
            logger.exception("Failed to list PRs for %s/%s", project_key, repo_slug)
            continue

        for pr in open_prs:
            pr_id = pr["id"]
            title = pr["title"]

            if state.is_pr_migrated(project_key, repo_slug, pr_id):
                logger.info("Skipping already migrated PR #%d: %s", pr_id, title)
                total_skipped += 1
                continue

            head_branch = pr["fromRef"]["displayId"]
            base_branch = pr["toRef"]["displayId"]

            if dry_run:
                logger.info(
                    "[DRY RUN] Would migrate PR #%d: %s (%s -> %s)",
                    pr_id, title, head_branch, base_branch,
                )
                total_migrated += 1
                continue

            try:
                _migrate_single_pr(
                    config, bb, gh, state, project_key, repo_slug, pr
                )
                total_migrated += 1
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


def _migrate_single_pr(config, bb, gh, state, project_key, repo_slug, pr):
    """Migrate a single pull request."""
    pr_id = pr["id"]
    title = pr["title"]
    head_branch = pr["fromRef"]["displayId"]
    base_branch = pr["toRef"]["displayId"]

    logger.info("Migrating PR #%d: %s (%s -> %s)", pr_id, title, head_branch, base_branch)

    # Create PR on GitHub
    body = _format_pr_body(pr, config)
    gh_pr = gh.create_pull_request(
        repo_name=repo_slug,
        title=title,
        body=body,
        head=head_branch,
        base=base_branch,
    )

    # Migrate comments
    activities = bb.get_pr_activities(project_key, repo_slug, pr_id)
    comment_count = 0
    for activity in activities:
        action = activity.get("action", "")
        if action == "COMMENTED" and "comment" in activity:
            comment_body = _format_comment(activity, config)
            gh.add_pr_comment(repo_slug, gh_pr.number, comment_body)
            comment_count += 1

    # Assign reviewers (best effort)
    reviewers = _map_reviewers(pr, config)
    if reviewers:
        gh.add_pr_reviewers(repo_slug, gh_pr.number, reviewers)

    # Record mapping
    state.record_pr_mapping(project_key, repo_slug, pr_id, gh_pr.number)

    logger.info(
        "Migrated PR #%d -> GitHub PR #%d (%d comments, %d reviewers)",
        pr_id, gh_pr.number, comment_count, len(reviewers),
    )
