"""Tests for the PR migrator module."""

from unittest.mock import MagicMock, patch

import pytest

from bb2gh.config import Config
from bb2gh.pr_migrator import (
    migrate_pull_requests,
    _format_pr_body,
    _format_comment,
    _map_reviewers,
    _resolve_head_sha,
)


@pytest.fixture
def mock_config():
    config = MagicMock(spec=Config)
    config.bb_base_url = "https://bitbucket.example.com"
    config.bb_token = "fake-token"
    config.bb_verify_ssl = True
    config.gh_base_url = "https://github.example.com/api/v3"
    config.gh_token = "fake-gh-token"
    config.gh_org = "my-org"
    config.work_dir = "/tmp/test"
    config.user_mapping = {"john.doe": "johndoe"}
    config.resolve_target = MagicMock(return_value=("my-org", "my-repo"))
    return config


@pytest.fixture
def sample_pr():
    return {
        "id": 42,
        "title": "Fix login bug",
        "description": "This fixes the login timeout issue.",
        "author": {
            "user": {"name": "john.doe", "displayName": "John Doe"}
        },
        "fromRef": {
            "displayId": "fix/login-bug",
            "repository": {"slug": "my-repo", "project": {"key": "PROJ"}},
        },
        "toRef": {
            "displayId": "main",
            "repository": {"slug": "my-repo", "project": {"key": "PROJ"}},
        },
        "reviewers": [
            {"user": {"name": "john.doe", "displayName": "John Doe"}},
            {"user": {"name": "jane.smith", "displayName": "Jane Smith"}},
        ],
        "createdDate": 1711234567000,
    }


class TestFormatPrBody:
    def test_includes_metadata(self, sample_pr, mock_config):
        body = _format_pr_body(sample_pr, mock_config)

        assert "Migrated from Bitbucket" in body
        assert "John Doe" in body
        assert "PROJ" in body
        assert "my-repo" in body
        assert "42" in body
        assert "This fixes the login timeout issue." in body

    def test_handles_empty_description(self, sample_pr, mock_config):
        sample_pr["description"] = None
        body = _format_pr_body(sample_pr, mock_config)

        assert "Migrated from Bitbucket" in body


class TestFormatComment:
    def test_formats_comment(self, mock_config):
        activity = {
            "action": "COMMENTED",
            "comment": {
                "author": {"name": "jane", "displayName": "Jane Smith"},
                "text": "Looks good to me!",
                "createdDate": 1711234567000,
            },
        }

        result = _format_comment(activity, mock_config)

        assert "Jane Smith" in result
        assert "Looks good to me!" in result


class TestMapReviewers:
    def test_maps_known_users(self, sample_pr, mock_config):
        reviewers = _map_reviewers(sample_pr, mock_config)

        assert "johndoe" in reviewers  # mapped
        assert "jane.smith" in reviewers  # passthrough (no mapping)

    def test_empty_reviewers(self, mock_config):
        pr = {"reviewers": []}
        assert _map_reviewers(pr, mock_config) == []


