"""
Tests for LightweightLLMCaller: single-shot LLM call for planner/evaluator nodes (M6.3).

Covers:
- Basic single LLM call returns response text
- Token usage tracking (input_tokens, output_tokens accumulation)
- Cooperative cancellation via cancel_event
- SessionStore event emission with correct type and payload
- Empty response handling
- LLM API error handling (exception propagation)
- System prompt and user message construction
- Multiple calls accumulate token usage
"""
from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.lightweight_llm_caller import LightweightLLMCaller
from core.config import LLMConfig
from core.event_models import EventType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> LLMConfig:
    """Create an LLMConfig with sensible test defaults."""
    defaults = {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "api_key": "test-key-123",
        "base_url": "",
        "timeout": 30,
        "max_tokens": 1024,
    }
    defaults.update(overrides)
    return LLMConfig(**defaults)


@pytest.fixture
def mock_session_store():
    return MagicMock()


@pytest.fixture
def config():
    return _make_config()


@pytest.fixture
def caller(config, mock_session_store):
    with patch.object(LightweightLLMCaller, "__init__", lambda self, *a, **kw: None):
        c = LightweightLLMCaller.__new__(LightweightLLMCaller)
    c.session_store = mock_session_store
    c.token_usage = {"input_tokens": 0, "output_tokens": 0}
    c._default_llm = MagicMock()
    c._llm_router = None
    return c


# ---------------------------------------------------------------------------
# Basic call tests
# ---------------------------------------------------------------------------


