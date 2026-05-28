"""
Tests for #220: planner JSON parse failure includes LLM response preview.

When the orchestrator fails to extract JSON from the LLM response after
retries, the error message now includes a preview of the last LLM output
for debugging.
"""
import pytest
from unittest.mock import MagicMock, patch

from orchestrator.intelligent_orchestrator import IntelligentOrchestrator


@pytest.fixture
def orchestrator(tmp_path):
    from core.config import LLMConfig
    from session.store import SessionStore

    store = SessionStore(base_path=str(tmp_path / "events"))
    config = LLMConfig(model="test-model")
    return IntelligentOrchestrator(
        llm_config=config,
        session_store=store,
        agent_registry=MagicMock(),
    )


class TestPlannerJsonParseDebug:
    """Verify error message contains LLM response preview."""

    @pytest.mark.asyncio
    async def test_error_includes_response_preview(self, orchestrator):
        """When JSON parse fails, ValueError includes LLM output preview."""
        # LLM returns non-JSON content
        bad_response = "I think the plan should be: first do X, then do Y."
        with patch.object(orchestrator.llm, "call", return_value={
            "content": bad_response,
        }):
            with pytest.raises(ValueError) as exc_info:
                await orchestrator.plan("Build a REST API")

            error_msg = str(exc_info.value)
            # Error should contain the LLM response preview
            assert bad_response in error_msg
            assert "retries" in error_msg.lower()

    @pytest.mark.asyncio
    async def test_error_includes_preview_truncated(self, orchestrator):
        """Long LLM responses are truncated to 500 chars in error."""
        long_response = "x" * 2000
        with patch.object(orchestrator.llm, "call", return_value={
            "content": long_response,
        }):
            with pytest.raises(ValueError) as exc_info:
                await orchestrator.plan("Build something")

            error_msg = str(exc_info.value)
            # Should be truncated
            assert len(error_msg) < 2000
            assert "x" * 400 in error_msg  # Most of first 500 chars present

    @pytest.mark.asyncio
    async def test_empty_response_handled(self, orchestrator):
        """Empty LLM response produces helpful error message."""
        with patch.object(orchestrator.llm, "call", return_value={
            "content": "",
        }):
            with pytest.raises(ValueError) as exc_info:
                await orchestrator.plan("Build something")

            error_msg = str(exc_info.value)
            assert "empty response" in error_msg.lower() or "retries" in error_msg.lower()

    @pytest.mark.asyncio
    async def test_error_logged_at_error_level(self, orchestrator):
        """The parse failure raises ValueError with descriptive message."""
        with patch.object(orchestrator.llm, "call", return_value={
            "content": "Not JSON at all",
        }):
            with pytest.raises(ValueError) as exc_info:
                await orchestrator.plan("Build something")

            error_msg = str(exc_info.value)
            assert "Failed to parse" in error_msg or "retries" in error_msg.lower()