class TestMigratePullRequests:
    @patch("bb2gh.pr_migrator.State")
    @patch("bb2gh.pr_migrator.GithubClient")
    @patch("bb2gh.pr_migrator.BitbucketClient")
    def test_dry_run(self, MockBB, MockGH, MockState, mock_config, sample_pr):
        state_instance = MockState.return_value
        state_instance.get_migrated_repos.return_value = [("PROJ", "my-repo")]
        state_instance.get_github_target.return_value = ("my-org", "my-repo")
        state_instance.is_pr_migrated.return_value = False

        bb_instance = MockBB.return_value
        bb_instance.list_pull_requests.return_value = [sample_pr]

        migrated, skipped, failed = migrate_pull_requests(mock_config, dry_run=True)

        assert migrated == 1
        assert skipped == 0
        assert failed == 0

        # GitHub should NOT have been called in dry run
        MockGH.return_value.create_pull_request.assert_not_called()

    @patch("bb2gh.pr_migrator.State")
    @patch("bb2gh.pr_migrator.GithubClient")
    @patch("bb2gh.pr_migrator.BitbucketClient")
    def test_skips_already_migrated(self, MockBB, MockGH, MockState, mock_config, sample_pr):
        state_instance = MockState.return_value
        state_instance.get_migrated_repos.return_value = [("PROJ", "my-repo")]
        state_instance.get_github_target.return_value = ("my-org", "my-repo")
        state_instance.is_pr_migrated.return_value = True

        bb_instance = MockBB.return_value
        bb_instance.list_pull_requests.return_value = [sample_pr]

        migrated, skipped, failed = migrate_pull_requests(mock_config, dry_run=False)

        assert migrated == 0
        assert skipped == 1

    @patch("bb2gh.pr_migrator.State")
    @patch("bb2gh.pr_migrator.GithubClient")
    @patch("bb2gh.pr_migrator.BitbucketClient")
    def test_migrates_pr_with_comments(self, MockBB, MockGH, MockState, mock_config, sample_pr):
        state_instance = MockState.return_value
        state_instance.get_migrated_repos.return_value = [("PROJ", "my-repo")]
        state_instance.get_github_target.return_value = ("my-org", "my-repo")
        state_instance.is_pr_migrated.return_value = False

        bb_instance = MockBB.return_value
        bb_instance.list_pull_requests.return_value = [sample_pr]
        bb_instance.get_pr_activities.return_value = [
            {
                "action": "COMMENTED",
                "comment": {
                    "author": {"name": "reviewer", "displayName": "Reviewer"},
                    "text": "LGTM",
                    "createdDate": 1711234567000,
                },
            },
            {"action": "APPROVED"},  # Non-comment activity, should be skipped
        ]

        mock_pr = MagicMock()
        mock_pr.number = 99
        MockGH.return_value.create_pull_request.return_value = mock_pr

        migrated, skipped, failed = migrate_pull_requests(mock_config, dry_run=False)

        assert migrated == 1
        assert failed == 0

        # Verify PR was created with correct org
        MockGH.return_value.create_pull_request.assert_called_once()
        call_kwargs = MockGH.return_value.create_pull_request.call_args
        assert call_kwargs[1]["org_name"] == "my-org"
        assert call_kwargs[1]["repo_name"] == "my-repo"

        # Verify comment was added (only 1 — the APPROVED activity is skipped)
        MockGH.return_value.add_pr_comment.assert_called_once()

        # Verify state recorded
        state_instance.record_pr_mapping.assert_called_once_with("PROJ", "my-repo", 42, 99)

    @patch("bb2gh.pr_migrator.State")
    @patch("bb2gh.pr_migrator.GithubClient")
    @patch("bb2gh.pr_migrator.BitbucketClient")
    def test_uses_mapped_org_for_pr(self, MockBB, MockGH, MockState, mock_config, sample_pr):
        """Test that PRs are created in the correct mapped org."""
        state_instance = MockState.return_value
        state_instance.get_migrated_repos.return_value = [("PROJ", "my-repo")]
        # Simulate a repo migrated to a different org
        state_instance.get_github_target.return_value = ("infra-team", "infra-my-repo")
        state_instance.is_pr_migrated.return_value = False

        bb_instance = MockBB.return_value
        bb_instance.list_pull_requests.return_value = [sample_pr]
        bb_instance.get_pr_activities.return_value = []

        mock_pr = MagicMock()
        mock_pr.number = 5
        MockGH.return_value.create_pull_request.return_value = mock_pr

        migrated, skipped, failed = migrate_pull_requests(mock_config, dry_run=False)

        assert migrated == 1

        # Verify PR was created in the mapped org with the mapped repo name
        call_kwargs = MockGH.return_value.create_pull_request.call_args
        assert call_kwargs[1]["org_name"] == "infra-team"
        assert call_kwargs[1]["repo_name"] == "infra-my-repo"

    @patch("bb2gh.pr_migrator.State")
    @patch("bb2gh.pr_migrator.GithubClient")
    @patch("bb2gh.pr_migrator.BitbucketClient")
    def test_fallback_to_config_resolve(self, MockBB, MockGH, MockState, mock_config, sample_pr):
        """Test fallback to config.resolve_target when state has no GitHub target."""
        state_instance = MockState.return_value
        state_instance.get_migrated_repos.return_value = [("PROJ", "my-repo")]
        # Simulate old state without gh_org/gh_repo_name
        state_instance.get_github_target.return_value = (None, None)
        state_instance.is_pr_migrated.return_value = False

        mock_config.resolve_target.return_value = ("fallback-org", "fallback-repo")

        bb_instance = MockBB.return_value
        bb_instance.list_pull_requests.return_value = [sample_pr]
        bb_instance.get_pr_activities.return_value = []

        mock_pr = MagicMock()
        mock_pr.number = 10
        MockGH.return_value.create_pull_request.return_value = mock_pr

        migrated, _, _ = migrate_pull_requests(mock_config, dry_run=False)

        assert migrated == 1
        mock_config.resolve_target.assert_called_once_with("PROJ", "my-repo")
        call_kwargs = MockGH.return_value.create_pull_request.call_args
        assert call_kwargs[1]["org_name"] == "fallback-org"
        assert call_kwargs[1]["repo_name"] == "fallback-repo"


