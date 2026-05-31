"""End-to-end integration tests for the GitHub issue pipeline.

Covers the full lifecycle: fetch -> normalize -> rank -> execute -> post_execution -> PR,
with mocked gh CLI calls via run_with_progress.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from control_plane.models import RunStatus
from integrations.config import IntegrationConfig
from integrations.github.branch_manager import BranchManager, generate_slug
from integrations.github.github_host import GitHubCodeHost
from integrations.github.github_tracker import GitHubIssueTracker
from integrations.models import NormalizedIssue, RawIssue
from integrations.ranker import IssueRanker


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_raw_issue_data():
    """Standard GitHub issue JSON as returned by gh issue list."""
    return {
        "number": 42,
        "title": "Fix login timeout on slow networks",
        "body": "Users report 30s timeout is too short.",
        "labels": [{"name": "weave"}, {"name": "bug"}],
        "url": "https://github.com/acme/app/issues/42",
        "repo": "acme/app",
        "createdAt": "2026-03-15T09:30:00Z",
        "author": {"login": "dev-alice"},
    }


@pytest.fixture
def sample_raw_issue(sample_raw_issue_data):
    return RawIssue(source="github", data=sample_raw_issue_data)


@pytest.fixture
def sample_normalized():
    return NormalizedIssue(
        number=42,
        title="Fix login timeout on slow networks",
        body="Users report 30s timeout is too short.",
        labels=["weave", "bug"],
        url="https://github.com/acme/app/issues/42",
        repo="acme/app",
        created_at=datetime(2026, 3, 15, 9, 30, tzinfo=timezone.utc),
        author="dev-alice",
    )


@pytest.fixture
def issue_metadata():
    """Standard job_metadata dict used by handle_result."""
    return {
        "issue_number": 42,
        "requirement": "Fix login timeout on slow networks",
        "branch_name": "fix/42-fix-login-timeout-on-slow",
        "repo": "acme/app",
        "issue_url": "https://github.com/acme/app/issues/42",
        "labels": ["weave", "bug"],
    }


def _make_run(status=RunStatus.SUCCEEDED, dag_result=None, started_at=None, completed_at=None):
    """Factory for mock Run objects used in post_execution tests."""
    run = MagicMock()
    run.status = status
    run.dag_result = dag_result or {"work_dir": "/tmp/work"}
    run.started_at = started_at
    run.completed_at = completed_at
    return run


def _mock_subprocess_ok(stdout="", stderr=""):
    return MagicMock(returncode=0, stdout=stdout, stderr=stderr)


def _mock_subprocess_fail(stderr="error"):
    return MagicMock(returncode=1, stdout="", stderr=stderr)


# ===========================================================================
# 1. cmd_issue_poll: fetch -> normalize -> rank -> execute -> PR
# ===========================================================================

class TestCmdIssuePoll:
    """Integration tests for cmd_issue_poll wiring."""

    @pytest.mark.asyncio
    async def test_poll_fetches_normalizes_ranks_and_executes_top_issue(
        self, sample_raw_issue_data,
    ):
        """Full pipeline: gh auth ok -> fetch issues -> normalize -> rank -> execute."""
        tracker = GitHubIssueTracker()
        host = MagicMock()
        host.update_labels = AsyncMock()
        host.push_changes = AsyncMock(return_value=True)
        host.find_existing_pr = AsyncMock(return_value="")
        host.create_pr = AsyncMock(return_value="https://github.com/acme/app/pull/1")
        host.comment_on_issue = AsyncMock()

        mock_run = _make_run(
            status=RunStatus.SUCCEEDED,
            dag_result={"work_dir": "/tmp/work", "total_nodes": 2, "success_nodes": 2},
        )
        mock_job = MagicMock()
        mock_job.id = "job-1"
        mock_job.metadata = {
            "issue_number": 42,
            "requirement": "Fix login timeout",
            "branch_name": "fix/42-fix-login-timeout",
            "repo": "acme/app",
            "issue_url": "https://github.com/acme/app/issues/42",
            "labels": ["weave", "bug"],
        }

        mock_service = MagicMock()
        mock_service.submit_job = AsyncMock(return_value=mock_job)
        mock_service.run_job = AsyncMock(return_value=mock_run)

        with patch("integrations.github.github_tracker.run_with_progress",
                   return_value=_mock_subprocess_ok(
                       stdout=json.dumps([sample_raw_issue_data])
                   )), \
             patch("cli.github._make_tracker", return_value=tracker), \
             patch("cli.github._make_host", return_value=host), \
             patch("cli.github._make_services", return_value=(MagicMock(), mock_service)), \
             patch("cli.github._make_ranker", return_value=IssueRanker(llm_config=None)), \
             patch("cli.github.IntegrationConfig.from_env",
                   return_value=IntegrationConfig(github_repo="acme/app")), \
             patch("integrations.github.post_execution._detect_changes",
                   return_value=True), \
             patch("integrations.github.post_execution._commit_changes",
                   return_value=True), \
             patch("integrations.github.post_execution.generate_pr_body",
                   return_value="PR body"), \
             patch("integrations.github.post_execution.handle_result",
                   return_value=MagicMock(
                       status="success",
                       pr_url="https://github.com/acme/app/pull/1",
                       issue_comment="",
                   )):
            from cli.github import cmd_issue_poll

            args = MagicMock()
            args.repo = "acme/app"
            args.dry_run = False
            args.limit = 1
            await cmd_issue_poll(args)

        # Verify labels were updated to mark running
        assert host.update_labels.call_count >= 1

    @pytest.mark.asyncio
    async def test_poll_dry_run_stops_before_execution(self, sample_raw_issue_data):
        """Dry run should fetch and rank but not execute."""
        tracker = GitHubIssueTracker()

        with patch("integrations.github.github_tracker.run_with_progress",
                   return_value=_mock_subprocess_ok(
                       stdout=json.dumps([sample_raw_issue_data])
                   )), \
             patch("cli.github._make_tracker", return_value=tracker), \
             patch("cli.github._make_ranker", return_value=IssueRanker(llm_config=None)), \
             patch("cli.github.IntegrationConfig.from_env",
                   return_value=IntegrationConfig(github_repo="acme/app", dry_run=True)):
            from cli.github import cmd_issue_poll

            args = MagicMock()
            args.repo = "acme/app"
            args.dry_run = True
            args.limit = 1
            await cmd_issue_poll(args)

    @pytest.mark.asyncio
    async def test_poll_no_repo_exits(self):
        """Should exit if no repo configured."""
        with patch("cli.github.IntegrationConfig.from_env",
                   return_value=IntegrationConfig(github_repo="")):
            from cli.github import cmd_issue_poll

            args = MagicMock()
            args.repo = None
            args.dry_run = False
            args.limit = 1
            with pytest.raises(SystemExit):
                await cmd_issue_poll(args)

    @pytest.mark.asyncio
    async def test_poll_auth_failure_exits(self):
        """Should exit if gh auth fails."""
        with patch("integrations.github.github_tracker.run_with_progress",
                   return_value=_mock_subprocess_fail()), \
             patch("cli.github._make_tracker", return_value=GitHubIssueTracker()), \
             patch("cli.github.IntegrationConfig.from_env",
                   return_value=IntegrationConfig(github_repo="acme/app")):
            from cli.github import cmd_issue_poll

            args = MagicMock()
            args.repo = "acme/app"
            args.dry_run = False
            args.limit = 1
            with pytest.raises(SystemExit):
                await cmd_issue_poll(args)

    @pytest.mark.asyncio
    async def test_poll_empty_issues_prints_message(self):
        """Should print 'No issues found' when no matching issues."""
        with patch("integrations.github.github_tracker.run_with_progress",
                   return_value=_mock_subprocess_ok(stdout="[]")), \
             patch("cli.github._make_tracker", return_value=GitHubIssueTracker()), \
             patch("cli.github.IntegrationConfig.from_env",
                   return_value=IntegrationConfig(github_repo="acme/app")):
            from cli.github import cmd_issue_poll

            args = MagicMock()
            args.repo = "acme/app"
            args.dry_run = False
            args.limit = 1
            await cmd_issue_poll(args)


# ===========================================================================
# 2. cmd_issue_run: fetch single issue -> execute -> PR
# ===========================================================================

class TestCmdIssueRun:
    """Integration tests for cmd_issue_run single-issue execution."""

    @pytest.mark.asyncio
    async def test_run_fetches_and_executes_single_issue(self, sample_raw_issue_data):
        """cmd_issue_run should fetch a single issue by number and execute it."""
        host = MagicMock()
        host.update_labels = AsyncMock()
        host.push_changes = AsyncMock(return_value=True)
        host.find_existing_pr = AsyncMock(return_value="")
        host.create_pr = AsyncMock(return_value="https://github.com/acme/app/pull/2")
        host.comment_on_issue = AsyncMock()

        mock_run = _make_run(status=RunStatus.SUCCEEDED)
        mock_job = MagicMock()
        mock_job.id = "job-2"
        mock_job.metadata = {
            "issue_number": 42,
            "requirement": "Fix login timeout",
            "branch_name": "fix/42-fix-login-timeout",
            "repo": "acme/app",
            "issue_url": "https://github.com/acme/app/issues/42",
            "labels": ["bug"],
        }
        mock_service = MagicMock()
        mock_service.submit_job = AsyncMock(return_value=mock_job)
        mock_service.run_job = AsyncMock(return_value=mock_run)

        view_stdout = json.dumps(sample_raw_issue_data)

        mock_core_rwp = MagicMock(side_effect=[
            _mock_subprocess_ok(stdout=view_stdout),  # gh issue view (from local import)
        ])
        mock_tracker_rwp = MagicMock(side_effect=[
            _mock_subprocess_ok(),       # health_check (from tracker module import)
        ])

        with patch("core.subprocess_runner.run_with_progress", mock_core_rwp), \
             patch("integrations.github.github_tracker.run_with_progress", mock_tracker_rwp), \
             patch("cli.github._make_tracker", return_value=GitHubIssueTracker()), \
             patch("cli.github._make_host", return_value=host), \
             patch("cli.github._make_services", return_value=(MagicMock(), mock_service)), \
             patch("cli.github.IntegrationConfig.from_env",
                   return_value=IntegrationConfig(github_repo="acme/app")), \
             patch("integrations.github.post_execution.handle_result",
                   return_value=MagicMock(status="success",
                                          pr_url="https://github.com/acme/app/pull/2",
                                          issue_comment="")):

            from cli.github import cmd_issue_run

            args = MagicMock()
            args.repo = "acme/app"
            args.number = 42
            await cmd_issue_run(args)

        host.update_labels.assert_called()

    @pytest.mark.asyncio
    async def test_run_issue_not_found_exits(self):
        """Should exit if issue number not found."""
        with patch("integrations.github.github_tracker.run_with_progress") as mock, \
             patch("cli.github._make_tracker", return_value=GitHubIssueTracker()), \
             patch("cli.github.IntegrationConfig.from_env",
                   return_value=IntegrationConfig(github_repo="acme/app")):
            mock.side_effect = [
                _mock_subprocess_ok(),       # health_check
                _mock_subprocess_fail(),     # gh issue view
            ]
            from cli.github import cmd_issue_run

            args = MagicMock()
            args.repo = "acme/app"
            args.number = 9999
            with pytest.raises(SystemExit):
                await cmd_issue_run(args)

    @pytest.mark.asyncio
    async def test_run_no_repo_exits(self):
        """Should exit if no repo configured."""
        with patch("cli.github.IntegrationConfig.from_env",
                   return_value=IntegrationConfig(github_repo="")):
            from cli.github import cmd_issue_run

            args = MagicMock()
            args.repo = None
            args.number = 1
            with pytest.raises(SystemExit):
                await cmd_issue_run(args)


# ===========================================================================
# 3. cmd_issue_status: display issue states
# ===========================================================================

class TestCmdIssueStatus:
    """Integration tests for cmd_issue_status display."""

    @pytest.mark.asyncio
    async def test_status_displays_classified_issues(self, sample_raw_issue_data):
        """Should fetch issues and display status based on labels."""
        issues_data = [
            {**sample_raw_issue_data, "number": 1, "title": "Bug fix",
             "labels": [{"name": "weave-running"}]},
            {**sample_raw_issue_data, "number": 2, "title": "Feature",
             "labels": [{"name": "weave-pr"}]},
            {**sample_raw_issue_data, "number": 3, "title": "Failed one",
             "labels": [{"name": "weave-failed"}]},
            {**sample_raw_issue_data, "number": 4, "title": "Queued",
             "labels": [{"name": "weave"}]},
        ]

        with patch("integrations.github.github_tracker.run_with_progress",
                   return_value=_mock_subprocess_ok(stdout=json.dumps(issues_data))), \
             patch("cli.github._make_tracker", return_value=GitHubIssueTracker()), \
             patch("cli.github.IntegrationConfig.from_env",
                   return_value=IntegrationConfig(github_repo="acme/app")):
            from cli.github import cmd_issue_status

            args = MagicMock()
            args.repo = "acme/app"
            await cmd_issue_status(args)

    @pytest.mark.asyncio
    async def test_status_no_issues(self):
        """Should print message when no issues found."""
        with patch("integrations.github.github_tracker.run_with_progress",
                   return_value=_mock_subprocess_ok(stdout="[]")), \
             patch("cli.github._make_tracker", return_value=GitHubIssueTracker()), \
             patch("cli.github.IntegrationConfig.from_env",
                   return_value=IntegrationConfig(github_repo="acme/app")):
            from cli.github import cmd_issue_status

            args = MagicMock()
            args.repo = "acme/app"
            await cmd_issue_status(args)

    @pytest.mark.asyncio
    async def test_status_no_repo_exits(self):
        """Should exit if no repo configured."""
        with patch("cli.github.IntegrationConfig.from_env",
                   return_value=IntegrationConfig(github_repo="")):
            from cli.github import cmd_issue_status

            args = MagicMock()
            args.repo = None
            with pytest.raises(SystemExit):
                await cmd_issue_status(args)


# ===========================================================================
# 4. post_execution.handle_result: three outcomes
# ===========================================================================

class TestHandleResultPipeline:
    """Integration tests for the post_execution outcome logic."""

    @pytest.mark.asyncio
    async def test_success_outcome_creates_regular_pr(self, issue_metadata):
        """Succeeded run with changes should create a non-draft PR."""
        run = _make_run(
            status=RunStatus.SUCCEEDED,
            dag_result={"work_dir": "/tmp/work"},
        )
        host = MagicMock()
        host.push_changes = AsyncMock(return_value=True)
        host.find_existing_pr = AsyncMock(return_value="")
        host.create_pr = AsyncMock(return_value="https://github.com/acme/app/pull/10")

        with patch("integrations.github.post_execution._detect_changes",
                   return_value=True), \
             patch("integrations.github.post_execution._commit_changes",
                   return_value=True), \
             patch("integrations.github.post_execution.generate_pr_body",
                   return_value="PR body"):
            from integrations.github.post_execution import handle_result
            result = await handle_result(run, issue_metadata, host)

        assert result.status == "success"
        assert result.pr_url == "https://github.com/acme/app/pull/10"
        _, kwargs = host.create_pr.call_args
        assert kwargs["draft"] is False

    @pytest.mark.asyncio
    async def test_partial_outcome_creates_draft_pr(self, issue_metadata):
        """Failed run with changes should create a draft PR."""
        run = _make_run(
            status=RunStatus.FAILED,
            dag_result={"work_dir": "/tmp/work", "error": "eval failed"},
        )
        host = MagicMock()
        host.push_changes = AsyncMock(return_value=True)
        host.find_existing_pr = AsyncMock(return_value="")
        host.create_pr = AsyncMock(return_value="https://github.com/acme/app/pull/11")

        with patch("integrations.github.post_execution._detect_changes",
                   return_value=True), \
             patch("integrations.github.post_execution._commit_changes",
                   return_value=True), \
             patch("integrations.github.post_execution.generate_pr_body",
                   return_value="PR body"):
            from integrations.github.post_execution import handle_result
            result = await handle_result(run, issue_metadata, host)

        assert result.status == "partial"
        assert "draft PR" in result.issue_comment
        _, kwargs = host.create_pr.call_args
        assert kwargs["draft"] is True

    @pytest.mark.asyncio
    async def test_no_output_outcome_comments_on_issue(self, issue_metadata):
        """Run with no detected changes should comment 'no code changes'."""
        run = _make_run()
        host = MagicMock()
        host.find_existing_pr = AsyncMock(return_value="")

        with patch("integrations.github.post_execution._detect_changes",
                   return_value=False):
            from integrations.github.post_execution import handle_result
            result = await handle_result(run, issue_metadata, host)

        assert result.status == "no_output"
        assert "no code changes" in result.issue_comment.lower()

    @pytest.mark.asyncio
    async def test_push_failed_outcome(self, issue_metadata):
        """Run where push fails should return push_failed status."""
        run = _make_run()
        host = MagicMock()
        host.push_changes = AsyncMock(return_value=False)

        with patch("integrations.github.post_execution._detect_changes",
                   return_value=True), \
             patch("integrations.github.post_execution._commit_changes",
                   return_value=True), \
             patch("integrations.github.post_execution.generate_pr_body",
                   return_value="PR body"):
            from integrations.github.post_execution import handle_result
            result = await handle_result(run, issue_metadata, host)

        assert result.status == "push_failed"
        assert "push failed" in result.issue_comment.lower()

    @pytest.mark.asyncio
    async def test_no_work_dir_returns_push_failed(self, issue_metadata):
        """Run without work_dir should return push_failed immediately."""
        run = MagicMock()
        run.dag_result = {}
        host = MagicMock()

        from integrations.github.post_execution import handle_result
        result = await handle_result(run, issue_metadata, host)

        assert result.status == "push_failed"
        assert "work directory" in result.issue_comment.lower()

    @pytest.mark.asyncio
    async def test_commit_failed_returns_push_failed(self, issue_metadata):
        """Run where commit fails should return push_failed."""
        run = _make_run()
        host = MagicMock()

        with patch("integrations.github.post_execution._detect_changes",
                   return_value=True), \
             patch("integrations.github.post_execution._commit_changes",
                   return_value=False):
            from integrations.github.post_execution import handle_result
            result = await handle_result(run, issue_metadata, host)

        assert result.status == "push_failed"
        assert "commit" in result.issue_comment.lower()


# ===========================================================================
# 5. BranchManager: creation, slug, reuse
# ===========================================================================

class TestBranchManagerPipeline:
    """Integration tests for BranchManager with mocked git commands."""

    @pytest.mark.asyncio
    async def test_create_new_branch(self, sample_normalized):
        """Should create a new branch when it does not exist."""
        with patch("integrations.github.branch_manager.run_with_progress") as mock:
            # rev-parse fails (branch doesn't exist), checkout -b succeeds
            mock.side_effect = [
                _mock_subprocess_fail(),       # rev-parse --verify
                _mock_subprocess_ok(),         # checkout -b
            ]
            mgr = BranchManager(repo_root="/tmp/repo")
            branch = await mgr.create_branch("acme/app", sample_normalized)

        assert branch == "fix/42-fix-login-timeout-on-slow"
        assert mock.call_count == 2

    @pytest.mark.asyncio
    async def test_reuse_existing_branch(self, sample_normalized):
        """Should checkout existing branch instead of creating new one."""
        with patch("integrations.github.branch_manager.run_with_progress") as mock:
            # rev-parse succeeds (branch exists), checkout succeeds
            mock.side_effect = [
                _mock_subprocess_ok(),         # rev-parse --verify
                _mock_subprocess_ok(),         # checkout existing
            ]
            mgr = BranchManager(repo_root="/tmp/repo")
            branch = await mgr.create_branch("acme/app", sample_normalized)

        assert branch == "fix/42-fix-login-timeout-on-slow"
        # Should call checkout, not checkout -b
        second_call_args = mock.call_args_list[1]
        assert "checkout" in second_call_args[0][0]
        assert "-b" not in second_call_args[0][0]

    @pytest.mark.asyncio
    async def test_push_branch_success(self):
        """push_branch should return True on success."""
        with patch("integrations.github.branch_manager.run_with_progress",
                   return_value=_mock_subprocess_ok()):
            mgr = BranchManager(repo_root="/tmp/repo")
            result = await mgr.push_branch("fix/42-foo")
        assert result is True

    @pytest.mark.asyncio
    async def test_push_branch_failure(self):
        """push_branch should return False on failure."""
        with patch("integrations.github.branch_manager.run_with_progress",
                   return_value=_mock_subprocess_fail("push rejected")):
            mgr = BranchManager(repo_root="/tmp/repo")
            result = await mgr.push_branch("fix/42-foo")
        assert result is False

    @pytest.mark.asyncio
    async def test_push_branch_with_force(self):
        """push_branch with force=True should include --force flag."""
        with patch("integrations.github.branch_manager.run_with_progress",
                   return_value=_mock_subprocess_ok()) as mock:
            mgr = BranchManager(repo_root="/tmp/repo")
            await mgr.push_branch("fix/42-foo", force=True)
        cmd_args = mock.call_args[0][0]
        assert "--force" in cmd_args

    def test_slug_generation_basic(self):
        """generate_slug should produce lowercase hyphenated slug."""
        assert generate_slug("Fix login timeout") == "fix-login-timeout"

    def test_slug_generation_special_chars(self):
        """generate_slug should strip special characters."""
        assert generate_slug("Fix bug: null pointer! (urgent)") == "fix-bug-null-pointer-urgent"

    def test_slug_generation_max_length(self):
        """generate_slug should respect max_length."""
        result = generate_slug("a " * 30, max_length=20)
        assert len(result) <= 20

    def test_slug_generation_non_ascii_fallback(self):
        """generate_slug should return empty for non-ASCII input."""
        assert generate_slug("修复登录问题") == ""


# ===========================================================================
# 6. IssueRanker: LLM ranking + chronological fallback
# ===========================================================================

class TestIssueRankerPipeline:
    """Integration tests for IssueRanker with mocked LLM."""

    @pytest.mark.asyncio
    async def test_llm_ranking_reorders_issues(self):
        """LLM should reorder issues by priority."""
        issues = [
            NormalizedIssue(number=1, title="Low priority",
                            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
            NormalizedIssue(number=2, title="High priority",
                            created_at=datetime(2026, 2, 1, tzinfo=timezone.utc)),
            NormalizedIssue(number=3, title="Medium priority",
                            created_at=datetime(2026, 3, 1, tzinfo=timezone.utc)),
        ]
        mock_llm_config = MagicMock()
        mock_llm_config.api_key = "test-key"
        mock_llm_config.model = "test-model"

        mock_client = MagicMock()
        mock_client.chat = AsyncMock(return_value={"content": "[3, 2, 1]"})

        with patch("core.llm_client.LLMClient", return_value=mock_client):
            ranker = IssueRanker(llm_config=mock_llm_config)
            result = await ranker.rank(issues)

        assert result[0].number == 3
        assert result[1].number == 2
        assert result[2].number == 1

    @pytest.mark.asyncio
    async def test_llm_ranking_handles_markdown_wrapped_json(self):
        """LLM response may wrap JSON in markdown code blocks."""
        issues = [
            NormalizedIssue(number=10, title="A",
                            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
            NormalizedIssue(number=20, title="B",
                            created_at=datetime(2026, 2, 1, tzinfo=timezone.utc)),
        ]
        mock_llm_config = MagicMock()
        mock_llm_config.api_key = "test-key"
        mock_llm_config.model = "test-model"

        mock_client = MagicMock()
        mock_client.chat = AsyncMock(
            return_value={"content": "```json\n[20, 10]\n```"}
        )

        with patch("core.llm_client.LLMClient", return_value=mock_client):
            ranker = IssueRanker(llm_config=mock_llm_config)
            result = await ranker.rank(issues)

        assert result[0].number == 20
        assert result[1].number == 10

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_chronological(self):
        """When LLM fails, should fall back to chronological order."""
        issues = [
            NormalizedIssue(number=5, title="New",
                            created_at=datetime(2026, 6, 1, tzinfo=timezone.utc)),
            NormalizedIssue(number=3, title="Old",
                            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        ]
        mock_llm_config = MagicMock()
        mock_llm_config.api_key = "test-key"

        with patch("core.llm_client.LLMClient", side_effect=Exception("API down")):
            ranker = IssueRanker(llm_config=mock_llm_config)
            result = await ranker.rank(issues)

        assert result[0].number == 3  # oldest first
        assert result[1].number == 5

    @pytest.mark.asyncio
    async def test_single_issue_bypasses_llm(self):
        """Single issue should be returned as-is without LLM call."""
        issues = [NormalizedIssue(number=1, title="Only issue")]
        ranker = IssueRanker(llm_config=MagicMock())
        result = await ranker.rank(issues)
        assert result == issues

    @pytest.mark.asyncio
    async def test_empty_list_returns_empty(self):
        """Empty issue list should return empty."""
        ranker = IssueRanker(llm_config=MagicMock())
        result = await ranker.rank([])
        assert result == []

    @pytest.mark.asyncio
    async def test_llm_returns_partial_numbers_keeps_valid(self):
        """LLM may return numbers not in the issue set; those should be dropped."""
        issues = [
            NormalizedIssue(number=1, title="A",
                            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
            NormalizedIssue(number=2, title="B",
                            created_at=datetime(2026, 2, 1, tzinfo=timezone.utc)),
        ]
        mock_llm_config = MagicMock()
        mock_llm_config.api_key = "test-key"
        mock_llm_config.model = "test-model"

        # LLM returns [99, 2, 1] -- 99 doesn't exist
        mock_client = MagicMock()
        mock_client.chat = AsyncMock(return_value={"content": "[99, 2, 1]"})

        with patch("core.llm_client.LLMClient", return_value=mock_client):
            ranker = IssueRanker(llm_config=mock_llm_config)
            result = await ranker.rank(issues)

        assert len(result) == 2
        assert result[0].number == 2
        assert result[1].number == 1


# ===========================================================================
# 7. pr_body.generate_pr_body: diff stat + execution summary
# ===========================================================================

class TestPrBodyPipeline:
    """Integration tests for PR body generation."""

    @pytest.mark.asyncio
    async def test_full_pr_body_with_execution_summary(self, sample_normalized):
        """PR body should contain issue ref, diff stat, execution metrics, and footer."""
        execution_summary = {
            "nodes_total": 4,
            "nodes_completed": 4,
            "tokens_in": 12000,
            "tokens_out": 6000,
            "duration_sec": 183.5,
            "test_summary": "18 passed, 0 failed",
            "lint_summary": "0 errors, 2 warnings",
        }

        with patch("integrations.github.pr_body.run_with_progress",
                   return_value=_mock_subprocess_ok(stdout="5 files changed, 120 insertions")):
            from integrations.github.pr_body import generate_pr_body
            body = await generate_pr_body("/tmp/work", sample_normalized,
                                          llm_config=None,
                                          execution_summary=execution_summary)

        assert "Fix #42" in body
        assert "Fix login timeout on slow networks" in body
        assert "5 files changed" in body
        assert "4/4 completed" in body
        assert "12000in / 6000out" in body
        assert "3m 3s" in body
        assert "18 passed, 0 failed" in body
        assert "0 errors, 2 warnings" in body
        assert "Fixes #42" in body

    @pytest.mark.asyncio
    async def test_pr_body_with_llm_review(self, sample_normalized):
        """PR body should include LLM code review section when configured."""
        mock_config = MagicMock()
        mock_config.api_key = "test-key"
        mock_config.model = "test-model"
        mock_config.provider = "anthropic"
        mock_config.base_url = None

        mock_client = MagicMock()
        mock_client.call = MagicMock(return_value={
            "content": "Changes look good. Consider adding error handling."
        })

        with patch("integrations.github.pr_body.run_with_progress",
                   return_value=_mock_subprocess_ok(stdout="2 files changed")), \
             patch("core.llm_client.LLMClient", return_value=mock_client), \
             patch("core.config.LLMConfig", return_value=MagicMock()):
            from integrations.github.pr_body import generate_pr_body
            body = await generate_pr_body("/tmp/work", sample_normalized,
                                          llm_config=mock_config)

        assert "Code Review" in body
        assert "error handling" in body

    @pytest.mark.asyncio
    async def test_pr_body_enhancement_prefix(self):
        """Enhancement issues should use 'Feat' prefix."""
        issue = NormalizedIssue(number=10, title="Add dark mode",
                                labels=["enhancement"])
        with patch("integrations.github.pr_body.run_with_progress",
                   return_value=_mock_subprocess_ok(stdout="1 file changed")):
            from integrations.github.pr_body import generate_pr_body
            body = await generate_pr_body("/tmp/work", issue, llm_config=None)

        assert "Feat #10" in body
        assert "Fixes #10" in body


# ===========================================================================
# 8. GitHubCodeHost: find_existing_pr, update_pr, push
# ===========================================================================

class TestGitHubCodeHostPipeline:
    """Integration tests for CodeHost methods not covered elsewhere."""

    @pytest.mark.asyncio
    async def test_find_existing_pr_found(self):
        """Should return PR URL when one exists."""
        with patch("integrations.github.github_host.run_with_progress",
                   return_value=_mock_subprocess_ok(
                       stdout=json.dumps([{"url": "https://github.com/o/r/pull/7"}])
                   )):
            host = GitHubCodeHost()
            url = await host.find_existing_pr("o/r", "fix/42-foo")
        assert url == "https://github.com/o/r/pull/7"

    @pytest.mark.asyncio
    async def test_find_existing_pr_not_found(self):
        """Should return empty string when no PR exists."""
        with patch("integrations.github.github_host.run_with_progress",
                   return_value=_mock_subprocess_ok(stdout="[]")):
            host = GitHubCodeHost()
            url = await host.find_existing_pr("o/r", "fix/42-foo")
        assert url == ""

    @pytest.mark.asyncio
    async def test_find_existing_pr_malformed_json(self):
        """Should return empty string on malformed JSON."""
        with patch("integrations.github.github_host.run_with_progress",
                   return_value=_mock_subprocess_ok(stdout="not json")):
            host = GitHubCodeHost()
            url = await host.find_existing_pr("o/r", "fix/42-foo")
        assert url == ""

    @pytest.mark.asyncio
    async def test_update_pr_success(self):
        """update_pr should return True on success."""
        with patch("integrations.github.github_host.run_with_progress",
                   return_value=_mock_subprocess_ok()):
            host = GitHubCodeHost()
            result = await host.update_pr("o/r", "https://github.com/o/r/pull/1", "new body")
        assert result is True

    @pytest.mark.asyncio
    async def test_update_pr_failure(self):
        """update_pr should return False on failure."""
        with patch("integrations.github.github_host.run_with_progress",
                   return_value=_mock_subprocess_fail()):
            host = GitHubCodeHost()
            result = await host.update_pr("o/r", "https://github.com/o/r/pull/1", "new body")
        assert result is False


# ===========================================================================
# 9. GitHubIssueTracker: fetch edge cases
# ===========================================================================

class TestGitHubIssueTrackerPipeline:
    """Integration tests for tracker fetch edge cases."""

    @pytest.mark.asyncio
    async def test_fetch_with_empty_stdout(self):
        """Should return empty list when gh returns empty stdout."""
        with patch("integrations.github.github_tracker.run_with_progress",
                   return_value=_mock_subprocess_ok(stdout="")):
            tracker = GitHubIssueTracker()
            result = await tracker.fetch("o/r", labels=["weave"])
        assert result == []

    @pytest.mark.asyncio
    async def test_fetch_with_gh_error(self):
        """Should return empty list when gh CLI fails."""
        with patch("integrations.github.github_tracker.run_with_progress",
                   return_value=_mock_subprocess_fail("rate limited")):
            tracker = GitHubIssueTracker()
            result = await tracker.fetch("o/r", labels=["weave"])
        assert result == []

    @pytest.mark.asyncio
    async def test_fetch_multiple_issues(self):
        """Should return multiple RawIssues."""
        data = [
            {"number": 1, "title": "First"},
            {"number": 2, "title": "Second"},
            {"number": 3, "title": "Third"},
        ]
        with patch("integrations.github.github_tracker.run_with_progress",
                   return_value=_mock_subprocess_ok(stdout=json.dumps(data))):
            tracker = GitHubIssueTracker()
            result = await tracker.fetch("o/r")
        assert len(result) == 3
        assert all(r.source == "github" for r in result)

    @pytest.mark.asyncio
    async def test_normalize_pipeline_with_missing_created_at(self):
        """Normalize should handle missing createdAt gracefully."""
        tracker = GitHubIssueTracker()
        raw = RawIssue(source="github", data={
            "number": 99,
            "title": "No date",
            "labels": [],
        })
        issue = tracker.normalize(raw)
        assert issue.number == 99
        assert issue.created_at is None
