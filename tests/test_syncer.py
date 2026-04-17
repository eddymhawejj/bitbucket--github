"""Tests for the continuous syncer module."""

import os
from unittest.mock import MagicMock, patch, call

import pytest

from bb2gh.config import Config
from bb2gh.syncer import Syncer, _clean_hidden_refs


@pytest.fixture
def mock_config(tmp_path):
    config = MagicMock(spec=Config)
    config.work_dir = str(tmp_path)
    config.sync_interval = 1
    config.lfs_enabled = False
    config.lfs_threshold = "100mb"
    return config


class TestSyncer:
    @patch("bb2gh.syncer.State")
    @patch("bb2gh.syncer._run_git")
    def test_sync_repo(self, mock_git, MockState, mock_config, tmp_path):
        """Test syncing a single repo."""
        # Create fake bare repo dir
        bare_path = tmp_path / "PROJ__my-repo.git"
        bare_path.mkdir()

        state_instance = MockState.return_value
        state_instance.get_migrated_repos.return_value = [("PROJ", "my-repo")]
        state_instance.get_github_target.return_value = ("my-org", "my-repo")

        mock_git.return_value = ""

        syncer = Syncer(mock_config)
        syncer._sync_all()

        # Should fetch from origin and push to github
        mock_git.assert_any_call(["fetch", "origin", "--prune"], cwd=str(bare_path))
        mock_git.assert_any_call(["push", "github", "--mirror"], cwd=str(bare_path))
        state_instance.update_sync_time.assert_called_once_with("PROJ", "my-repo")

    @patch("bb2gh.syncer.State")
    def test_no_migrated_repos(self, MockState, mock_config):
        """Test sync when no repos are migrated yet."""
        state_instance = MockState.return_value
        state_instance.get_migrated_repos.return_value = []

        syncer = Syncer(mock_config)
        syncer._sync_all()  # Should not raise

    @patch("bb2gh.syncer.State")
    @patch("bb2gh.syncer._run_git")
    def test_sync_handles_failure(self, mock_git, MockState, mock_config, tmp_path):
        """Test that sync continues if one repo fails."""
        bare1 = tmp_path / "PROJ__repo1.git"
        bare1.mkdir()
        bare2 = tmp_path / "PROJ__repo2.git"
        bare2.mkdir()

        state_instance = MockState.return_value
        state_instance.get_migrated_repos.return_value = [
            ("PROJ", "repo1"),
            ("PROJ", "repo2"),
        ]
        state_instance.get_github_target.return_value = ("my-org", "repo1")

        # First repo fetch fails, second succeeds
        def side_effect(args, cwd=None):
            if "repo1" in str(cwd) and args[0] == "fetch":
                raise Exception("Network error")
            return ""

        mock_git.side_effect = side_effect

        syncer = Syncer(mock_config)
        syncer._sync_all()

        # repo2 should still be synced
        state_instance.update_sync_time.assert_called_once_with("PROJ", "repo2")

    @patch("bb2gh.syncer.State")
    @patch("bb2gh.syncer._run_git")
    def test_sync_logs_github_target(self, mock_git, MockState, mock_config, tmp_path):
        """Test that sync uses the stored GitHub target for logging."""
        bare_path = tmp_path / "INFRA__my-service.git"
        bare_path.mkdir()

        state_instance = MockState.return_value
        state_instance.get_migrated_repos.return_value = [("INFRA", "my-service")]
        state_instance.get_github_target.return_value = ("infra-team", "infra-my-service")

        mock_git.return_value = ""

        syncer = Syncer(mock_config)
        syncer._sync_all()

        state_instance.get_github_target.assert_called_once_with("INFRA", "my-service")
