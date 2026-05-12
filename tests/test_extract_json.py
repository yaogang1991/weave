"""
Tests for IntelligentOrchestrator._extract_json — JSON extraction
strategies from LLM responses (code blocks, raw JSON, truncated).
"""
import pytest

from orchestrator.intelligent_orchestrator import IntelligentOrchestrator


@pytest.fixture
def orchestrator():
    """Create an IntelligentOrchestrator with minimal config."""
    from core.config import LLMConfig
    from core.agent_registry import AgentRegistry
    from session.store import SessionStore
    return IntelligentOrchestrator(
        llm_config=LLMConfig(),
        session_store=SessionStore(base_path="/tmp/test_events"),
        agent_registry=AgentRegistry(),
    )


class TestCodeBlockJSON:
    def test_json_block(self, orchestrator):
        text = '```json\n{"nodes": [], "edges": []}\n```'
        assert orchestrator._extract_json(text) == {"nodes": [], "edges": []}

    def test_json_block_with_surrounding_text(self, orchestrator):
        text = 'Here is the plan:\n```json\n{"nodes": [], "edges": []}\n```\nDone.'
        assert orchestrator._extract_json(text) == {"nodes": [], "edges": []}

    def test_generic_code_block_with_json(self, orchestrator):
        text = '```\n{"nodes": [], "edges": []}\n```'
        assert orchestrator._extract_json(text) == {"nodes": [], "edges": []}

    def test_code_block_invalid_json_falls_through(self, orchestrator):
        """Invalid JSON in code block should fall through to other strategies."""
        text = '```json\n{bad json}\n```\n{"nodes": []}'
        result = orchestrator._extract_json(text)
        assert result == {"nodes": []}


class TestRawJSON:
    def test_raw_json_object(self, orchestrator):
        text = '{"reasoning": "plan", "nodes": [], "edges": []}'
        assert orchestrator._extract_json(text) is not None

    def test_raw_json_with_trailing_text(self, orchestrator):
        """Brace-matched JSON followed by trailing text."""
        text = '{"nodes": []} trailing text here'
        result = orchestrator._extract_json(text)
        assert result == {"nodes": []}

    def test_first_valid_object_wins(self, orchestrator):
        """When multiple JSON objects exist, first valid one wins."""
        text = '{"a": 1} some text {"b": 2}'
        result = orchestrator._extract_json(text)
        assert result == {"a": 1}


class TestTruncatedJSON:
    def test_truncated_single_brace(self, orchestrator):
        text = '{"nodes": []'
        result = orchestrator._extract_json(text)
        assert result is not None
        assert result["nodes"] == []

    def test_truncated_multiple_braces(self, orchestrator):
        text = '{"a": {"b": 1'
        result = orchestrator._extract_json(text)
        assert result is not None
        assert "a" in result

    def test_truncated_with_unclosed_quote(self, orchestrator):
        text = '{"key": "value'
        result = orchestrator._extract_json(text)
        assert result is not None
        assert result["key"] == "value"


class TestEdgeCases:
    def test_empty_string(self, orchestrator):
        assert orchestrator._extract_json("") is None

    def test_no_json(self, orchestrator):
        assert orchestrator._extract_json("just plain text") is None

    def test_array_only_returns_none(self, orchestrator):
        """_extract_json returns dict only — arrays should return None."""
        text = '[1, 2, 3]'
        assert orchestrator._extract_json(text) is None

    def test_complete_but_broken_json_returns_none(self, orchestrator):
        """Complete but malformed JSON should return None, not attempt repair."""
        text = '{"key": undefined}'
        assert orchestrator._extract_json(text) is None


class TestRepairTruncatedJSON:
    def test_closes_unclosed_braces(self, orchestrator):
        result = orchestrator._repair_truncated_json('{"a": 1', brace_depth=1)
        assert result == '{"a": 1}'

    def test_closes_unclosed_quote_and_braces(self, orchestrator):
        result = orchestrator._repair_truncated_json('{"key": "val', brace_depth=1)
        assert result == '{"key": "val"}'

    def test_already_complete(self, orchestrator):
        result = orchestrator._repair_truncated_json('{"a": 1}', brace_depth=0)
        assert result == '{"a": 1}'
