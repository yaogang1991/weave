"""Tests for worker-memory integration (#481)."""
from unittest.mock import MagicMock

from agent.worker import AgentWorker
from core.config import LLMConfig


def _make_worker(**kwargs):
    """Create an AgentWorker with mocked dependencies."""
    config = LLMConfig(model="test-model")
    store = MagicMock()
    return AgentWorker(config, store, **kwargs)


class TestMemoryInjection:
    """Memory context is injected at worker run start (#481)."""

    def test_memory_injected_when_available(self):
        """Memory entries are injected as a user message before the loop."""
        mock_memory = MagicMock()
        mock_memory.config.enabled = True
        mock_memory.get_context_for_agent.return_value = [
            MagicMock(content="Previous session learned X"),
        ]
        mock_memory.format_memory_prompt.return_value = (
            "## Relevant memory:\n- Previous session learned X"
        )
        mock_memory.config.auto_store = True

        worker = _make_worker(memory_manager=mock_memory)

        # Mock LLM to return no tool calls (immediate exit)
        worker.llm.call = MagicMock(return_value={
            "role": "assistant",
            "content": "done",
        })

        list(worker.run(
            session_id="test-session",
            system_prompt="you are a helper",
            user_message="build a function",
            tools=[],
            tool_executor=MagicMock(),
        ))

        # Memory retrieval was called
        mock_memory.get_context_for_agent.assert_called_once_with(
            agent_type="shared",
            task_description="build a function",
            session_id="test-session",
        )
        # format_memory_prompt was called with the entries
        mock_memory.format_memory_prompt.assert_called_once()

    def test_no_injection_when_disabled(self):
        """Memory injection skipped when config.enabled=False."""
        mock_memory = MagicMock()
        mock_memory.config.enabled = False

        worker = _make_worker(memory_manager=mock_memory)
        worker.llm.call = MagicMock(return_value={
            "role": "assistant",
            "content": "done",
        })

        list(worker.run(
            session_id="test-session",
            system_prompt="sys",
            user_message="task",
            tools=[],
            tool_executor=MagicMock(),
        ))

        mock_memory.get_context_for_agent.assert_not_called()

    def test_no_injection_when_no_manager(self):
        """No memory injection when memory_manager is None."""
        worker = _make_worker()
        worker.llm.call = MagicMock(return_value={
            "role": "assistant",
            "content": "done",
        })

        # Should not raise
        list(worker.run(
            session_id="test-session",
            system_prompt="sys",
            user_message="task",
            tools=[],
            tool_executor=MagicMock(),
        ))


class TestMemoryPersistence:
    """Key learnings are persisted after worker loop (#481)."""

    def test_persist_called_after_loop(self):
        """extract_and_store is called after the loop completes."""
        mock_memory = MagicMock()
        mock_memory.config.enabled = True
        mock_memory.config.auto_store = True
        mock_memory.get_context_for_agent.return_value = []
        mock_memory.format_memory_prompt.return_value = ""

        worker = _make_worker(memory_manager=mock_memory)
        worker.llm.call = MagicMock(return_value={
            "role": "assistant",
            "content": "done",
        })

        list(worker.run(
            session_id="test-session",
            system_prompt="sys",
            user_message="build auth",
            tools=[],
            tool_executor=MagicMock(),
        ))

        mock_memory.extract_and_store.assert_called_once()
        call_kwargs = mock_memory.extract_and_store.call_args
        assert call_kwargs.kwargs["agent_type"] == "shared"
        assert call_kwargs.kwargs["session_id"] == "test-session"
        assert "build auth" in call_kwargs.kwargs["task_description"]

    def test_persist_skipped_when_auto_store_disabled(self):
        """No persistence when auto_store=False."""
        mock_memory = MagicMock()
        mock_memory.config.enabled = True
        mock_memory.config.auto_store = False
        mock_memory.get_context_for_agent.return_value = []

        worker = _make_worker(memory_manager=mock_memory)
        worker.llm.call = MagicMock(return_value={
            "role": "assistant",
            "content": "done",
        })

        list(worker.run(
            session_id="test-session",
            system_prompt="sys",
            user_message="task",
            tools=[],
            tool_executor=MagicMock(),
        ))

        mock_memory.extract_and_store.assert_not_called()

    def test_persist_failure_does_not_crash(self):
        """If extract_and_store raises, worker still completes normally."""
        mock_memory = MagicMock()
        mock_memory.config.enabled = True
        mock_memory.config.auto_store = True
        mock_memory.get_context_for_agent.return_value = []
        mock_memory.format_memory_prompt.return_value = ""
        mock_memory.extract_and_store.side_effect = RuntimeError("db error")

        worker = _make_worker(memory_manager=mock_memory)
        worker.llm.call = MagicMock(return_value={
            "role": "assistant",
            "content": "done",
        })

        # Should not raise despite memory persistence failure
        results = list(worker.run(
            session_id="test-session",
            system_prompt="sys",
            user_message="task",
            tools=[],
            tool_executor=MagicMock(),
        ))
        assert len(results) == 1
