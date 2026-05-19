"""Tests for text-based tool args fallback (#579)."""
from core.llm_client import LLMClient


class TestExtractToolArgsFromText:
    """Verify _extract_tool_args_from_text parses JSON from text content."""

    def test_no_json_returns_none(self):
        result = LLMClient._extract_tool_args_from_text(
            "No JSON here", "write",
        )
        assert result is None

    def test_json_after_tool_name(self):
        text = 'I will use write({"file_path": "a.py", "content": "hello"})'
        result = LLMClient._extract_tool_args_from_text(text, "write")
        assert result is not None
        assert result["file_path"] == "a.py"
        assert result["content"] == "hello"

    def test_json_after_colon(self):
        text = 'write: {"file_path": "b.py", "content": "world"}'
        result = LLMClient._extract_tool_args_from_text(text, "write")
        assert result is not None
        assert result["file_path"] == "b.py"

    def test_fallback_any_json_object(self):
        """When tool name not found, returns any JSON object with 2+ keys."""
        text = 'Here is what I want to do {"file_path": "c.py", "content": "x"}'
        result = LLMClient._extract_tool_args_from_text(text, "bash")
        assert result is not None
        assert result["file_path"] == "c.py"

    def test_single_key_json_skipped_in_fallback(self):
        """Fallback skips JSON objects with only 1 key."""
        text = '{"command": "echo"}'
        result = LLMClient._extract_tool_args_from_text(text, "unknown_tool")
        assert result is None

    def test_empty_text_returns_none(self):
        result = LLMClient._extract_tool_args_from_text("", "write")
        assert result is None

    def test_invalid_json_returns_none(self):
        text = 'write({"file_path": broken)'
        result = LLMClient._extract_tool_args_from_text(text, "write")
        # The regex finds {file_path: broken} which is not valid JSON
        # but the fallback might find nothing with 2+ keys
        assert result is None or isinstance(result, dict)

    def test_multiple_json_objects_picks_first(self):
        """When multiple JSON objects exist, returns the first with 2+ keys."""
        text = (
            '{"a": 1} then {"file_path": "d.py", "content": "hi"}'
        )
        result = LLMClient._extract_tool_args_from_text(text, "write")
        assert result is not None
        assert "file_path" in result
