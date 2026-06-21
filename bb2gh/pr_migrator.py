"""Migrate pull requests from Bitbucket Server to GitHub Enterprise."""

import logging
import os
import subprocess

from .bitbucket_client import BitbucketClient
from .github_client import GithubClient
from .state import State

logger = logging.getLogger(__name__)


def _run_git(args, cwd=None, quiet=False):
    cmd = ["git"] + args
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        if not quiet:
            logger.error("git %s failed: %s", args[0], result.stderr.strip())
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )
    return result.stdout.strip()


def _format_pr_body(pr, config, closed_state=None):
    """Format the GitHub PR body with migration metadata."""
    bb_url = config.bb_base_url
    project = pr["toRef"]["repository"]["project"]["key"]
    repo = pr["toRef"]["repository"]["slug"]
    pr_id = pr["id"]
    author = pr["author"]["user"].get("displayName", pr["author"]["user"].get("name", "Unknown"))
    created = pr.get("createdDate", "")

    header = (
        f"> **Migrated from Bitbucket**\n"
        f"> Source: [{project}/{repo} PR #{pr_id}]"
        f"({bb_url}/projects/{project}/repos/{repo}/pull-requests/{pr_id})\n"
        f"> Original author: **{author}**\n"
    )
    if created:
        header += f"> Created: {created}\n"
    if closed_state:
        header += f"> Original status: **{closed_state}**\n"

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


def _commit_exists(bare_path, sha):
    """Check if a commit SHA exists in the bare repo."""
    try:
        _run_git(["cat-file", "-t", sha], cwd=bare_path, quiet=True)
        return True
    except subprocess.CalledProcessError:
        return False


def _create_temp_branch(bare_path, branch_name, sha):
    """Create a branch pointing to a specific commit in the bare repo."""
    try:
        _run_git(["branch", branch_name, sha], cwd=bare_path, quiet=True)
        return True
    except subprocess.CalledProcessError:
        return False


def _push_temp_branch(bare_path, branch_name):
    """Push a branch to the github remote."""
    try:
        _run_git(["push", "github", f"{branch_name}:{branch_name}"], cwd=bare_path, quiet=True)
        return True
    except subprocess.CalledProcessError:
        return False


def _delete_temp_branch(bare_path, branch_name):
    """Delete a local branch from the bare repo."""
    try:
        _run_git(["branch", "-D", branch_name], cwd=bare_path, quiet=True)
    except subprocess.CalledProcessError:
        pass


