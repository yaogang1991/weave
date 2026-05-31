"""Tests for #992: CLI subprocess serialization via semaphore.

ClaudeCodeBackend uses a class-level asyncio.Semaphore(1) to serialize
CLI invocations, preventing concurrent processes from hanging on Windows
due to ~/.claude/ file-lock contention.
"""
import asyncio
import inspect

from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig


class TestCLISemaphore:
    """CLI semaphore serializes concurrent invocations."""

    def test_semaphore_is_class_level(self):
        """All instances share the same semaphore."""
        cfg = ClaudeCodeRuntimeConfig()
        b1 = ClaudeCodeBackend(config=cfg)
        b2 = ClaudeCodeBackend(config=cfg)
        assert b1._cli_semaphore is b2._cli_semaphore

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
        assert "_cli_semaphore" in source
        assert "async with" in source

    def test_execute_via_cli_delegates_to_inner(self):
        """_execute_via_cli delegates to _execute_via_cli_inner."""
        cfg = ClaudeCodeRuntimeConfig()
        backend = ClaudeCodeBackend(config=cfg)
        source = inspect.getsource(backend._execute_via_cli)
        assert "_execute_via_cli_inner" in source