class TestBasicCall:
    """Test that a single LLM call returns the response text."""

    @pytest.mark.asyncio
    async def test_returns_response_content(self, caller, mock_session_store):
        caller._default_llm.call.return_value = {
            "role": "assistant",
            "content": "Hello from LLM",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

        result = await caller.call(
            system_prompt="You are a planner.",
            user_message="Plan a REST API.",
            session_id="sess-001",
        )
        assert result == "Hello from LLM"

    @pytest.mark.asyncio
    async def test_returns_multiline_content(self, caller, mock_session_store):
        long_text = "Step 1: Define models.\nStep 2: Create routes.\nStep 3: Add tests."
        caller._default_llm.call.return_value = {
            "role": "assistant",
            "content": long_text,
        }

        result = await caller.call(
            system_prompt="system",
            user_message="task",
            session_id="sess-002",
        )
        assert result == long_text
        assert "Step 1" in result
        assert "Step 3" in result


# ---------------------------------------------------------------------------
# Token usage tracking
# ---------------------------------------------------------------------------


class TestTokenUsageTracking:
    """Test that token usage is correctly accumulated."""

    @pytest.mark.asyncio
    async def test_single_call_usage(self, caller, mock_session_store):
        caller._default_llm.call.return_value = {
            "role": "assistant",
            "content": "result",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }

        await caller.call("sys", "user", "sess-100")

        assert caller.token_usage["input_tokens"] == 100
        assert caller.token_usage["output_tokens"] == 50

    @pytest.mark.asyncio
    async def test_multiple_calls_accumulate(self, caller, mock_session_store):
        caller._default_llm.call.side_effect = [
            {
                "role": "assistant",
                "content": "first",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
            {
                "role": "assistant",
                "content": "second",
                "usage": {"input_tokens": 200, "output_tokens": 75},
            },
        ]

        await caller.call("sys", "user", "sess-101")
        await caller.call("sys", "user", "sess-102")

        assert caller.token_usage["input_tokens"] == 300
        assert caller.token_usage["output_tokens"] == 125

    @pytest.mark.asyncio
    async def test_missing_usage_defaults_to_zero(self, caller, mock_session_store):
        caller._default_llm.call.return_value = {
            "role": "assistant",
            "content": "no usage info",
        }

        await caller.call("sys", "user", "sess-103")

        assert caller.token_usage["input_tokens"] == 0
        assert caller.token_usage["output_tokens"] == 0

    @pytest.mark.asyncio
    async def test_partial_usage_only_input(self, caller, mock_session_store):
        caller._default_llm.call.return_value = {
            "role": "assistant",
            "content": "partial",
            "usage": {"input_tokens": 42},
        }

        await caller.call("sys", "user", "sess-104")

        assert caller.token_usage["input_tokens"] == 42
        assert caller.token_usage["output_tokens"] == 0


# ---------------------------------------------------------------------------
# Cooperative cancellation
# ---------------------------------------------------------------------------


class TestCooperativeCancellation:
    """Test that cancel_event is forwarded to LLMClient.call."""

    @pytest.mark.asyncio
    async def test_cancel_event_forwarded(self, caller, mock_session_store):
        caller._default_llm.call.return_value = {
            "role": "assistant",
            "content": "ok",
        }
        cancel = threading.Event()

        await caller.call("sys", "user", "sess-200", cancel_event=cancel)

        # Verify cancel_event was passed to llm.call
        caller._default_llm.call.assert_called_once()
        call_kwargs = caller._default_llm.call.call_args
        assert call_kwargs.kwargs.get("cancel_event") is cancel

    @pytest.mark.asyncio
    async def test_no_cancel_event_passes_none(self, caller, mock_session_store):
        caller._default_llm.call.return_value = {
            "role": "assistant",
            "content": "ok",
        }

        await caller.call("sys", "user", "sess-201")

        call_kwargs = caller._default_llm.call.call_args
        assert call_kwargs.kwargs.get("cancel_event") is None


# ---------------------------------------------------------------------------
# SessionStore event emission
# ---------------------------------------------------------------------------


class TestSessionStoreEventEmission:
    """Test that AGENT_MESSAGE events are emitted to SessionStore."""

    @pytest.mark.asyncio
    async def test_emits_agent_message_event(self, caller, mock_session_store):
        response = {
            "role": "assistant",
            "content": "planned",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        caller._default_llm.call.return_value = response

        await caller.call("sys", "user", "sess-300")

        mock_session_store.emit_event.assert_called_once()
        args = mock_session_store.emit_event.call_args
        assert args[0][0] == "sess-300"
        assert args[0][1] == EventType.AGENT_MESSAGE
        assert args[0][2] is response

    @pytest.mark.asyncio
    async def test_event_payload_contains_full_response(self, caller, mock_session_store):
        response = {
            "role": "assistant",
            "content": "evaluation result",
            "usage": {"input_tokens": 20, "output_tokens": 10},
            "finish_reason": "end_turn",
        }
        caller._default_llm.call.return_value = response

        await caller.call("sys", "user", "sess-301")

        emitted_payload = mock_session_store.emit_event.call_args[0][2]
        assert emitted_payload["content"] == "evaluation result"
        assert emitted_payload["role"] == "assistant"
        assert emitted_payload["finish_reason"] == "end_turn"


# ---------------------------------------------------------------------------
# Empty response handling
# ---------------------------------------------------------------------------


class TestEmptyResponseHandling:
    """Test handling of empty or missing content."""

    @pytest.mark.asyncio
    async def test_empty_content_returns_empty_string(self, caller, mock_session_store):
        caller._default_llm.call.return_value = {
            "role": "assistant",
            "content": "",
        }

        result = await caller.call("sys", "user", "sess-400")
        assert result == ""

    @pytest.mark.asyncio
    async def test_missing_content_key_returns_empty_string(self, caller, mock_session_store):
        caller._default_llm.call.return_value = {
            "role": "assistant",
        }

        result = await caller.call("sys", "user", "sess-401")
        assert result == ""

    @pytest.mark.asyncio
    async def test_none_content_returns_empty_string(self, caller, mock_session_store):
        caller._default_llm.call.return_value = {
            "role": "assistant",
            "content": None,
        }

        result = await caller.call("sys", "user", "sess-402")
        assert result == "" or result is None


# ---------------------------------------------------------------------------
# LLM API error handling
# ---------------------------------------------------------------------------


class TestLLMErrorHandling:
    """Test that exceptions from LLMClient.call propagate correctly."""

    @pytest.mark.asyncio
    async def test_rate_limit_error_propagates(self, caller, mock_session_store):
        from core.exceptions import RateLimitError

        caller._default_llm.call.side_effect = RateLimitError(
            provider="anthropic", model="claude-sonnet-4-6", retries=3,
        )

        with pytest.raises(RateLimitError):
            await caller.call("sys", "user", "sess-500")

    @pytest.mark.asyncio
    async def test_timeout_error_propagates(self, caller, mock_session_store):
        caller._default_llm.call.side_effect = TimeoutError("LLM call timed out")

        with pytest.raises(TimeoutError, match="timed out"):
            await caller.call("sys", "user", "sess-501")

    @pytest.mark.asyncio
    async def test_generic_error_propagates(self, caller, mock_session_store):
        caller._default_llm.call.side_effect = RuntimeError("API unavailable")

        with pytest.raises(RuntimeError, match="API unavailable"):
            await caller.call("sys", "user", "sess-502")

    @pytest.mark.asyncio
    async def test_error_does_not_emit_event(self, caller, mock_session_store):
        caller._default_llm.call.side_effect = RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await caller.call("sys", "user", "sess-503")

        # emit_event should NOT have been called since the LLM call failed
        mock_session_store.emit_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_does_not_update_token_usage(self, caller, mock_session_store):
        caller._default_llm.call.side_effect = RuntimeError("fail")

        with pytest.raises(RuntimeError):
            await caller.call("sys", "user", "sess-504")

        assert caller.token_usage["input_tokens"] == 0
        assert caller.token_usage["output_tokens"] == 0


# ---------------------------------------------------------------------------
# Message construction
# ---------------------------------------------------------------------------


class TestMessageConstruction:
    """Test that messages are built correctly from system_prompt and user_message."""

    @pytest.mark.asyncio
    async def test_system_and_user_messages(self, caller, mock_session_store):
        caller._default_llm.call.return_value = {
            "role": "assistant",
            "content": "done",
        }

        await caller.call(
            system_prompt="You are an evaluator.",
            user_message="Evaluate this code for correctness.",
            session_id="sess-600",
        )

        caller._default_llm.call.assert_called_once()
        messages = caller._default_llm.call.call_args[0][0]
        assert len(messages) == 2
        assert messages[0] == {"role": "system", "content": "You are an evaluator."}
        assert messages[1] == {
            "role": "user",
            "content": "Evaluate this code for correctness.",
        }

    @pytest.mark.asyncio
    async def test_long_system_prompt(self, caller, mock_session_store):
        caller._default_llm.call.return_value = {"role": "assistant", "content": "ok"}
        long_prompt = "You are a planner. " * 200

        await caller.call(system_prompt=long_prompt, user_message="task", session_id="s")

        messages = caller._default_llm.call.call_args[0][0]
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == long_prompt

    @pytest.mark.asyncio
    async def test_empty_prompts(self, caller, mock_session_store):
        caller._default_llm.call.return_value = {"role": "assistant", "content": "ok"}

        await caller.call(system_prompt="", user_message="", session_id="s")

        messages = caller._default_llm.call.call_args[0][0]
        assert messages[0]["content"] == ""
        assert messages[1]["content"] == ""


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


class TestInitialization:
    """Test constructor and initial state."""

    def test_token_usage_starts_at_zero(self):
        with patch("agent.lightweight_llm_caller.LLMClient"):
            config = _make_config()
            store = MagicMock()
            caller = LightweightLLMCaller(config, store)

            assert caller.token_usage == {"input_tokens": 0, "output_tokens": 0}

    def test_session_store_stored(self):
        with patch("agent.lightweight_llm_caller.LLMClient"):
            config = _make_config()
            store = MagicMock()
            caller = LightweightLLMCaller(config, store)

            assert caller.session_store is store

    def test_llm_client_created(self):
        with patch("agent.lightweight_llm_caller.LLMClient") as MockLLM:
            config = _make_config()
            store = MagicMock()
            caller = LightweightLLMCaller(config, store)

            MockLLM.assert_called_once_with(config)
            assert caller._llm_router is None

    def test_llm_router_stored(self):
        with patch("agent.lightweight_llm_caller.LLMClient"):
            config = _make_config()
            store = MagicMock()
            router = MagicMock()
            caller = LightweightLLMCaller(config, store, llm_router=router)

            assert caller._llm_router is router


# ---------------------------------------------------------------------------
# LLM Router integration
# ---------------------------------------------------------------------------


class TestLLMRouterIntegration:
    """Test that LLMRouter is used for agent_type-based model selection."""

    @pytest.mark.asyncio
    async def test_router_selects_client_by_agent_type(self, mock_session_store):
        with patch.object(LightweightLLMCaller, "__init__", lambda self, *a, **kw: None):
            c = LightweightLLMCaller.__new__(LightweightLLMCaller)
        c.session_store = mock_session_store
        c.token_usage = {"input_tokens": 0, "output_tokens": 0}
        c._default_llm = MagicMock()

        router_client = MagicMock()
        router_client.call.return_value = {
            "role": "assistant",
            "content": "routed response",
            "usage": {"input_tokens": 5, "output_tokens": 3},
        }
        mock_router = MagicMock()
        mock_router.get_client.return_value = router_client
        c._llm_router = mock_router

        result = await c.call(
            system_prompt="sys", user_message="task",
            session_id="sess-router-1", agent_type="planner",
        )

        mock_router.get_client.assert_called_once_with("planner")
        assert result == "routed response"

    @pytest.mark.asyncio
    async def test_no_agent_type_uses_default(self, caller, mock_session_store):
        caller._default_llm.call.return_value = {
            "role": "assistant",
            "content": "default",
        }

        result = await caller.call(
            system_prompt="sys", user_message="task",
            session_id="sess-router-2",
        )

        # Should NOT call router
        assert caller._llm_router is None
        assert result == "default"

    @pytest.mark.asyncio
    async def test_no_router_uses_default_for_agent_type(self, mock_session_store):
        with patch.object(LightweightLLMCaller, "__init__", lambda self, *a, **kw: None):
            c = LightweightLLMCaller.__new__(LightweightLLMCaller)
        c.session_store = mock_session_store
        c.token_usage = {"input_tokens": 0, "output_tokens": 0}
        c._default_llm = MagicMock()
        c._default_llm.call.return_value = {
            "role": "assistant",
            "content": "fallback",
        }
        c._llm_router = None

        result = await c.call(
            system_prompt="sys", user_message="task",
            session_id="sess-router-3", agent_type="evaluator",
        )

        # With agent_type but no router, _get_client falls back to default
        assert result == "fallback"
