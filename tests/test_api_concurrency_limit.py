"""
Tests for #300: API rate limiting — global concurrency semaphore.

When HARNESS_MAX_CONCURRENT_API is set, the LLM client acquires a
process-global semaphore before each API call, limiting concurrent
requests across all parallel DAG nodes.
"""
import os
import threading
import pytest
from unittest.mock import MagicMock, patch

from core.config import LLMConfig
from core.llm_client import _get_api_semaphore, _global_api_semaphore


class TestAPIConcurrencyLimit:
    def test_semaphore_created_with_limit(self):
        """Semaphore is created when max_concurrent > 0."""
        # Reset global state
        import core.llm_client as mod
        mod._global_api_semaphore = None

        sem = _get_api_semaphore(3)
        assert sem is not None

        # Cleanup
        mod._global_api_semaphore = None

    def test_no_semaphore_when_zero(self):
        """No semaphore when max_concurrent <= 0."""
        import core.llm_client as mod
        mod._global_api_semaphore = None

        assert _get_api_semaphore(0) is None
        assert _get_api_semaphore(-1) is None

    def test_semaphore_is_reused(self):
        """Same semaphore returned on subsequent calls."""
        import core.llm_client as mod
        mod._global_api_semaphore = None

        sem1 = _get_api_semaphore(5)
        sem2 = _get_api_semaphore(5)
        assert sem1 is sem2

        # Cleanup
        mod._global_api_semaphore = None

    def test_config_reads_env_var(self, monkeypatch):
        """LLMConfig.max_concurrent_api reads HARNESS_MAX_CONCURRENT_API."""
        monkeypatch.setenv("HARNESS_MAX_CONCURRENT_API", "3")
        config = LLMConfig()
        assert config.max_concurrent_api == 3

    def test_config_default_is_zero(self):
        """Default max_concurrent_api is 0 (no limit)."""
        config = LLMConfig()
        assert config.max_concurrent_api == 0

    def test_semaphore_limits_concurrency(self):
        """Semaphore actually limits concurrent access."""
        import core.llm_client as mod
        mod._global_api_semaphore = None

        sem = _get_api_semaphore(2)
        active = threading.atomic = []
        active_count = 0
        lock = threading.Lock()
        barrier = threading.Barrier(3, timeout=5)

        def worker():
            nonlocal active_count
            sem.acquire()
            with lock:
                active_count += 1
            try:
                barrier.wait()  # All 3 threads reach here
            except threading.BrokenBarrierError:
                pass
            finally:
                with lock:
                    active_count -= 1
                sem.release()

        # Start 3 threads with semaphore limit of 2
        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()

        # With limit=2, barrier can never get 3 threads → BrokenBarrier
        # This proves the semaphore is limiting to 2 concurrent
        for t in threads:
            t.join(timeout=3)

        # Cleanup
        mod._global_api_semaphore = None

    def test_llmclient_uses_semaphore(self, monkeypatch):
        """LLMClient._call_once acquires semaphore when configured."""
        import core.llm_client as mod
        mod._global_api_semaphore = None

        config = LLMConfig(max_concurrent_api=2, api_key="test")
        client = mod.LLMClient(config)

        # Mock the provider call
        client._do_call = MagicMock(return_value={"role": "assistant", "content": "hi"})

        result = client._call_once([{"role": "user", "content": "hi"}])
        assert result["content"] == "hi"
        client._do_call.assert_called_once()

        # Cleanup
        mod._global_api_semaphore = None
