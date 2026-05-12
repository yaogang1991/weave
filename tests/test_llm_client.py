"""
Tests for core/llm_client.py — retry logic, rate limit parsing, provider dispatch.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from core.llm_client import LLMClient
from core.config import LLMConfig


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