class TestResolveHeadSha:
    """Tests for _resolve_head_sha — the SHA resolution cascade for closed PRs."""

    def _make_pr(self, head_sha="abc123"):
        return {
            "id": 10,
            "fromRef": {"displayId": "feature/x", "latestCommit": head_sha},
            "toRef": {"displayId": "main"},
        }

    @patch("bb2gh.pr_migrator._commit_exists")
    def test_uses_from_ref_when_commit_exists(self, mock_exists):
        mock_exists.return_value = True
        bb = MagicMock()

        sha, is_merge = _resolve_head_sha(bb, "/repo.git", "PROJ", "repo", self._make_pr("deadbeef"))

        assert sha == "deadbeef"
        assert is_merge is False
        bb.get_merge_commit.assert_not_called()

    @patch("bb2gh.pr_migrator._commit_exists")
    def test_falls_back_to_merge_commit_property(self, mock_exists):
        mock_exists.side_effect = lambda path, sha: sha == "squash111"
        bb = MagicMock()
        bb.get_merge_commit.return_value = "squash111"

        sha, is_merge = _resolve_head_sha(bb, "/repo.git", "PROJ", "repo", self._make_pr("gone"))

        assert sha == "squash111"
        assert is_merge is True

    @patch("bb2gh.pr_migrator._commit_exists")
    def test_falls_back_to_merge_activity(self, mock_exists):
        mock_exists.side_effect = lambda path, sha: sha == "act222"
        bb = MagicMock()
        bb.get_merge_commit.return_value = None
        bb.get_pr_activities.return_value = [
            {"action": "COMMENTED", "comment": {"text": "hi"}},
            {"action": "MERGED", "commit": {"id": "act222", "displayId": "act222"}},
        ]

        sha, is_merge = _resolve_head_sha(bb, "/repo.git", "PROJ", "repo", self._make_pr("gone"))

        assert sha == "act222"
        assert is_merge is True

    @patch("bb2gh.pr_migrator._commit_exists")
    def test_returns_none_when_nothing_found(self, mock_exists):
        mock_exists.return_value = False
        bb = MagicMock()
        bb.get_merge_commit.return_value = None
        bb.get_pr_activities.return_value = []

        sha, is_merge = _resolve_head_sha(bb, "/repo.git", "PROJ", "repo", self._make_pr("gone"))

        assert sha is None
        assert is_merge is False

    @patch("bb2gh.pr_migrator._commit_exists")
    def test_handles_empty_latest_commit(self, mock_exists):
        mock_exists.return_value = False
        bb = MagicMock()
        bb.get_merge_commit.return_value = None
        bb.get_pr_activities.return_value = []

        sha, is_merge = _resolve_head_sha(bb, "/repo.git", "PROJ", "repo", self._make_pr(""))

        assert sha is None
        assert is_merge is False


class TestFormatPrBodyClosed:
    def test_includes_closed_state(self, sample_pr, mock_config):
        body = _format_pr_body(sample_pr, mock_config, closed_state="MERGED")
        assert "MERGED" in body
        assert "Original status" in body

    def test_no_status_for_open(self, sample_pr, mock_config):
        body = _format_pr_body(sample_pr, mock_config)
        assert "Original status" not in body


class TestMigrateClosedDryRun:
    @patch("bb2gh.pr_migrator.State")
    @patch("bb2gh.pr_migrator.GithubClient")
    @patch("bb2gh.pr_migrator.BitbucketClient")
    def test_dry_run_includes_closed_prs(self, MockBB, MockGH, MockState, mock_config, sample_pr):
        state_instance = MockState.return_value
        state_instance.get_migrated_repos.return_value = [("PROJ", "my-repo")]
        state_instance.get_github_target.return_value = ("my-org", "my-repo")
        state_instance.is_pr_migrated.return_value = False

        merged_pr = dict(sample_pr, id=99, state="MERGED",
                         title="Already merged")
        merged_pr["fromRef"] = dict(sample_pr["fromRef"], latestCommit="aaa")

        bb_instance = MockBB.return_value
        bb_instance.list_pull_requests.side_effect = lambda proj, repo, state: {
            "OPEN": [sample_pr],
            "MERGED": [merged_pr],
            "DECLINED": [],
        }[state]

        migrated, skipped, failed = migrate_pull_requests(
            mock_config, dry_run=True, include_closed=True,
        )

        assert migrated == 2
        assert skipped == 0
        MockGH.return_value.create_pull_request.assert_not_called()
