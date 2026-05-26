"""Tests for integrations/github/pr_body.py (M5.3)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from integrations.models import NormalizedIssue


class TestGetDiffStat:
    @pytest.mark.asyncio
    async def test_returns_stdout(self):
        with patch("integrations.github.pr_body.run_with_progress") as mock:
            mock.return_value = MagicMock(returncode=0, stdout=" 3 files changed")
            from integrations.github.pr_body import get_diff_stat
            result = await get_diff_stat("/tmp/work")
            assert result == "3 files changed"

    @pytest.mark.asyncio
    async def test_empty_diff_falls_back(self):
        with patch("integrations.github.pr_body.run_with_progress") as mock:
            mock.side_effect = [
                MagicMock(returncode=0, stdout=""),
                MagicMock(returncode=0, stdout="2 files changed"),
            ]
            from integrations.github.pr_body import get_diff_stat
            result = await get_diff_stat("/tmp/work")
            assert result == "2 files changed"

    @pytest.mark.asyncio
    async def test_failed_returns_empty(self):
        with patch("integrations.github.pr_body.run_with_progress") as mock:
            mock.return_value = MagicMock(returncode=1, stderr="error")
            from integrations.github.pr_body import get_diff_stat
            result = await get_diff_stat("/tmp/work")
            assert result == ""


class TestGetFullDiff:
    @pytest.mark.asyncio
    async def test_truncates_long_diff(self):
        with patch("integrations.github.pr_body.run_with_progress") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="x" * 20000)
            from integrations.github.pr_body import get_full_diff
            result = await get_full_diff("/tmp/work", max_chars=1000)
            assert len(result) == 1000

    @pytest.mark.asyncio
    async def test_no_truncation_under_limit(self):
        with patch("integrations.github.pr_body.run_with_progress") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="short diff")
            from integrations.github.pr_body import get_full_diff
            result = await get_full_diff("/tmp/work")
            assert result == "short diff"

    @pytest.mark.asyncio
    async def test_failed_returns_empty(self):
        with patch("integrations.github.pr_body.run_with_progress") as mock:
            mock.return_value = MagicMock(returncode=1, stderr="error")
            from integrations.github.pr_body import get_full_diff
            result = await get_full_diff("/tmp/work")
            assert result == ""


class TestGenerateLlmReview:
    @pytest.mark.asyncio
    async def test_success(self):
        mock_config = MagicMock()
        mock_client = MagicMock()
        mock_client.call.return_value = {"content": "Summary of changes."}
        with patch("core.llm_client.LLMClient", return_value=mock_client):
            with patch("core.config.LLMConfig", return_value=MagicMock()):
                from integrations.github.pr_body import generate_llm_review
                result = await generate_llm_review("some diff", mock_config)
                assert result == "Summary of changes."

    @pytest.mark.asyncio
    async def test_empty_diff_returns_empty(self):
        from integrations.github.pr_body import generate_llm_review
        result = await generate_llm_review("", MagicMock())
        assert result == ""

    @pytest.mark.asyncio
    async def test_llm_failure_returns_empty(self):
        with patch("core.config.LLMConfig", side_effect=Exception("boom")):
            from integrations.github.pr_body import generate_llm_review
            result = await generate_llm_review("some diff", MagicMock())
            assert result == ""


class TestGeneratePrBody:
    @pytest.mark.asyncio
    async def test_with_review(self):
        issue = NormalizedIssue(number=42, title="Fix bug", repo="o/r")
        with patch("integrations.github.pr_body.get_diff_stat",
                   return_value="2 files changed"):
            with patch("integrations.github.pr_body.get_full_diff",
                       return_value="diff text"):
                with patch("integrations.github.pr_body.generate_llm_review",
                           return_value="Looks good."):
                    from integrations.github.pr_body import generate_pr_body
                    body = await generate_pr_body("/tmp/work", issue, MagicMock())
                    assert "Fix #42" in body
                    assert "Fix bug" in body
                    assert "2 files changed" in body
                    assert "Code Review" in body
                    assert "Looks good." in body
                    assert "Fixes #42" in body

    @pytest.mark.asyncio
    async def test_without_llm_config(self):
        issue = NormalizedIssue(number=1, title="Test")
        with patch("integrations.github.pr_body.get_diff_stat",
                   return_value="1 file changed"):
            from integrations.github.pr_body import generate_pr_body
            body = await generate_pr_body("/tmp/work", issue, llm_config=None)
            assert "Fix #1" in body
            assert "Code Review" not in body
            assert "Fixes #1" in body

    @pytest.mark.asyncio
    async def test_enhancement_prefix(self):
        issue = NormalizedIssue(number=10, title="Add feature", repo="o/r",
                                labels=["enhancement"])
        with patch("integrations.github.pr_body.get_diff_stat",
                   return_value="1 file changed"):
            from integrations.github.pr_body import generate_pr_body
            body = await generate_pr_body("/tmp/work", issue, llm_config=None)
            assert "Feat #10" in body
            assert "Fixes #10" in body

    @pytest.mark.asyncio
    async def test_review_failure_falls_back(self):
        issue = NormalizedIssue(number=3, title="T")
        with patch("integrations.github.pr_body.get_diff_stat",
                   return_value="1 file"):
            with patch("integrations.github.pr_body.get_full_diff",
                       return_value="diff"):
                with patch("integrations.github.pr_body.generate_llm_review",
                           return_value=""):
                    from integrations.github.pr_body import generate_pr_body
                    body = await generate_pr_body("/tmp/work", issue, MagicMock())
                    assert "Code Review" not in body

    @pytest.mark.asyncio
    async def test_execution_summary_section(self):
        issue = NormalizedIssue(number=5, title="Add feature")
        execution_summary = {
            "nodes_total": 3,
            "nodes_completed": 3,
            "tokens_in": 10000,
            "tokens_out": 5000,
            "duration_sec": 125.3,
        }
        with patch("integrations.github.pr_body.get_diff_stat",
                   return_value="2 files changed"):
            from integrations.github.pr_body import generate_pr_body
            body = await generate_pr_body(
                "/tmp/work", issue, llm_config=None,
                execution_summary=execution_summary,
            )
            assert "## Execution" in body
            assert "3/3 completed" in body
            assert "10000in / 5000out" in body
            assert "2m 5s" in body

    @pytest.mark.asyncio
    async def test_execution_summary_with_test_results(self):
        issue = NormalizedIssue(number=6, title="Fix test")
        execution_summary = {
            "nodes_total": 2,
            "nodes_completed": 2,
            "tokens_in": 5000,
            "tokens_out": 2000,
            "duration_sec": 45,
            "test_summary": "12 passed, 1 failed",
            "lint_summary": "0 errors",
        }
        with patch("integrations.github.pr_body.get_diff_stat",
                   return_value="1 file changed"):
            from integrations.github.pr_body import generate_pr_body
            body = await generate_pr_body(
                "/tmp/work", issue, llm_config=None,
                execution_summary=execution_summary,
            )
            assert "## Results" in body
            assert "12 passed, 1 failed" in body
            assert "0 errors" in body


class TestFormatDuration:
    def test_seconds_only(self):
        from integrations.github.pr_body import _format_duration
        assert _format_duration(30) == "30s"
        assert _format_duration(59) == "59s"

    def test_minutes_and_seconds(self):
        from integrations.github.pr_body import _format_duration
        assert _format_duration(125) == "2m 5s"
        assert _format_duration(3600) == "60m 0s"
