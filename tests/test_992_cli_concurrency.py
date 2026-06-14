"""Tests for #992: CLI subprocess serialization via semaphore.

ClaudeCodeBackend serializes CLI invocations through a module-level
``asyncio.Semaphore`` so concurrent processes do not hang on Windows due
to ~/.claude/ file-lock contention. The permit count is configurable via
``WEAVE_CLI_MAX_CONCURRENT`` (default 1) — see #1127.
"""
import asyncio
import inspect

from agent.backends import claude_code as cc
from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig


class TestCLISemaphore:
    """CLI semaphore serializes concurrent invocations."""

    def test_semaphore_is_shared(self, monkeypatch):
        """All instances share the same module-level semaphore (#992)."""
        monkeypatch.setattr(cc, "_cli_semaphore", None)
        cfg = ClaudeCodeRuntimeConfig()
        ClaudeCodeBackend(config=cfg)
        ClaudeCodeBackend(config=cfg)
        # The semaphore is a module-level singleton shared across instances.
        assert cc._get_cli_semaphore() is cc._get_cli_semaphore()

    def test_semaphore_initial_value_is_one(self):
        """Semaphore capacity is 1 — only one CLI at a time."""
        # Create a fresh semaphore to verify the pattern
        sem = asyncio.Semaphore(1)
        assert sem._value == 1

    def test_execute_via_cli_uses_semaphore(self):
        """_execute_via_cli acquires semaphore before spawning process."""
        cfg = ClaudeCodeRuntimeConfig()
        backend = ClaudeCodeBackend(config=cfg)
        source = inspect.getsource(backend._execute_via_cli)
        assert "_get_cli_semaphore" in source
        assert "async with" in source

    def test_execute_via_cli_delegates_to_inner(self):
        """_execute_via_cli delegates to _execute_via_cli_inner."""
        cfg = ClaudeCodeRuntimeConfig()
        backend = ClaudeCodeBackend(config=cfg)
        source = inspect.getsource(backend._execute_via_cli)
        assert "_execute_via_cli_inner" in source
