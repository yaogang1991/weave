"""Tests for #381: defensive tool argument parsing in LLMClient.

Verifies that malformed, empty, or null tool call arguments from LLM
responses are handled gracefully without crashing the agent loop.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.llm_client import LLMClient  # noqa: E402


# ---------------------------------------------------------------------------
# _parse_tool_arguments unit tests
# ---------------------------------------------------------------------------


class TestParseToolArguments:
    """Test LLMClient._parse_tool_arguments static method."""

    def test_none_args(self):
        assert LLMClient._parse_tool_arguments(None) == {}

    def test_empty_string_args(self):
        assert LLMClient._parse_tool_arguments("") == {}

    def test_whitespace_only_args(self):
        assert LLMClient._parse_tool_arguments("   ") == {}

    def test_malformed_json_args(self):
        assert LLMClient._parse_tool_arguments("{invalid") == {}

    def test_valid_json_args(self):
        raw = json.dumps({"file_path": "test.py", "content": "pass"})
        result = LLMClient._parse_tool_arguments(raw)
        assert result == {"file_path": "test.py", "content": "pass"}

    def test_non_dict_json_null(self):
        assert LLMClient._parse_tool_arguments("null") == {}

    def test_non_dict_json_array(self):
        assert LLMClient._parse_tool_arguments("[1, 2, 3]") == {}

    def test_non_dict_json_string(self):
        assert LLMClient._parse_tool_arguments('"hello"') == {}

    def test_non_dict_json_number(self):
        assert LLMClient._parse_tool_arguments("42") == {}

    def test_empty_dict_json(self):
        assert LLMClient._parse_tool_arguments("{}") == {}


# ---------------------------------------------------------------------------
# Integration: _call_openai with malformed arguments
# ---------------------------------------------------------------------------


class TestOpenAIMalformedArgs:
    """Verify _call_openai handles malformed tool call arguments."""

    @pytest.fixture
    def client(self):
        from core.config import LLMConfig
        config = LLMConfig(api_key="test", model="test-model")
        with patch.object(LLMClient, "_create_client", return_value=MagicMock()):
            return LLMClient(config=config)

    def _mock_tool_call(self, arguments):
        """Create a mock tool call with given arguments string."""
        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "write"
        tc.function.arguments = arguments
        return tc

    def _mock_response(self, tool_calls=None):
        """Create a mock OpenAI response."""
        msg = MagicMock()
        msg.content = ""
        msg.tool_calls = tool_calls
        choice = MagicMock()
        choice.message = msg
        resp = MagicMock()
        resp.choices = [choice]
        return resp

    def test_empty_string_args_no_crash(self, client):
        """Empty string arguments should produce empty dict, not crash."""
        tc = self._mock_tool_call("")
        resp = self._mock_response([tc])
        client._client.chat.completions.create.return_value = resp

        result = client._call_openai(
            [{"role": "user", "content": "test"}], []
        )
        assert result["tool_calls"][0]["arguments"] == {}

    def test_none_args_no_crash(self, client):
        """None arguments should produce empty dict, not crash."""
        tc = self._mock_tool_call(None)
        resp = self._mock_response([tc])
        client._client.chat.completions.create.return_value = resp

        result = client._call_openai(
            [{"role": "user", "content": "test"}], []
        )
        assert result["tool_calls"][0]["arguments"] == {}

    def test_malformed_json_args_no_crash(self, client):
        """Malformed JSON arguments should produce empty dict, not crash."""
        tc = self._mock_tool_call("{not valid json")
        resp = self._mock_response([tc])
        client._client.chat.completions.create.return_value = resp

        result = client._call_openai(
            [{"role": "user", "content": "test"}], []
        )
        assert result["tool_calls"][0]["arguments"] == {}

    def test_valid_args_passthrough(self, client):
        """Valid JSON arguments should pass through unchanged."""
        valid_json = json.dumps({"file_path": "test.py", "content": "pass"})
        tc = self._mock_tool_call(valid_json)
        resp = self._mock_response([tc])
        client._client.chat.completions.create.return_value = resp

        result = client._call_openai(
            [{"role": "user", "content": "test"}], []
        )
        assert result["tool_calls"][0]["arguments"] == {
            "file_path": "test.py", "content": "pass",
        }


# ---------------------------------------------------------------------------
# Integration: Anthropic non-dict input
# ---------------------------------------------------------------------------


class TestAnthropicNonDictInput:
    """Verify _call_anthropic handles non-dict tool input gracefully."""

    @pytest.fixture
    def client(self):
        from core.config import LLMConfig
        config = LLMConfig(
            api_key="test", model="test-model", provider="anthropic",
        )
        with patch.object(LLMClient, "_create_client", return_value=MagicMock()):
            return LLMClient(config=config)

    def _mock_block(self, input_val):
        block = MagicMock()
        block.type = "tool_use"
        block.id = "tool_1"
        block.name = "write"
        block.input = input_val
        return block

    def _mock_response(self, blocks):
        resp = MagicMock()
        resp.content = blocks
        resp.stop_reason = "end_turn"
        return resp

    def test_non_dict_input_defaults_to_empty(self, client):
        """Non-dict input should default to empty dict."""
        block = self._mock_block(None)
        resp = self._mock_response([block])
        client._client.messages.create.return_value = resp

        result = client._call_anthropic(
            [{"role": "user", "content": "test"}], []
        )
        assert result["tool_calls"][0]["arguments"] == {}

    def test_dict_input_passthrough(self, client):
        """Dict input should pass through unchanged."""
        block = self._mock_block({"file_path": "test.py", "content": "pass"})
        resp = self._mock_response([block])
        client._client.messages.create.return_value = resp

        result = client._call_anthropic(
            [{"role": "user", "content": "test"}], []
        )
        assert result["tool_calls"][0]["arguments"] == {
            "file_path": "test.py", "content": "pass",
        }
