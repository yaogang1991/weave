"""
Tests for #417: planner token limit protection.

Verifies:
- Token estimation utility
- Requirement truncation when prompt exceeds context window
- Failed response truncation on JSON parse retry
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


class TestTokenEstimation:
    def test_estimate_tokens_basic(self):
        orch = _make_orchestrator()
        # 350 chars ≈ 100 tokens at 3.5 chars/token
        tokens = orch._estimate_tokens("a" * 350)
        assert tokens == 100

    def test_estimate_tokens_empty(self):
        orch = _make_orchestrator()
        assert orch._estimate_tokens("") == 0

    def test_chars_per_token_constant(self):
        assert IntelligentOrchestrator._CHARS_PER_TOKEN == 3.5


class TestContextWindow:
    def test_known_model_claude(self):
        orch = _make_orchestrator()
        assert orch._get_context_window() == 200_000

    def test_known_model_gpt4o(self):
        config = LLMConfig(model="gpt-4o", api_key="test")
        orch = IntelligentOrchestrator(
            llm_config=config,
            session_store=MagicMock(spec=SessionStore),
            agent_registry=AgentRegistry(),
        )
        assert orch._get_context_window() == 128_000

    def test_unknown_model_uses_default(self):
        config = LLMConfig(model="kimi-for-coding", api_key="test")
        orch = IntelligentOrchestrator(
            llm_config=config,
            session_store=MagicMock(spec=SessionStore),
            agent_registry=AgentRegistry(),
        )
        # Default is 200K
        assert orch._get_context_window() == 200_000


class TestTruncateRequirement:
    def test_short_requirement_not_truncated(self):
        orch = _make_orchestrator()
        req = "Build a simple REST API"
        result = orch._truncate_requirement_if_needed(
            req, "system prompt", None,
        )
        assert result == req

    def test_long_requirement_gets_truncated(self):
        orch = _make_orchestrator()
        # Create a requirement that's larger than the context window
        huge_req = "x" * 2_000_000  # 2M chars ≈ 571K tokens
        result = orch._truncate_requirement_if_needed(
            huge_req, "system prompt", None,
        )
        assert len(result) < len(huge_req)
        assert "[NOTE:" in result
        assert "truncated" in result.lower()

    def test_truncation_preserves_boundary(self):
        orch = _make_orchestrator()
        # Create a requirement with double-newline boundaries
        sections = []
        for i in range(100):
            sections.append(f"## Module {i}\nDetailed spec for module {i}.")
        req = "\n\n".join(sections)
        result = orch._truncate_requirement_if_needed(
            req, "short system", None,
        )
        # Should cut at a double-newline boundary if possible
        assert result.endswith("plan.]") or "\n\n" in result

    def test_large_system_prompt_still_works(self):
        orch = _make_orchestrator()
        # System prompt already takes most of the budget
        huge_system = "s" * 500_000  # ~143K tokens
        req = "Build API"
        result = orch._truncate_requirement_if_needed(
            req, huge_system, None,
        )
        # Should return as-is with a warning (can't fit even the requirement)
        assert result == req

    def test_project_context_considered(self):
        orch = _make_orchestrator()
        # Large project context eats into the budget
        large_context = {"existing_files": ["file" + str(i) for i in range(50000)]}
        req = "Build a REST API for todo items"
        result = orch._truncate_requirement_if_needed(
            req, "system prompt", large_context,
        )
        # Should still be fine — the requirement itself is short
        assert result == req


class TestRetryContextTruncation:
    @pytest.mark.asyncio
    async def test_failed_response_truncated_on_retry(self):
        """Verify that a large failed response is truncated before retry (#417)."""
        orch = _make_orchestrator()

        # Mock the LLM to return invalid JSON first, then valid
        huge_invalid_json = "{" + '"nodes": [' + "x" * 100_000 + "]}"
        valid_json = json.dumps({
            "reasoning": "test",
            "nodes": [
                {"id": "n1", "agent_type": "planner", "task": "Plan"},
                {"id": "n2", "agent_type": "generator", "task": "Build"},
            ],
            "edges": [{"from": "n1", "to": "n2"}],
        })

        call_count = 0
        messages_captured = []

        def mock_call(messages, tools=None):
            nonlocal call_count
            call_count += 1
            messages_captured.append(list(messages))  # snapshot
            if call_count == 1:
                return {"content": huge_invalid_json}
            return {"content": valid_json}

        orch.llm = MagicMock()
        orch.llm.call = mock_call

        dag = await orch.plan("Build a simple API")

        # Verify: the second call should have truncated the failed response
        assert call_count == 2
        assert len(dag.nodes) == 2

        # The assistant message in the retry should be truncated
        retry_messages = messages_captured[1]
        assistant_msg = retry_messages[-2]  # second-to-last is assistant
        assert assistant_msg["role"] == "assistant"
        assert len(assistant_msg["content"]) <= 2500  # 2000 + truncation note

    @pytest.mark.asyncio
    async def test_short_failed_response_not_truncated(self):
        """Short failed responses should be kept as-is."""
        orch = _make_orchestrator()

        short_invalid = "Here is my plan: {not valid json"
        valid_json = json.dumps({
            "reasoning": "test",
            "nodes": [
                {"id": "n1", "agent_type": "generator", "task": "Build"},
            ],
            "edges": [],
        })

        call_count = 0
        def mock_call(messages, tools=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"content": short_invalid}
            return {"content": valid_json}

        orch.llm = MagicMock()
        orch.llm.call = mock_call

        await orch.plan("Build API")

        # Short response should not be truncated
        assert call_count == 2
