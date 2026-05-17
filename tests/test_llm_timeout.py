"""Tests for #367: LLM client timeout enforcement.

Verifies:
1. httpx.Timeout is used with separate connect/read/write/pool values
2. Hard timeout kills hanging API calls
3. TimeoutError is classified as transient for retry
"""
import threading
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from core.config import LLMConfig
from core.llm_client import LLMClient


def _make_config(**overrides) -> LLMConfig:
    defaults = {
        "provider": "openai",
        "api_key": "test-key",
        "model": "test-model",
        "timeout": 5,
    }
    defaults.update(overrides)
    return LLMConfig(**defaults)


class TestTimeoutConfiguration:
    """Verify httpx.Timeout with separate phases."""

    def test_openai_client_uses_httpx_timeout(self):
        """OpenAI client should use httpx.Timeout, not a plain int."""
        config = _make_config(timeout=60)
        with patch("core.llm_client.OpenAI") as mock_openai:
            mock_openai.return_value = MagicMock()
            LLMClient(config)  # noqa: F841

            call_args = mock_openai.call_args
            timeout = call_args.kwargs.get("timeout")
            assert isinstance(timeout, httpx.Timeout)
            assert timeout.connect == 30.0
            assert timeout.read == 60
            assert timeout.write == 30.0
            assert timeout.pool == 30.0

    def test_anthropic_client_uses_httpx_timeout(self):
        """Anthropic client should also use httpx.Timeout."""
        config = _make_config(provider="anthropic", timeout=90)
        with patch("core.llm_client.anthropic.Anthropic") as mock_anth:
            mock_anth.return_value = MagicMock()
            LLMClient(config)  # noqa: F841

            call_args = mock_anth.call_args
            timeout = call_args.kwargs.get("timeout")
            assert isinstance(timeout, httpx.Timeout)
            assert timeout.read == 90


class TestHardTimeout:
    """Verify hard timeout kills hanging calls."""

    def test_hanging_call_raises_timeout_error(self):
        """If API hangs beyond hard timeout, TimeoutError is raised."""
        config = _make_config(timeout=1)

        with patch("core.llm_client.OpenAI") as mock_openai:
            mock_client = MagicMock()

            # Simulate API that hangs forever (sleeps longer than timeout)
            def slow_create(*args, **kwargs):
                time.sleep(300)
                return MagicMock()

            mock_client.chat.completions.create = slow_create
            mock_openai.return_value = mock_client

            client = LLMClient(config)
            with pytest.raises(TimeoutError, match="hard timeout"):
                client.call(
                    [{"role": "user", "content": "hi"}],
                    max_retries=0,  # Don't retry — just fail fast
                )

    def test_fast_call_succeeds(self):
        """Calls that complete within timeout succeed normally."""
        config = _make_config(timeout=10)

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "hello"
        mock_response.choices[0].message.tool_calls = None

        with patch("core.llm_client.OpenAI") as mock_openai:
            mock_client = MagicMock()
            mock_client.chat.completions.create.return_value = mock_response
            mock_openai.return_value = mock_client

            client = LLMClient(config)
            result = client.call([{"role": "user", "content": "hi"}])
            assert result["content"] == "hello"

    def test_hard_timeout_is_config_timeout_plus_buffer(self):
        """Hard timeout = config.timeout + 30s buffer."""
        config = _make_config(timeout=2)
        thread_timeout = None

        original_join = threading.Thread.join

        def patched_join(self, timeout=None):
            nonlocal thread_timeout
            thread_timeout = timeout
            # Don't actually wait — simulate immediate completion
            return original_join(self, timeout=0.001)

        with patch("core.llm_client.OpenAI") as mock_openai:
            mock_client = MagicMock()

            def instant_call(*args, **kwargs):
                return MagicMock(
                    choices=[MagicMock(
                        message=MagicMock(content="ok", tool_calls=None)
                    )]
                )

            mock_client.chat.completions.create = instant_call
            mock_openai.return_value = mock_client

            with patch.object(threading.Thread, "join", patched_join):
                client = LLMClient(config)
                # The thread won't actually complete in 1ms, so we'll get
                # a TimeoutError or result. We just want to capture the timeout value.
                try:
                    client.call([{"role": "user", "content": "hi"}])
                except (TimeoutError, RuntimeError):
                    pass

        assert thread_timeout == 34  # 2s config * 2 + 30s buffer

    def test_semaphore_released_on_hard_timeout(self):
        """Semaphore permit must be released even when hard timeout fires (#367 review)."""
        config = _make_config(timeout=1)

        with patch("core.llm_client.OpenAI") as mock_openai:
            mock_client = MagicMock()

            def slow_create(*args, **kwargs):
                time.sleep(300)
                return MagicMock()

            mock_client.chat.completions.create = slow_create
            mock_openai.return_value = mock_client

            with patch("core.llm_client._get_api_semaphore") as mock_sem:
                sem = threading.Semaphore(1)
                mock_sem.return_value = sem

                client = LLMClient(config)
                with pytest.raises(TimeoutError, match="hard timeout"):
                    client.call(
                        [{"role": "user", "content": "hi"}],
                        max_retries=0,
                    )

                # Semaphore must be released after hard timeout
                assert sem.acquire(blocking=False), \
                    "Semaphore was not released after hard timeout — permit leak"
                sem.release()
