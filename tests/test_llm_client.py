"""
Tests for core/llm_client.py — retry logic, rate limit parsing, provider dispatch.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from core.llm_client import LLMClient
from core.config import LLMConfig
from core.exceptions import LLMResponseError


@pytest.fixture
def llm_config():
    return LLMConfig(
        provider="anthropic",
        model="claude-sonnet-4-6",
        api_key="test-key",
    )


@pytest.fixture
def client(llm_config):
    return LLMClient(llm_config, max_retries=3)


# ------------------------------------------------------------------------------
# Retry logic
# ------------------------------------------------------------------------------

class TestRetry:
    def test_transient_error_retries(self, client):
        """Retries on ConnectionError then succeeds."""
        client._call_once = MagicMock(side_effect=[
            ConnectionError("refused"),
            {"role": "assistant", "content": "OK"},
        ])
        with patch("core.llm_client.time.sleep"):
            result = client.call([{"role": "user", "content": "hi"}], [])
        assert result["content"] == "OK"
        assert client._call_once.call_count == 2

    def test_non_transient_raises(self, client):
        """ValueError is not retried."""
        client._call_once = MagicMock(side_effect=ValueError("bad input"))
        with pytest.raises(ValueError):
            client.call([{"role": "user", "content": "hi"}], [])

    def test_max_retries_exceeded(self, client):
        """Raises after max retries on persistent transient error."""
        client._call_once = MagicMock(side_effect=TimeoutError("timed out"))
        with patch("core.llm_client.time.sleep"):
            with pytest.raises(TimeoutError):
                client.call([{"role": "user", "content": "hi"}], [])
        assert client._call_once.call_count == 4  # 1 + 3 retries

    def test_max_retries_override(self, client):
        """Per-call max_retries overrides the default."""
        client._call_once = MagicMock(side_effect=ConnectionError("fail"))
        with patch("core.llm_client.time.sleep"):
            with pytest.raises(ConnectionError):
                client.call([], [], max_retries=1)
        assert client._call_once.call_count == 2  # 1 + 1 override


# ------------------------------------------------------------------------------
# Rate limit parsing
# ------------------------------------------------------------------------------

class TestRateLimitParsing:
    def test_parse_reset_datetime(self):
        """Parse 'will reset at YYYY-MM-DD HH:MM:SS' pattern."""
        future = datetime.now(timezone.utc) + timedelta(seconds=120)
        msg = f"429 - rate limit exceeded, will reset at {future.strftime('%Y-%m-%d %H:%M:%S')}"
        wait = LLMClient._parse_rate_limit_wait(msg)
        assert wait is not None
        assert 100 < wait < 130

    def test_parse_retry_after_seconds(self):
        msg = "429 rate limit, retry-after: 60"
        wait = LLMClient._parse_rate_limit_wait(msg)
        assert wait == 60.0

    def test_parse_retry_in_seconds(self):
        msg = "rate limited, retry in 30 seconds"
        wait = LLMClient._parse_rate_limit_wait(msg)
        assert wait == 30.0

    def test_parse_no_match_returns_none(self):
        assert LLMClient._parse_rate_limit_wait("some other error") is None

    def test_rate_limit_uses_parsed_wait(self, client):
        """429 error should sleep for parsed duration, not short backoff."""
        client._call_once = MagicMock(side_effect=[
            RuntimeError("429 rate limit, retry-after: 60"),
            {"role": "assistant", "content": "done"},
        ])
        with patch("core.llm_client.time.sleep") as mock_sleep:
            client.call([{"role": "user", "content": "hi"}], [])
        # Should sleep ~61 seconds (parsed + 1 buffer), not 2^0 = 1 second
        assert mock_sleep.call_count == 1
        assert mock_sleep.call_args[0][0] > 30


# ------------------------------------------------------------------------------
# Prompt caching (#503)
# ------------------------------------------------------------------------------

class TestPromptCaching:
    def test_system_prompt_has_cache_control(self, client):
        """System prompt is sent as a content block with cache_control (#503)."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="hi")]
        mock_response.usage = MagicMock(
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        client._client.messages.create = MagicMock(return_value=mock_response)

        messages = [
            {"role": "system", "content": "you are a helper"},
            {"role": "user", "content": "hello"},
        ]
        client._call_anthropic(messages, [])

        call_kwargs = client._client.messages.create.call_args.kwargs
        system = call_kwargs.get("system")
        assert system is not None
        assert isinstance(system, list)
        assert len(system) == 1
        assert system[0]["type"] == "text"
        assert system[0]["text"] == "you are a helper"
        assert system[0]["cache_control"] == {"type": "ephemeral"}

    def test_no_system_prompt_no_cache_control(self, client):
        """When no system prompt, 'system' key is absent from kwargs."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="hi")]
        mock_response.usage = MagicMock()
        client._client.messages.create = MagicMock(return_value=mock_response)

        messages = [{"role": "user", "content": "hello"}]
        client._call_anthropic(messages, [])

        call_kwargs = client._client.messages.create.call_args.kwargs
        assert "system" not in call_kwargs

    def test_cache_usage_logged(self, client):
        """Cache usage stats are logged when present in response (#503)."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="done")]
        mock_response.usage = MagicMock(
            cache_read_input_tokens=500,
            cache_creation_input_tokens=200,
        )
        client._client.messages.create = MagicMock(return_value=mock_response)

        with patch("core.llm_client.logger") as mock_logger:
            client._call_anthropic(
                [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
                [],
            )
        # Check that info was called with cache stats
        info_calls = [str(c) for c in mock_logger.info.call_args_list]
        assert any("cache" in c.lower() for c in info_calls)

    def test_no_cache_usage_no_log(self, client):
        """No cache log when usage has no cache tokens."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="hi")]
        mock_response.usage = MagicMock(
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        client._client.messages.create = MagicMock(return_value=mock_response)

        with patch("core.llm_client.logger") as mock_logger:
            client._call_anthropic(
                [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
                [],
            )
        # Should not log cache stats when both are 0
        info_calls = [str(c) for c in mock_logger.info.call_args_list]
        assert not any("cache" in c.lower() for c in info_calls)


# ------------------------------------------------------------------------------
# max_tokens_override (#621)
# ------------------------------------------------------------------------------


class TestMaxTokensOverride:
    def test_override_used_in_anthropic_call(self, client):
        """max_tokens_override replaces config.max_tokens in Anthropic calls."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="ok")]
        mock_response.usage = MagicMock(
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        client._client.messages.create = MagicMock(return_value=mock_response)

        client._call_anthropic(
            [{"role": "user", "content": "test"}],
            [],
        )
        call_kwargs = client._client.messages.create.call_args[1]
        assert call_kwargs["max_tokens"] == 4096  # default

        # Now with override
        client._max_tokens_override = 8192
        client._call_anthropic(
            [{"role": "user", "content": "test"}],
            [],
        )
        call_kwargs = client._client.messages.create.call_args[1]
        assert call_kwargs["max_tokens"] == 8192

    def test_override_none_uses_default(self, client):
        """When _max_tokens_override is None, config default is used."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="ok")]
        mock_response.usage = MagicMock(
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        client._client.messages.create = MagicMock(return_value=mock_response)

        client._max_tokens_override = None
        client._call_anthropic(
            [{"role": "user", "content": "test"}],
            [],
        )
        call_kwargs = client._client.messages.create.call_args[1]
        assert call_kwargs["max_tokens"] == 4096  # config default


# ------------------------------------------------------------------------------
# Empty response boundary checks (#1007)
# ------------------------------------------------------------------------------

class TestEmptyResponseGuards:
    def test_openai_empty_choices_raises(self):
        """OpenAI returning empty choices should raise LLMResponseError."""
        cfg = LLMConfig(provider="openai", model="gpt-4", api_key="test-key")
        client = LLMClient(cfg, max_retries=1)

        mock_response = MagicMock()
        mock_response.choices = []
        client._client.chat.completions.create = MagicMock(
            return_value=mock_response,
        )

        with pytest.raises(LLMResponseError, match="empty choices"):
            client._call_openai(
                [{"role": "user", "content": "test"}], [],
            )

    def test_anthropic_empty_content_raises(self):
        """Anthropic returning empty content should raise LLMResponseError."""
        cfg = LLMConfig(provider="anthropic", model="claude-sonnet-4-6", api_key="test-key")
        client = LLMClient(cfg, max_retries=1)

        mock_response = MagicMock()
        mock_response.content = []
        mock_response.usage = MagicMock(
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        client._client.messages.create = MagicMock(
            return_value=mock_response,
        )

        with pytest.raises(LLMResponseError, match="empty content"):
            client._call_anthropic(
                [{"role": "user", "content": "test"}], [],
            )