def migrate_pull_requests(config, dry_run=False, include_closed=False, only_repos=None):
    """Migrate pull requests from Bitbucket to GitHub.

    Args:
        config: Config object.
        dry_run: If True, log what would be done without making changes.
        include_closed: If True, also migrate merged/declined PRs.
        only_repos: Optional set of "PROJECT/SLUG" strings to filter repos.
    """
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

        # Fetch open PRs
        try:
            prs = bb.list_pull_requests(project_key, repo_slug, state="OPEN")
        except Exception:
            logger.exception("Failed to list PRs for %s/%s", project_key, repo_slug)
            continue

        # Fetch closed PRs if requested
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

            head_branch = pr["fromRef"]["displayId"]
            base_branch = pr["toRef"]["displayId"]

            if dry_run:
                logger.info(
                    "[DRY RUN] Would migrate PR #%d [%s]: %s (%s -> %s) to %s/%s",
                    pr_id, pr_state, title, head_branch, base_branch, gh_org, gh_repo_name,
                )
                total_migrated += 1
                continue

            try:
                if pr_state == "OPEN":
                    _migrate_open_pr(
                        config, bb, gh, state, project_key, repo_slug,
                        gh_org, gh_repo_name, pr,
                    )
                else:
                    _migrate_closed_pr(
                        config, bb, gh, state, project_key, repo_slug,
                        gh_org, gh_repo_name, pr,
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


def _migrate_open_pr(config, bb, gh, state, project_key, repo_slug, gh_org, gh_repo_name, pr):
    """Migrate an open pull request."""
    pr_id = pr["id"]
    title = pr["title"]
    head_branch = pr["fromRef"]["displayId"]
    base_branch = pr["toRef"]["displayId"]

    logger.info("Migrating open PR #%d: %s (%s -> %s)", pr_id, title, head_branch, base_branch)

    body = _format_pr_body(pr, config)
    gh_pr = gh.create_pull_request(
        repo_name=gh_repo_name, title=title, body=body,
        head=head_branch, base=base_branch, org_name=gh_org,
    )

    _migrate_pr_comments(bb, gh, config, project_key, repo_slug, gh_org, gh_repo_name, pr_id, gh_pr.number)

    reviewers = _map_reviewers(pr, config)
    if reviewers:
        gh.add_pr_reviewers(gh_repo_name, gh_pr.number, reviewers, org_name=gh_org)

    state.record_pr_mapping(project_key, repo_slug, pr_id, gh_pr.number)
    logger.info("Migrated open PR #%d -> GitHub PR #%d", pr_id, gh_pr.number)


def _resolve_head_sha(bb, bare_path, project_key, repo_slug, pr):
    """Find a usable commit SHA for recreating a closed PR's head branch.

    Tries in order:
    1. fromRef.latestCommit — the original branch tip (works for regular merges)
    2. merge/squash commit from PR properties — guaranteed to exist for merged PRs
    3. merge commit from PR activities — alternative source for the same info

    Returns (sha, is_merge_commit) or (None, False).
    """
    pr_id = pr["id"]
    head_sha = pr["fromRef"].get("latestCommit", "")

    # 1. Original source branch tip
    if head_sha and _commit_exists(bare_path, head_sha):
        logger.debug("PR #%d: using fromRef.latestCommit %s", pr_id, head_sha[:12])
        return head_sha, False

    # 2. Merge/squash commit from PR properties
    merge_sha = bb.get_merge_commit(project_key, repo_slug, pr_id)
    if merge_sha and _commit_exists(bare_path, merge_sha):
        logger.debug("PR #%d: using merge commit %s from properties", pr_id, merge_sha[:12])
        return merge_sha, True

    # 3. Merge commit from activities
    try:
        activities = bb.get_pr_activities(project_key, repo_slug, pr_id)
        for activity in activities:
            if activity.get("action") == "MERGED":
                commit = activity.get("commit", {})
                act_sha = commit.get("id") or commit.get("displayId")
                if act_sha and _commit_exists(bare_path, act_sha):
                    logger.debug("PR #%d: using merge commit %s from activity", pr_id, act_sha[:12])
                    return act_sha, True
    except Exception:
        logger.debug("PR #%d: could not fetch activities for merge commit", pr_id)

    return None, False


def _migrate_closed_pr(config, bb, gh, state, project_key, repo_slug, gh_org, gh_repo_name, pr):
    """Migrate a closed (merged/declined) PR by recreating the branch from the commit SHA."""
    pr_id = pr["id"]
    title = pr["title"]
    pr_state = pr.get("state", "UNKNOWN")
    head_branch = pr["fromRef"]["displayId"]
    base_branch = pr["toRef"]["displayId"]

    logger.info("Migrating %s PR #%d: %s (%s -> %s)", pr_state, pr_id, title, head_branch, base_branch)

    bare_path = os.path.join(config.work_dir, f"{project_key}__{repo_slug}.git")

    temp_branch = f"migrated-pr/{pr_id}/{head_branch}"
    branch_created = False
    pr_base = base_branch

    if os.path.exists(bare_path):
        head_sha, is_merge_commit = _resolve_head_sha(
            bb, bare_path, project_key, repo_slug, pr,
        )

        if head_sha:
            if is_merge_commit:
                # The merge/squash commit is already on the target branch.
                # To get a meaningful diff, target the PR at the commit's parent.
                try:
                    parent_sha = _run_git(
                        ["rev-parse", f"{head_sha}^"], cwd=bare_path, quiet=True,
                    )
                    # Create a temp base branch at the parent so the PR shows the squash diff
                    pr_base = f"migrated-pr/{pr_id}/base"
                    if _create_temp_branch(bare_path, pr_base, parent_sha):
                        _push_temp_branch(bare_path, pr_base)
                        _delete_temp_branch(bare_path, pr_base)
                except subprocess.CalledProcessError:
                    logger.debug("PR #%d: could not resolve parent of merge commit", pr_id)

            if _create_temp_branch(bare_path, temp_branch, head_sha):
                if _push_temp_branch(bare_path, temp_branch):
                    branch_created = True
                _delete_temp_branch(bare_path, temp_branch)

    if branch_created:
        # Create a real PR on GitHub, then close it
        body = _format_pr_body(pr, config, closed_state=pr_state)
        try:
            gh_pr = gh.create_pull_request(
                repo_name=gh_repo_name,
                title=f"[{pr_state}] {title}",
                body=body,
                head=temp_branch,
                base=base_branch,
                org_name=gh_org,
            )

            _migrate_pr_comments(bb, gh, config, project_key, repo_slug,
                                 gh_org, gh_repo_name, pr_id, gh_pr.number)

            # Close the PR with a status comment
            gh.add_pr_comment(
                gh_repo_name, gh_pr.number,
                f"This PR was **{pr_state.lower()}** on Bitbucket. "
                f"Migrated for historical reference.",
                org_name=gh_org,
            )

            # Close the PR
            repo = gh.get_repo(gh_repo_name, org_name=gh_org)
            gh_pull = repo.get_pull(gh_pr.number)
            gh_pull.edit(state="closed")

            state.record_pr_mapping(project_key, repo_slug, pr_id, gh_pr.number)
            logger.info("Migrated %s PR #%d -> GitHub PR #%d (closed)", pr_state, pr_id, gh_pr.number)
            return

        except Exception:
            logger.warning("Could not create PR for %s PR #%d, falling back to issue", pr_state, pr_id)

    # Fallback: create as a GitHub Issue
    _migrate_pr_as_issue(config, bb, gh, state, project_key, repo_slug,
                         gh_org, gh_repo_name, pr)


def _migrate_pr_as_issue(config, bb, gh, state, project_key, repo_slug,
                         gh_org, gh_repo_name, pr):
    """Migrate a PR as a GitHub Issue (when branch can't be recreated)."""
    pr_id = pr["id"]
    title = pr["title"]
    pr_state = pr.get("state", "UNKNOWN")

    body = _format_pr_body(pr, config, closed_state=pr_state)
    body += f"\n\n---\n*Migrated as issue because the source branch could not be recreated.*"

    repo = gh.get_repo(gh_repo_name, org_name=gh_org)

    # Create issue
    issue = repo.create_issue(
        title=f"[Migrated {pr_state} PR #{pr_id}] {title}",
        body=body,
        labels=["migrated-pr", pr_state.lower()],
    )

    # Migrate comments
    activities = bb.get_pr_activities(project_key, repo_slug, pr_id)
    for activity in activities:
        action = activity.get("action", "")
        if action == "COMMENTED" and "comment" in activity:
            comment_body = _format_comment(activity, config)
            issue.create_comment(comment_body)

    # Close the issue
    issue.edit(state="closed")

    state.record_pr_mapping(project_key, repo_slug, pr_id, issue.number)
    logger.info("Migrated %s PR #%d -> GitHub Issue #%d (closed)", pr_state, pr_id, issue.number)


def _migrate_pr_comments(bb, gh, config, project_key, repo_slug,
                         gh_org, gh_repo_name, pr_id, gh_pr_number):
    """Migrate comments from a Bitbucket PR to a GitHub PR."""
    activities = bb.get_pr_activities(project_key, repo_slug, pr_id)
    comment_count = 0
    for activity in activities:
        action = activity.get("action", "")
        if action == "COMMENTED" and "comment" in activity:
            comment_body = _format_comment(activity, config)
            gh.add_pr_comment(gh_repo_name, gh_pr_number, comment_body, org_name=gh_org)
            comment_count += 1
    return comment_count
