"""Tests for #1127: CLI semaphore permit count configurable via WEAVE_CLI_MAX_CONCURRENT.

Previously ``_cli_semaphore`` was a hardcoded ``Semaphore(1)`` class attribute
with no override path, so parallel DAG nodes queued behind the single permit
and were killed by the wall-clock node timeout while still waiting for a slot
(#1127). The default permit count of 1 still preserves the #992 Windows
``~/.claude/`` file-lock serialization, but ``WEAVE_CLI_MAX_CONCURRENT`` lets
the operator raise it when each node runs in an isolated workspace.
"""
import asyncio

import pytest

from agent.backends import claude_code as cc


class TestCliSemaphoreConfig:
    """#1127: WEAVE_CLI_MAX_CONCURRENT controls the Claude CLI concurrency."""

    @pytest.fixture(autouse=True)
    def _reset_semaphore_cache(self, monkeypatch):
        """Reset the module-level semaphore cache between tests."""
        monkeypatch.setattr(cc, "_cli_semaphore", None)
        yield
        monkeypatch.setattr(cc, "_cli_semaphore", None)

    def test_default_permit_count(self, monkeypatch):
        monkeypatch.delenv("WEAVE_CLI_MAX_CONCURRENT", raising=False)
        sem = cc._get_cli_semaphore()
        assert sem._value == 1

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("WEAVE_CLI_MAX_CONCURRENT", "4")
        sem = cc._get_cli_semaphore()
        assert sem._value == 4

    def test_zero_clamped_to_one(self, monkeypatch):
        monkeypatch.setenv("WEAVE_CLI_MAX_CONCURRENT", "0")
        sem = cc._get_cli_semaphore()
        assert sem._value == 1

    def test_negative_clamped_to_one(self, monkeypatch):
        monkeypatch.setenv("WEAVE_CLI_MAX_CONCURRENT", "-3")
        sem = cc._get_cli_semaphore()
        assert sem._value == 1

    def test_invalid_value_falls_back_to_one(self, monkeypatch):
        monkeypatch.setenv("WEAVE_CLI_MAX_CONCURRENT", "not-a-number")
        sem = cc._get_cli_semaphore()
        assert sem._value == 1

    def test_read_once_at_first_use(self, monkeypatch):
        """The env var is read once when the semaphore is first created."""
        monkeypatch.setenv("WEAVE_CLI_MAX_CONCURRENT", "3")
        sem1 = cc._get_cli_semaphore()
        monkeypatch.setenv("WEAVE_CLI_MAX_CONCURRENT", "5")
        sem2 = cc._get_cli_semaphore()
        assert sem1 is sem2
        assert sem2._value == 3  # not 5

    @pytest.mark.asyncio
    async def test_permits_actually_allow_concurrency(self, monkeypatch):
        """N permits allow N concurrent acquirers; the (N+1)th blocks."""
        monkeypatch.setenv("WEAVE_CLI_MAX_CONCURRENT", "2")
        sem = cc._get_cli_semaphore()

        await sem.acquire()
        await sem.acquire()

        acquired_third = asyncio.Event()

        async def try_third():
            await sem.acquire()
            acquired_third.set()

        task = asyncio.ensure_future(try_third())
        await asyncio.sleep(0.05)
        assert not acquired_third.is_set()  # only 2 permits, third is blocked

        sem.release()
        await asyncio.sleep(0.05)
        assert acquired_third.is_set()  # a permit freed, third proceeds

        task.cancel()
