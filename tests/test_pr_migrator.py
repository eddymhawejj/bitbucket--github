"""Tests for the PR migrator module."""

from unittest.mock import MagicMock, patch

import pytest

from bb2gh.config import Config
from bb2gh.pr_migrator import (
    migrate_pull_requests,
    Throttler,
    _format_pr_body,
    _format_comment,
    _map_reviewers,
    _iter_comment_activities,
)


@pytest.fixture(autouse=True)
def fast_throttler(monkeypatch):
    """Ensure Throttler defaults to zero-delay for all tests."""
    original_init = Throttler.__init__

    def zero_init(self, api_delay=0.5, pr_delay=3.0, max_retries=5):
        original_init(self, api_delay=0, pr_delay=0, max_retries=max_retries)

    monkeypatch.setattr(Throttler, "__init__", zero_init)


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


class TestIterCommentActivities:
    def test_filters_and_sorts_chronologically(self):
        activities = [
            {"action": "COMMENTED", "comment": {"text": "second", "createdDate": 200}},
            {"action": "APPROVED"},
            {"action": "COMMENTED", "comment": {"text": "first", "createdDate": 100}},
            {"action": "MERGED"},
            {"action": "COMMENTED", "comment": {"text": "third", "createdDate": 300}},
        ]

        result = list(_iter_comment_activities(activities))

        assert len(result) == 3
        assert result[0]["comment"]["text"] == "first"
        assert result[1]["comment"]["text"] == "second"
        assert result[2]["comment"]["text"] == "third"

    def test_returns_empty_when_no_comments(self):
        activities = [{"action": "APPROVED"}, {"action": "MERGED"}]
        assert list(_iter_comment_activities(activities)) == []


class TestMigrateClosedAsIssue:
    """Closed PRs are migrated as closed GitHub Issues (no branch recreation)."""

    @patch("bb2gh.pr_migrator.State")
    @patch("bb2gh.pr_migrator.GithubClient")
    @patch("bb2gh.pr_migrator.BitbucketClient")
    def test_closed_pr_becomes_closed_issue(self, MockBB, MockGH, MockState, mock_config, sample_pr):
        state_instance = MockState.return_value
        state_instance.get_migrated_repos.return_value = [("PROJ", "my-repo")]
        state_instance.get_github_target.return_value = ("my-org", "my-repo")
        state_instance.is_pr_migrated.return_value = False

        merged_pr = dict(sample_pr, id=99, state="MERGED", title="Old merged PR")

        bb_instance = MockBB.return_value
        bb_instance.list_pull_requests.side_effect = lambda proj, repo, state: {
            "MERGED": [merged_pr], "DECLINED": [],
        }.get(state, [])
        bb_instance.get_pr_activities.return_value = [
            {
                "action": "COMMENTED",
                "comment": {
                    "author": {"name": "reviewer", "displayName": "Reviewer"},
                    "text": "Great work",
                    "createdDate": 1711234567000,
                },
            },
        ]

        mock_issue = MagicMock()
        mock_issue.number = 42
        mock_repo = MagicMock()
        mock_repo.create_issue.return_value = mock_issue
        MockGH.return_value.get_repo.return_value = mock_repo

        migrated, _, failed = migrate_pull_requests(
            mock_config, dry_run=False, closed_only=True,
        )

        assert migrated == 1
        assert failed == 0

        # No PR created — it's an issue
        MockGH.return_value.create_pull_request.assert_not_called()

        # Issue was created with the right title prefix
        mock_repo.create_issue.assert_called_once()
        issue_kwargs = mock_repo.create_issue.call_args.kwargs
        assert "MERGED PR #99" in issue_kwargs["title"]
        assert "Old merged PR" in issue_kwargs["title"]
        assert "migrated-pr" in issue_kwargs["labels"]
        assert "merged" in issue_kwargs["labels"]

        # Comment was added on the issue
        mock_issue.create_comment.assert_called_once()

        # Issue was closed
        mock_issue.edit.assert_called_once_with(state="closed")

        # State recorded the mapping
        state_instance.record_pr_mapping.assert_called_once_with(
            "PROJ", "my-repo", 99, 42,
        )

    @patch("bb2gh.pr_migrator.State")
    @patch("bb2gh.pr_migrator.GithubClient")
    @patch("bb2gh.pr_migrator.BitbucketClient")
    def test_declined_pr_labeled_declined(self, MockBB, MockGH, MockState, mock_config, sample_pr):
        state_instance = MockState.return_value
        state_instance.get_migrated_repos.return_value = [("PROJ", "my-repo")]
        state_instance.get_github_target.return_value = ("my-org", "my-repo")
        state_instance.is_pr_migrated.return_value = False

        declined_pr = dict(sample_pr, id=7, state="DECLINED", title="Won't fix")

        bb_instance = MockBB.return_value
        bb_instance.list_pull_requests.side_effect = lambda proj, repo, state: {
            "MERGED": [], "DECLINED": [declined_pr],
        }.get(state, [])
        bb_instance.get_pr_activities.return_value = []

        mock_issue = MagicMock()
        mock_issue.number = 8
        mock_repo = MagicMock()
        mock_repo.create_issue.return_value = mock_issue
        MockGH.return_value.get_repo.return_value = mock_repo

        migrate_pull_requests(mock_config, closed_only=True)

        issue_kwargs = mock_repo.create_issue.call_args.kwargs
        assert "declined" in issue_kwargs["labels"]

    @patch("bb2gh.pr_migrator.State")
    @patch("bb2gh.pr_migrator.GithubClient")
    @patch("bb2gh.pr_migrator.BitbucketClient")
    def test_falls_back_when_labels_missing(self, MockBB, MockGH, MockState, mock_config, sample_pr):
        """If the labels don't exist on the repo yet, create the issue anyway."""
        from github import GithubException

        state_instance = MockState.return_value
        state_instance.get_migrated_repos.return_value = [("PROJ", "my-repo")]
        state_instance.get_github_target.return_value = ("my-org", "my-repo")
        state_instance.is_pr_migrated.return_value = False

        merged_pr = dict(sample_pr, id=11, state="MERGED", title="Merged x")

        bb_instance = MockBB.return_value
        bb_instance.list_pull_requests.side_effect = lambda proj, repo, state: {
            "MERGED": [merged_pr], "DECLINED": [],
        }.get(state, [])
        bb_instance.get_pr_activities.return_value = []

        mock_issue = MagicMock()
        mock_issue.number = 12
        mock_repo = MagicMock()
        # First call (with labels) fails 422; second call (no labels) succeeds
        mock_repo.create_issue.side_effect = [
            GithubException(422, {"message": "label not found"}, {}),
            mock_issue,
        ]
        MockGH.return_value.get_repo.return_value = mock_repo

        migrated, _, failed = migrate_pull_requests(mock_config, closed_only=True)

        assert migrated == 1
        assert failed == 0
        assert mock_repo.create_issue.call_count == 2
        # Second call had no labels kwarg
        second_call = mock_repo.create_issue.call_args_list[1]
        assert "labels" not in second_call.kwargs


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

    @patch("bb2gh.pr_migrator.State")
    @patch("bb2gh.pr_migrator.GithubClient")
    @patch("bb2gh.pr_migrator.BitbucketClient")
    def test_closed_only_skips_open(self, MockBB, MockGH, MockState, mock_config, sample_pr):
        state_instance = MockState.return_value
        state_instance.get_migrated_repos.return_value = [("PROJ", "my-repo")]
        state_instance.get_github_target.return_value = ("my-org", "my-repo")
        state_instance.is_pr_migrated.return_value = False

        merged_pr = dict(sample_pr, id=99, state="MERGED", title="Old merged PR")
        merged_pr["fromRef"] = dict(sample_pr["fromRef"], latestCommit="aaa")

        bb_instance = MockBB.return_value
        calls = []
        def list_prs(proj, repo, state):
            calls.append(state)
            return {"MERGED": [merged_pr], "DECLINED": []}.get(state, [])
        bb_instance.list_pull_requests.side_effect = list_prs

        migrated, _, _ = migrate_pull_requests(
            mock_config, dry_run=True, closed_only=True,
        )

        # Only one PR (the merged one), OPEN was never queried
        assert migrated == 1
        assert "OPEN" not in calls
        assert "MERGED" in calls
        assert "DECLINED" in calls


