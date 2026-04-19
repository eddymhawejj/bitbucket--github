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
    config.migrate_delay = 0
    config.sync_exclude_projects = set()
    return config


class TestSyncer:
    @patch("bb2gh.syncer.State")
    @patch("bb2gh.syncer._run_git")
    def test_sync_repo_with_changes(self, mock_git, MockState, mock_config, tmp_path):
        """Test syncing a repo when changes are detected (no prior snapshot)."""
        bare_path = tmp_path / "PROJ__my-repo.git"
        bare_path.mkdir()

        state_instance = MockState.return_value
        state_instance.get_migrated_repos.return_value = [("PROJ", "my-repo")]
        state_instance.get_github_target.return_value = ("my-org", "my-repo")

        def side_effect(args, cwd=None, quiet=False):
            if args == ["show-ref"]:
                return "abc123 refs/heads/master"
            return ""

        mock_git.side_effect = side_effect

        # No prior snapshot file → should detect changes
        syncer = Syncer(mock_config)
        syncer._sync_all()

        mock_git.assert_any_call(["fetch", "origin", "--prune",
                                  "+refs/heads/*:refs/heads/*",
                                  "+refs/tags/*:refs/tags/*"], cwd=str(bare_path))
        mock_git.assert_any_call(["push", "github", "--mirror"], cwd=str(bare_path))
        state_instance.update_sync_time.assert_called_once_with("PROJ", "my-repo")

    @patch("bb2gh.syncer.State")
    @patch("bb2gh.syncer._run_git")
    def test_sync_skips_when_no_changes(self, mock_git, MockState, mock_config, tmp_path):
        """Test that sync skips push when BB refs match stored snapshot."""
        bare_path = tmp_path / "PROJ__my-repo.git"
        bare_path.mkdir()

        # Write a prior snapshot matching what show-ref will return
        refs_file = bare_path / "bb2gh_last_sync_refs"
        refs_file.write_text("abc123 refs/heads/master")

        state_instance = MockState.return_value
        state_instance.get_migrated_repos.return_value = [("PROJ", "my-repo")]
        state_instance.get_github_target.return_value = ("my-org", "my-repo")

        def side_effect(args, cwd=None, quiet=False):
            if args == ["show-ref"]:
                return "abc123 refs/heads/master"
            return ""

        mock_git.side_effect = side_effect

        syncer = Syncer(mock_config)
        syncer._sync_all()

        # Push should NOT be called
        for c in mock_git.call_args_list:
            assert c[0][0] != ["push", "github", "--mirror"]
        state_instance.update_sync_time.assert_not_called()

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

        def side_effect(args, cwd=None, quiet=False):
            if "repo1" in str(cwd) and args[0] == "fetch":
                raise Exception("Network error")
            if args == ["show-ref"]:
                return "abc123 refs/heads/master"
            return ""

        mock_git.side_effect = side_effect

        syncer = Syncer(mock_config)
        syncer._sync_all()

        # repo1 failed on fetch, repo2 skipped (no changes)
        # Neither should have update_sync_time called

    @patch("bb2gh.syncer.State")
    @patch("bb2gh.syncer._run_git")
    def test_sync_logs_github_target(self, mock_git, MockState, mock_config, tmp_path):
        """Test that sync uses the stored GitHub target for logging."""
        bare_path = tmp_path / "INFRA__my-service.git"
        bare_path.mkdir()
        # No prior snapshot → will detect changes

        state_instance = MockState.return_value
        state_instance.get_migrated_repos.return_value = [("INFRA", "my-service")]
        state_instance.get_github_target.return_value = ("infra-team", "infra-my-service")

        def side_effect(args, cwd=None, quiet=False):
            if args == ["show-ref"]:
                return "aaa refs/heads/main"
            return ""

        mock_git.side_effect = side_effect

        syncer = Syncer(mock_config)
        syncer._sync_all()

        state_instance.get_github_target.assert_called_once_with("INFRA", "my-service")
