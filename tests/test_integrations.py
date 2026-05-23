"""Tests for integrations layer (M5.2)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from integrations.models import LabelConfig, NormalizedIssue, RawIssue
from integrations.config import IntegrationConfig
from integrations.registry import IntegrationRegistry
from integrations.base import CodeHost, IssueTracker
from integrations.github.branch_manager import BranchManager, generate_slug
from integrations.github.github_tracker import GitHubIssueTracker
from integrations.github.github_host import GitHubCodeHost
from integrations.ranker import IssueRanker


# -- Models --

class TestRawIssue:
    def test_creation(self):
        raw = RawIssue(source="github", data={"number": 1})
        assert raw.source == "github"
        assert raw.data["number"] == 1


class TestNormalizedIssue:
    def test_to_requirement(self):
        issue = NormalizedIssue(number=42, title="Fix bug", body="Details here", repo="owner/repo")
        req = issue.to_requirement()
        assert "#42" in req
        assert "Fix bug" in req
        assert "Details here" in req
        assert "owner/repo" in req

    def test_to_requirement_truncation(self):
        issue = NormalizedIssue(number=1, title="T", body="x" * 5000, repo="r")
        req = issue.to_requirement(max_body=100)
        assert "x" * 100 in req

    def test_defaults(self):
        issue = NormalizedIssue(number=1, title="Test")
        assert issue.body == ""
        assert issue.labels == []
        assert issue.created_at is None
        assert issue.author == ""


class TestLabelConfig:
    def test_defaults(self):
        cfg = LabelConfig()
        assert cfg.trigger_label == "weave"
        assert cfg.running_label == "weave-running"


# -- Config --

class TestIntegrationConfig:
    def test_defaults(self):
        cfg = IntegrationConfig()
        assert cfg.github_repo == ""
        assert cfg.dry_run is False

    @patch.dict("os.environ", {"WEAVE_GITHUB_REPO": "foo/bar"})
    def test_from_env(self):
        cfg = IntegrationConfig.from_env()
        assert cfg.github_repo == "foo/bar"


# -- Registry --

class TestIntegrationRegistry:
    def test_register_and_get(self):
        reg = IntegrationRegistry()
        tracker = MagicMock(spec=IssueTracker)
        host = MagicMock(spec=CodeHost)
        reg.register_tracker("github", tracker)
        reg.register_host("github", host)
        assert reg.get_tracker("github") is tracker
        assert reg.get_host("github") is host

    def test_missing_returns_none(self):
        reg = IntegrationRegistry()
        assert reg.get_tracker("missing") is None
        assert reg.get_host("missing") is None

    def test_list(self):
        reg = IntegrationRegistry()
        reg.register_tracker("a", MagicMock(spec=IssueTracker))
        reg.register_host("b", MagicMock(spec=CodeHost))
        assert reg.list_trackers() == ["a"]
        assert reg.list_hosts() == ["b"]


# -- Branch Manager --

class TestGenerateSlug:
    def test_basic(self):
        assert generate_slug("Add OAuth2 support") == "add-oauth2-support"

    def test_special_chars(self):
        assert generate_slug("Fix bug: null pointer! (urgent)") == "fix-bug-null-pointer-urgent"

    def test_non_ascii_fallback(self):
        result = generate_slug("修复登录问题")
        assert result == ""

    def test_max_length(self):
        result = generate_slug("a " * 30, max_length=20)
        assert len(result) <= 20


class TestBranchManager:
    def test_branch_name_with_slug(self):
        issue = NormalizedIssue(number=42, title="Add OAuth2 support")
        mgr = BranchManager()
        assert mgr._branch_name(issue) == "fix/42-add-oauth2-support"

    def test_branch_name_fallback(self):
        issue = NormalizedIssue(number=42, title="修复登录问题")
        mgr = BranchManager()
        assert mgr._branch_name(issue) == "fix/42-issue-42"


# -- GitHub Tracker --

class TestGitHubIssueTracker:
    def test_normalize(self):
        tracker = GitHubIssueTracker()
        raw = RawIssue(source="github", data={
            "number": 42, "title": "Fix bug", "body": "Body text",
            "labels": [{"name": "weave"}],
            "url": "https://github.com/o/r/issues/42",
            "repo": "o/r",
            "createdAt": "2026-01-15T10:00:00Z",
            "author": {"login": "dev"},
        })
        issue = tracker.normalize(raw)
        assert issue.number == 42
        assert issue.labels == ["weave"]
        assert issue.author == "dev"

    def test_normalize_missing_fields(self):
        tracker = GitHubIssueTracker()
        raw = RawIssue(source="github", data={})
        issue = tracker.normalize(raw)
        assert issue.number == 0

    @pytest.mark.asyncio
    async def test_health_check_ok(self):
        with patch("integrations.github.github_tracker.run_with_progress") as mock:
            mock.return_value = MagicMock(returncode=0)
            tracker = GitHubIssueTracker()
            assert await tracker.health_check()

    @pytest.mark.asyncio
    async def test_health_check_fail(self):
        with patch("integrations.github.github_tracker.run_with_progress") as mock:
            mock.return_value = MagicMock(returncode=1)
            tracker = GitHubIssueTracker()
            assert not await tracker.health_check()

    @pytest.mark.asyncio
    async def test_fetch_returns_issues(self):
        with patch("integrations.github.github_tracker.run_with_progress") as mock:
            mock.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps([{"number": 1, "title": "Bug"}]),
            )
            tracker = GitHubIssueTracker()
            raw = await tracker.fetch("owner/repo", labels=["weave"])
            assert len(raw) == 1
            assert raw[0].source == "github"


# -- GitHub Host --

class TestGitHubCodeHost:
    @pytest.mark.asyncio
    async def test_update_labels(self):
        with patch("integrations.github.github_host.run_with_progress") as mock:
            mock.return_value = MagicMock(returncode=0)
            host = GitHubCodeHost()
            await host.update_labels("o/r", 42, add=["running"], remove=["weave"])
            mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_pr(self):
        with patch("integrations.github.github_host.run_with_progress") as mock:
            mock.return_value = MagicMock(
                returncode=0, stdout="https://github.com/o/r/pull/1\n"
            )
            host = GitHubCodeHost()
            url = await host.create_pr("o/r", "fix/1-bug", "Fix", "Body")
            assert "pull" in url

    @pytest.mark.asyncio
    async def test_comment_on_issue(self):
        with patch("integrations.github.github_host.run_with_progress") as mock:
            mock.return_value = MagicMock(returncode=0)
            host = GitHubCodeHost()
            await host.comment_on_issue("o/r", 42, "comment")
            mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_push_changes_fail(self):
        with patch("integrations.github.github_host.run_with_progress") as mock:
            mock.return_value = MagicMock(returncode=1, stderr="error")
            host = GitHubCodeHost()
            assert not await host.push_changes("o/r", "branch")


# -- Ranker --

class TestIssueRanker:
    @pytest.mark.asyncio
    async def test_single_issue_no_llm(self):
        ranker = IssueRanker()
        issues = [NormalizedIssue(number=1, title="A")]
        result = await ranker.rank(issues)
        assert result == issues

    @pytest.mark.asyncio
    async def test_empty_list(self):
        ranker = IssueRanker()
        result = await ranker.rank([])
        assert result == []

    @pytest.mark.asyncio
    async def test_fallback_chronological(self):
        ranker = IssueRanker(llm_config=None)
        issues = [
            NormalizedIssue(number=1, title="Old",
                            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
            NormalizedIssue(number=2, title="New",
                            created_at=datetime(2026, 6, 1, tzinfo=timezone.utc)),
        ]
        result = await ranker.rank(issues)
        assert result[0].number == 1
        assert result[1].number == 2
