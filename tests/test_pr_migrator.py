"""Tests for the PR migrator module."""

from unittest.mock import MagicMock, patch

import pytest

from bb2gh.config import Config
from bb2gh.pr_migrator import (
    migrate_pull_requests,
    _format_pr_body,
    _format_comment,
    _map_reviewers,
)


@pytest.fixture
def mock_config():
    config = MagicMock(spec=Config)
    config.bb_base_url = "https://bitbucket.example.com"
    config.bb_token = "fake-token"
    config.gh_base_url = "https://github.example.com/api/v3"
    config.gh_token = "fake-gh-token"
    config.gh_org = "my-org"
    config.work_dir = "/tmp/test"
    config.user_mapping = {"john.doe": "johndoe"}
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

        # Verify PR was created
        MockGH.return_value.create_pull_request.assert_called_once()

        # Verify comment was added (only 1 — the APPROVED activity is skipped)
        MockGH.return_value.add_pr_comment.assert_called_once()

        # Verify state recorded
        state_instance.record_pr_mapping.assert_called_once_with("PROJ", "my-repo", 42, 99)
