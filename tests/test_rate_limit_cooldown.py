"""Tests for rate-limit recovery cooldown (#583)."""
import time
from unittest.mock import patch

from core.llm_client import LLMClient, LLMConfig

_OK_RESPONSE = {"role": "assistant", "content": "ok"}


class TestRateLimitCooldown:
    """Verify cooldown delay after rate-limit recovery (#583)."""

    def _make_client(self) -> LLMClient:
        config = LLMConfig(api_key="test-key")
        return LLMClient(config)

    def test_no_cooldown_on_first_call(self):
        """First call has no cooldown."""
        client = self._make_client()
        assert client._last_rate_limit_recovery == 0.0
        elapsed = time.monotonic() - client._last_rate_limit_recovery
        assert not (0 < elapsed < client._rate_limit_cooldown_sec)

    def test_cooldown_set_after_rate_limit_recovery(self):
        """Successful call after 429 sets recovery timestamp."""
        client = self._make_client()
        call_count = 0

        def mock_call_once(messages, tools=None, tool_choice=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("429 rate limit exceeded")
            return _OK_RESPONSE

        with patch.object(client, '_call_once', side_effect=mock_call_once):
            client.call(
                [{"role": "user", "content": "test"}], max_retries=2,
            )

        assert client._last_rate_limit_recovery > 0

    def test_cooldown_triggers_sleep_on_next_call(self):
        """Next call after recovery sleeps for remaining cooldown."""
        client = self._make_client()
        client._rate_limit_cooldown_sec = 10.0
        client._last_rate_limit_recovery = time.monotonic() - 2.0

        sleep_times = []
        with patch('time.sleep', side_effect=lambda s: sleep_times.append(s)):
            with patch.object(
                client, '_call_once', return_value=_OK_RESPONSE,
            ):
                client.call(
                    [{"role": "user", "content": "test"}], max_retries=1,
                )

        assert len(sleep_times) >= 1
        assert 7.0 <= sleep_times[0] <= 9.0

    def test_no_cooldown_when_fully_elapsed(self):
        """No sleep when cooldown period has fully elapsed."""
        client = self._make_client()
        client._rate_limit_cooldown_sec = 1.0
        client._last_rate_limit_recovery = time.monotonic() - 5.0

        sleep_times = []
        with patch('time.sleep', side_effect=lambda s: sleep_times.append(s)):
            with patch.object(
                client, '_call_once', return_value=_OK_RESPONSE,
            ):
                client.call(
                    [{"role": "user", "content": "test"}], max_retries=1,
                )

        cooldown_sleeps = [s for s in sleep_times if s > 1.0]
        assert len(cooldown_sleeps) == 0

    def test_cooldown_not_set_without_rate_limit(self):
        """Normal successful call does not set recovery timestamp."""
        client = self._make_client()
        assert client._last_rate_limit_recovery == 0.0

        with patch.object(
            client, '_call_once', return_value=_OK_RESPONSE,
        ):
            client.call(
                [{"role": "user", "content": "test"}], max_retries=1,
            )

        assert client._last_rate_limit_recovery == 0.0
