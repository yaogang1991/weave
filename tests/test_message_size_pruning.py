"""
Tests for #419: planner message size pruning.

Verifies:
- Message byte estimation
- Pruning strategy (assistant truncation, user truncation, message dropping)
- Integration with plan() to prevent 2M byte limit exceeded
"""
import json
import pytest
from unittest.mock import MagicMock, patch

from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
from core.config import LLMConfig
from core.agent_registry import AgentRegistry
from session.store import SessionStore


def _make_orchestrator() -> IntelligentOrchestrator:
    config = LLMConfig(model="claude-sonnet-4-6", api_key="test-key")
    store = MagicMock(spec=SessionStore)
    registry = AgentRegistry()
    return IntelligentOrchestrator(
        llm_config=config,
        session_store=store,
        agent_registry=registry,
    )


class TestEstimateMessagesBytes:
    def test_empty_messages(self):
        orch = _make_orchestrator()
        assert orch._estimate_messages_bytes([]) == 0

    def test_single_message(self):
        orch = _make_orchestrator()
        messages = [{"role": "user", "content": "hello"}]
        size = orch._estimate_messages_bytes(messages)
        # "hello" = 5 bytes + ~50 overhead
        assert 50 < size < 100

    def test_unicode_content(self):
        orch = _make_orchestrator()
        messages = [{"role": "user", "content": "你好世界"}]
        size = orch._estimate_messages_bytes(messages)
        # 4 CJK chars = 12 bytes in UTF-8 + overhead
        assert size > 50

    def test_large_content(self):
        orch = _make_orchestrator()
        messages = [{"role": "assistant", "content": "x" * 1_000_000}]
        size = orch._estimate_messages_bytes(messages)
        assert size > 1_000_000


class TestPruneMessages:
    def test_small_messages_not_pruned(self):
        orch = _make_orchestrator()
        messages = [
            {"role": "system", "content": "You are a planner."},
            {"role": "user", "content": "Build a REST API"},
        ]
        result = orch._prune_messages_for_size(messages)
        assert len(result) == 2
        assert result[0]["content"] == "You are a planner."

    def test_large_assistant_message_truncated(self):
        orch = _make_orchestrator()
        messages = [
            {"role": "system", "content": "system"},
            {"role": "assistant", "content": "x" * 2_000_000},
            {"role": "user", "content": "retry please"},
        ]
        result = orch._prune_messages_for_size(messages)
        # Assistant message should be truncated to ~2000 chars
        assert len(result[1]["content"]) <= 2100
        assert "truncated" in result[1]["content"]

    def test_preserves_system_and_last_user(self):
        orch = _make_orchestrator()
        messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "assistant", "content": "a" * 2_000_000},
            {"role": "user", "content": "u" * 2_000_000},
        ]
        result = orch._prune_messages_for_size(messages)
        # System prompt must always be preserved
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "system prompt"

    def test_drops_intermediate_messages_when_needed(self):
        orch = _make_orchestrator()
        messages = [
            {"role": "system", "content": "s"},
            {"role": "assistant", "content": "a" * 1_500_000},
            {"role": "user", "content": "u" * 1_500_000},
        ]
        result = orch._prune_messages_for_size(messages)
        # Should still have messages (maybe truncated or dropped)
        total = orch._estimate_messages_bytes(result)
        max_allowed = int(orch._MAX_MESSAGE_BYTES * orch._PRUNE_THRESHOLD)
        assert total <= max_allowed or len(result) == 2

    def test_does_not_mutate_original(self):
        orch = _make_orchestrator()
        original = [
            {"role": "system", "content": "s"},
            {"role": "assistant", "content": "a" * 2_000_000},
            {"role": "user", "content": "retry"},
        ]
        original_copy = [dict(m) for m in original]
        orch._prune_messages_for_size(original)
        # Original messages should not be modified
        for i, msg in enumerate(original):
            assert msg["content"] == original_copy[i]["content"]


class TestConstants:
    def test_max_message_bytes(self):
        assert IntelligentOrchestrator._MAX_MESSAGE_BYTES == 2_097_152

    def test_prune_threshold(self):
        assert IntelligentOrchestrator._PRUNE_THRESHOLD == 0.80


class TestPlanIntegration:
    @pytest.mark.asyncio
    async def test_plan_prunes_on_retry(self):
        """Verify pruning happens during plan() retries (#419)."""
        orch = _make_orchestrator()

        # Create a huge invalid response followed by a valid one
        huge_invalid = "{" + '"nodes": [' + "x" * 500_000 + "]}"
        valid_json = json.dumps({
            "reasoning": "test",
            "nodes": [
                {"id": "n1", "agent_type": "generator", "task": "Build"},
            ],
            "edges": [],
        })

        call_count = 0
        captured_messages = []

        def mock_call(messages, tools=None):
            nonlocal call_count
            call_count += 1
            captured_messages.append([dict(m) for m in messages])
            if call_count == 1:
                return {"content": huge_invalid}
            return {"content": valid_json}

        orch.llm = MagicMock()
        orch.llm.call = mock_call

        dag = await orch.plan("Build API")

        assert call_count == 2
        # The second call's messages should have been pruned
        # (the assistant message should be truncated, not 500K chars)
        retry_messages = captured_messages[1]
        assistant_msgs = [
            m for m in retry_messages if m["role"] == "assistant"
        ]
        if assistant_msgs:
            assert len(assistant_msgs[-1]["content"]) <= 2100
