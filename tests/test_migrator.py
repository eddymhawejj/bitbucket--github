"""Tests for the bulk migrator module."""

import os
import json
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from bb2gh.config import Config
from bb2gh.migrator import migrate_repos, _clean_hidden_refs


@pytest.fixture
def mock_config(tmp_path):
    """Create a mock config object."""
    config = MagicMock(spec=Config)
    config.bb_base_url = "https://bitbucket.example.com"
    config.bb_token = "fake-bb-token"
    config.bb_ssh_url = "ssh://git@bitbucket.example.com:7999"
    config.bb_projects = ["PROJ1"]
    config.gh_base_url = "https://github.example.com/api/v3"
    config.gh_token = "fake-gh-token"
    config.gh_org = "my-org"
    config.work_dir = str(tmp_path)
    config.user_mapping = {}
    return config


class TestCleanHiddenRefs:
    @patch("bb2gh.migrator._run_git")
    def test_removes_pull_refs(self, mock_git):
        mock_git.return_value = (
            "abc123 refs/heads/main\n"
            "def456 refs/pull/1/head\n"
            "ghi789 refs/pull/2/head\n"
        )

        _clean_hidden_refs("/fake/repo.git")

        # Should call show-ref once, then update-ref -d for each pull ref
        assert mock_git.call_count == 3
        mock_git.assert_any_call(["update-ref", "-d", "refs/pull/1/head"], cwd="/fake/repo.git")
        mock_git.assert_any_call(["update-ref", "-d", "refs/pull/2/head"], cwd="/fake/repo.git")

    @patch("bb2gh.migrator._run_git")
    def test_no_hidden_refs(self, mock_git):
        mock_git.return_value = "abc123 refs/heads/main\ndef456 refs/tags/v1.0\n"

        _clean_hidden_refs("/fake/repo.git")

        # Only show-ref called, no deletions
        assert mock_git.call_count == 1


class TestMigrateRepos:
    @patch("bb2gh.migrator.State")
    @patch("bb2gh.migrator.GithubClient")
    @patch("bb2gh.migrator.BitbucketClient")
    @patch("bb2gh.migrator._run_git")
    def test_migrates_new_repo(self, mock_git, MockBB, MockGH, MockState, mock_config):
        # Setup mocks
        bb_instance = MockBB.return_value
        bb_instance.list_repos.return_value = [
            {
                "slug": "my-repo",
                "name": "My Repo",
                "description": "A test repo",
                "links": {"clone": [{"name": "ssh", "href": "ssh://git@bb:7999/proj1/my-repo.git"}]},
            }
        ]

        gh_instance = MockGH.return_value
        gh_instance.get_clone_url.return_value = "https://github.example.com/my-org/my-repo.git"

        state_instance = MockState.return_value
        state_instance.is_migrated.return_value = False

        mock_git.return_value = ""

        migrated, skipped, failed = migrate_repos(mock_config)

        assert migrated == 1
        assert skipped == 0
        assert failed == 0
        state_instance.mark_migrated.assert_called_once_with("PROJ1", "my-repo")

    @patch("bb2gh.migrator.State")
    @patch("bb2gh.migrator.GithubClient")
    @patch("bb2gh.migrator.BitbucketClient")
    def test_skips_already_migrated(self, MockBB, MockGH, MockState, mock_config):
        bb_instance = MockBB.return_value
        bb_instance.list_repos.return_value = [{"slug": "my-repo", "name": "My Repo"}]

        state_instance = MockState.return_value
        state_instance.is_migrated.return_value = True

        migrated, skipped, failed = migrate_repos(mock_config)

        assert migrated == 0
        assert skipped == 1
        assert failed == 0
