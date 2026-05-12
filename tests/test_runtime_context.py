"""Tests for runtime context injection into agent prompt (#144).

Verifies that _build_runtime_context produces environment info and
the bash tool schema includes path guidance.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestRuntimeContext:
    """Verify _build_runtime_context produces useful environment info."""

    def _make_agent(self, base_cwd=None):
        from agent.agent_pool import WorkerAgent
        from core.models import AgentCapability
        from core.config import LLMConfig
        from session.store import SessionStore
        from tools.registry import ToolRegistry

        capability = AgentCapability(id="generator", name="Generator", description="impl")
        store = MagicMock(spec=SessionStore)
        tool_reg = ToolRegistry(base_cwd=base_cwd) if base_cwd else ToolRegistry()

        # Patch LLMClient to avoid needing real API credentials
        with patch("agent.worker.LLMClient"):
            agent = WorkerAgent(
                capability=capability,
                llm_config=MagicMock(spec=LLMConfig),
                session_store=store,
                tool_registry=tool_reg,
            )
        return agent

    def test_context_contains_os_info(self):
        agent = self._make_agent(base_cwd="/tmp/test_project")
        ctx = agent._build_runtime_context()

        assert "PROJECT_ROOT" in ctx
        assert "/tmp/test_project" in ctx
        assert "Path rules" in ctx
        assert "relative to PROJECT_ROOT" in ctx

    def test_context_without_base_cwd_uses_cwd(self):
        agent = self._make_agent()
        ctx = agent._build_runtime_context()

        assert "PROJECT_ROOT" in ctx
        assert "Do NOT invent absolute paths" in ctx

    def test_context_contains_path_separator(self):
        agent = self._make_agent(base_cwd="/my/project")
        ctx = agent._build_runtime_context()

        assert "/my/project" in ctx
        assert "Path separator" in ctx


class TestBashToolSchema:
    """Verify bash tool schema includes path guidance."""

    def test_bash_description_mentions_project_root(self):
        from tools.registry import ToolRegistry

        reg = ToolRegistry()
        bash_schema = next(s for s in reg.schemas if s["name"] == "bash")
        desc = bash_schema["description"]
        assert "PROJECT_ROOT" in desc
        assert "relative paths" in desc

    def test_bash_description_warns_against_absolute_paths(self):
        from tools.registry import ToolRegistry

        reg = ToolRegistry()
        bash_schema = next(s for s in reg.schemas if s["name"] == "bash")
        desc = bash_schema["description"]
        assert "Do NOT guess absolute paths" in desc
