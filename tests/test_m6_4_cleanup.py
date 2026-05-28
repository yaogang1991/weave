"""M6.4: Cleanup + documentation update — import smoke tests.

Validates that all M6.x backend models and modules are importable
without errors.
"""
from __future__ import annotations

import importlib
import pytest


# --- Backend models (M6.1) ---

class TestBackendModels:
    """Smoke tests for core/backend_models.py imports."""

    def test_import_backend_context(self):
        from core.backend_models import BackendContext
        assert BackendContext is not None

    def test_import_backend_result(self):
        from core.backend_models import BackendResult
        assert BackendResult is not None

    def test_import_backend_status(self):
        from core.backend_models import BackendStatus
        assert BackendStatus is not None


# --- Agent backends (M6.1, M6.3) ---

class TestAgentBackends:
    """Smoke tests for agent/backends/ imports."""

    def test_import_agent_backend_base(self):
        from agent.backends.base import AgentBackend
        assert AgentBackend is not None

    def test_import_builtin_backend(self):
        from agent.backends.builtin import BuiltinBackend
        assert BuiltinBackend is not None

    def test_import_claude_code_backend(self):
        from agent.backends.claude_code import ClaudeCodeBackend
        assert ClaudeCodeBackend is not None

    def test_import_codex_backend(self):
        from agent.backends.codex import CodexBackend
        assert CodexBackend is not None

    def test_import_backend_registry(self):
        from agent.backends.registry import BackendRegistry
        assert BackendRegistry is not None

    def test_import_stderr_tail(self):
        from agent.backends.stderr_tail import StderrTail
        assert StderrTail is not None

    def test_import_stream_parser(self):
        from agent.backends.stream_parser import StreamParser
        assert StreamParser is not None


# --- Core M6 modules ---

class TestCoreM6Modules:
    """Smoke tests for core/ modules added in M6."""

    def test_import_activity_detector(self):
        from core.activity_detector import is_meaningful_event, ActivityDetector
        assert is_meaningful_event is not None
        assert ActivityDetector is not None

    def test_import_lightweight_llm_caller(self):
        """M6.3: LightweightLLMCaller should be importable."""
        from agent.lightweight_llm_caller import LightweightLLMCaller
        assert LightweightLLMCaller is not None


# --- Deprecation annotations ---

class TestDeprecationAnnotations:
    """Verify deprecated modules still import cleanly (not deleted)."""

    def test_import_output_monitor(self):
        from guardrails.output_monitor import OutputMonitor
        assert OutputMonitor is not None

    def test_import_stuck_detector(self):
        from core.stuck_detector import StuckDetector
        assert StuckDetector is not None

    def test_import_agent_prompts(self):
        from agent.prompts import SYSTEM_PROMPTS, TOOL_ALLOWLIST
        assert "generator" in SYSTEM_PROMPTS
        assert "generator" in TOOL_ALLOWLIST

    def test_import_tool_registry(self):
        from tools.registry import ToolRegistry
        registry = ToolRegistry()
        assert registry is not None