class TestThrottler:
    def test_call_retries_on_rate_limit(self, monkeypatch):
        from bb2gh.pr_migrator import Throttler
        import github

        throttler = Throttler(api_delay=0, pr_delay=0, max_retries=3)

        # Fake rate-limit exception on first two calls, success on third
        attempts = {"n": 0}
        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 3:
                exc = github.GithubException(429, {"message": "too fast"}, {"Retry-After": "0"})
                raise exc
            return "ok"

        result = throttler.call(flaky)
        assert result == "ok"
        assert attempts["n"] == 3

    def test_call_raises_after_max_retries(self):
        from bb2gh.pr_migrator import Throttler
        import github

        throttler = Throttler(api_delay=0, pr_delay=0, max_retries=2)

        def always_fail():
            raise github.GithubException(429, {"message": "no"}, {"Retry-After": "0"})

        with pytest.raises(github.GithubException):
            throttler.call(always_fail)

    def test_call_reraises_non_rate_limit(self):
        from bb2gh.pr_migrator import Throttler
        import github

        throttler = Throttler(api_delay=0, pr_delay=0, max_retries=3)

        def not_found():
            raise github.GithubException(404, {"message": "gone"}, {})

        with pytest.raises(github.GithubException) as exc_info:
            throttler.call(not_found)
        assert exc_info.value.status == 404

    def test_wait_api_spacing(self, monkeypatch):
        from bb2gh.pr_migrator import Throttler

        sleeps = []
        monkeypatch.setattr("bb2gh.pr_migrator.time.sleep", lambda s: sleeps.append(s))

        # First call returns 100.1 (elapsed check), second returns 100.5 (record)
        times = iter([100.1, 100.5])
        monkeypatch.setattr(
            "bb2gh.pr_migrator.time.monotonic", lambda: next(times),
        )

        throttler = Throttler(api_delay=0.5, pr_delay=0, max_retries=0)
        throttler.api_delay = 0.5  # override autouse zeroing
        throttler._last_call = 100.0  # simulate a previous call at t=100.0
        throttler.wait_api()

        # elapsed = 100.1 - 100.0 = 0.1, so remaining = 0.5 - 0.1 = 0.4
        assert sleeps and abs(sleeps[0] - 0.4) < 0.01
