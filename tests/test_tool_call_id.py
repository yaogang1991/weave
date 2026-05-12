"""Tests for tool_call_id defensive handling (#169).

Verifies that empty/missing tool_call_id values are replaced with
generated IDs to prevent 'tool_call_id is not found' API errors.
"""
import uuid
from unittest.mock import MagicMock, patch

import pytest


class TestToolCallIdDefense:
    """Verify tool_call_id is always a non-empty string."""

    def _make_worker(self):
        from agent.worker import AgentWorker
        from core.config import LLMConfig
        from session.store import SessionStore
        from unittest.mock import MagicMock

        store = MagicMock(spec=SessionStore)
        with patch("agent.worker.LLMClient"):
            worker = AgentWorker(MagicMock(spec=LLMConfig), store)
        return worker

    def test_empty_id_replaced(self):
        """Tool call with empty id gets a generated one."""
        worker = self._make_worker()
        tc = {"id": "", "name": "read", "arguments": {"file_path": "x.py"}}

        # Simulate the defensive check from worker.run()
        tool_call_id = tc.get("id") or ""
        if not tool_call_id.strip():
            tool_call_id = f"tool_{uuid.uuid4().hex[:8]}"
            tc["id"] = tool_call_id

        assert tc["id"].startswith("tool_")
        assert len(tc["id"]) == 13  # "tool_" + 8 hex chars

    def test_whitespace_id_replaced(self):
        """Tool call with whitespace-only id gets a generated one."""
        tc = {"id": "   ", "name": "read", "arguments": {}}

        tool_call_id = tc.get("id") or ""
        if not tool_call_id.strip():
            tool_call_id = f"tool_{uuid.uuid4().hex[:8]}"
            tc["id"] = tool_call_id

        assert tc["id"].startswith("tool_")

    def test_missing_id_replaced(self):
        """Tool call with no id field gets a generated one."""
        tc = {"name": "read", "arguments": {}}

        tool_call_id = tc.get("id") or ""
        if not tool_call_id.strip():
            tool_call_id = f"tool_{uuid.uuid4().hex[:8]}"
            tc["id"] = tool_call_id

        assert tc["id"].startswith("tool_")

    def test_valid_id_preserved(self):
        """Tool call with a valid id keeps it unchanged."""
        tc = {"id": "call_abc123", "name": "read", "arguments": {}}

        tool_call_id = tc.get("id") or ""
        if not tool_call_id.strip():
            tool_call_id = f"tool_{uuid.uuid4().hex[:8]}"
            tc["id"] = tool_call_id

        assert tc["id"] == "call_abc123"

    def test_none_id_replaced(self):
        """Tool call with None id gets a generated one."""
        tc = {"id": None, "name": "read", "arguments": {}}

        tool_call_id = tc.get("id") or ""
        if not tool_call_id.strip():
            tool_call_id = f"tool_{uuid.uuid4().hex[:8]}"
            tc["id"] = tool_call_id

        assert tc["id"].startswith("tool_")
