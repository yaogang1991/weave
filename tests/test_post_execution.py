"""Tests for integrations/github/post_execution.py (M5.3)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from control_plane.models import RunStatus


def _make_run(status=RunStatus.SUCCEEDED, dag_result=None):
    run = MagicMock()
    run.status = status
    run.dag_result = dag_result or {"work_dir": "/tmp/work"}
    return run


METADATA = {
    "issue_number": 42,
    "requirement": "Fix login bug",
    "branch_name": "fix/42-login",
    "repo": "o/r",
    "issue_url": "https://github.com/o/r/issues/42",
}


class TestHandleResult:
    @pytest.mark.asyncio
    async def test_no_output(self):
        run = _make_run()
        host = MagicMock()
        with patch(
            "integrations.github.post_execution._has_changes",
            return_value=False,
        ):
            from integrations.github.post_execution import handle_result

            result = await handle_result(run, METADATA, host)
        assert result.status == "no_output"
        assert "no code changes" in result.issue_comment.lower()

    @pytest.mark.asyncio
    async def test_full_success(self):
        run = _make_run(status=RunStatus.SUCCEEDED)
        host = MagicMock()
        host.push_changes = AsyncMock(return_value=True)
        host.create_pr = AsyncMock(return_value="https://github.com/o/r/pull/99")
        with patch(
            "integrations.github.post_execution._has_changes",
            return_value=True,
        ), patch(
            "integrations.github.post_execution._commit_changes",
            return_value=True,
        ), patch(
            "integrations.github.post_execution.generate_pr_body",
            return_value="PR body",
        ):
            from integrations.github.post_execution import handle_result

            result = await handle_result(run, METADATA, host)
        assert result.status == "success"
        assert result.pr_url == "https://github.com/o/r/pull/99"
        host.create_pr.assert_awaited_once()
        _, kwargs = host.create_pr.call_args
        assert kwargs["draft"] is False

    @pytest.mark.asyncio
    async def test_partial_success(self):
        run = _make_run(
            status=RunStatus.FAILED,
            dag_result={"work_dir": "/tmp/work", "error": "eval failed"},
        )
        host = MagicMock()
        host.push_changes = AsyncMock(return_value=True)
        host.create_pr = AsyncMock(return_value="https://github.com/o/r/pull/100")
        with patch(
            "integrations.github.post_execution._has_changes",
            return_value=True,
        ), patch(
            "integrations.github.post_execution._commit_changes",
            return_value=True,
        ), patch(
            "integrations.github.post_execution.generate_pr_body",
            return_value="PR body",
        ):
            from integrations.github.post_execution import handle_result

            result = await handle_result(run, METADATA, host)
        assert result.status == "partial"
        assert "draft PR" in result.issue_comment
        _, kwargs = host.create_pr.call_args
        assert kwargs["draft"] is True

    @pytest.mark.asyncio
    async def test_push_failed(self):
        run = _make_run()
        host = MagicMock()
        host.push_changes = AsyncMock(return_value=False)
        with patch(
            "integrations.github.post_execution._has_changes",
            return_value=True,
        ), patch(
            "integrations.github.post_execution._commit_changes",
            return_value=True,
        ), patch(
            "integrations.github.post_execution.generate_pr_body",
            return_value="PR body",
        ):
            from integrations.github.post_execution import handle_result

            result = await handle_result(run, METADATA, host)
        assert result.status == "push_failed"
        assert "push failed" in result.issue_comment.lower()

    @pytest.mark.asyncio
    async def test_commit_failed(self):
        run = _make_run()
        host = MagicMock()
        with patch(
            "integrations.github.post_execution._has_changes",
            return_value=True,
        ), patch(
            "integrations.github.post_execution._commit_changes",
            return_value=False,
        ):
            from integrations.github.post_execution import handle_result

            result = await handle_result(run, METADATA, host)
        assert result.status == "push_failed"
        assert "commit" in result.issue_comment.lower()

    @pytest.mark.asyncio
    async def test_no_work_dir(self):
        run = MagicMock()
        run.dag_result = {}
        host = MagicMock()
        from integrations.github.post_execution import handle_result

        result = await handle_result(run, METADATA, host)
        assert result.status == "push_failed"
        assert "work directory" in result.issue_comment.lower()
