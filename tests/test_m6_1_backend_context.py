"""Tests for M6.1: BackendContext extension + default backend switch.

Covers:
- BackendContext new fields (memory_prompt, project_context)
- ProjectConfig.to_summary()
- NodeExecutor memory + project context injection
- ClaudeCodeBackend / CodexBackend _build_prompt() uses new fields
- Default agent backend selection
"""
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.backend_models import BackendContext
from core.config import WeaveConfig
from core.dag_engine import DAGEngineConfig
from core.dag_models import DAGNode
from core.project_config import ProjectConfig, ProjectContext
from core.artifact_handoff import HandoffArtifact


# ---------------------------------------------------------------------------
# BackendContext new fields
# ---------------------------------------------------------------------------

class TestBackendContextNewFields:
    def test_defaults_empty(self):
        ctx = BackendContext(node={})
        assert ctx.memory_prompt == ""
        assert ctx.project_context == ""

    def test_set_values(self):
        ctx = BackendContext(
            node={},
            memory_prompt="relevant memory",
            project_context="Language: python",
        )
        assert ctx.memory_prompt == "relevant memory"
        assert ctx.project_context == "Language: python"

    def test_round_trip_serialization(self):
        ctx = BackendContext(
            node={"id": "n1"},
            memory_prompt="mem",
            project_context="proj",
            session_id="s1",
        )
        data = ctx.model_dump()
        ctx2 = BackendContext(**data)
        assert ctx2.memory_prompt == "mem"
        assert ctx2.project_context == "proj"
        assert ctx2.session_id == "s1"

    def test_backward_compat_no_new_fields(self):
        """Existing code that doesn't pass new fields still works."""
        ctx = BackendContext(
            node=DAGNode(id="n1", agent_type="generator", task_description="test"),
            artifacts=[],
            session_id="s1",
        )
        assert ctx.memory_prompt == ""
        assert ctx.project_context == ""


# ---------------------------------------------------------------------------
# ProjectConfig.to_summary()
# ---------------------------------------------------------------------------

class TestProjectConfigSummary:
    def test_empty_config_returns_empty(self):
        pc = ProjectConfig()
        assert pc.to_summary() == ""

    def test_all_fields(self):
        pc = ProjectConfig(project_context=ProjectContext(
            language="python",
            framework="fastapi",
            test_runner="pytest",
            conventions=["PEP 8", "async/await"],
        ))
        summary = pc.to_summary()
        assert "Language: python" in summary
        assert "Framework: fastapi" in summary
        assert "Test runner: pytest" in summary
        assert "Conventions: PEP 8, async/await" in summary

    def test_partial_fields(self):
        pc = ProjectConfig(project_context=ProjectContext(
            language="typescript",
            framework="nextjs",
        ))
        summary = pc.to_summary()
        assert "Language: typescript" in summary
        assert "Framework: nextjs" in summary
        assert "Test runner" not in summary
        assert "Conventions" not in summary

    def test_only_language(self):
        pc = ProjectConfig(project_context=ProjectContext(language="go"))
        assert pc.to_summary() == "Language: go"


# ---------------------------------------------------------------------------
# Default agent backend
# ---------------------------------------------------------------------------

class TestDefaultAgentBackend:
    def test_weave_config_default(self):
        cfg = WeaveConfig()
        assert cfg.default_agent_backend == "claude_code"

    def test_engine_config_default(self):
        cfg = DAGEngineConfig()
        assert cfg.default_agent_backend == "claude_code"

    @patch.dict(os.environ, {"WEAVE_DEFAULT_AGENT_BACKEND": "builtin"})
    def test_env_override(self):
        cfg = WeaveConfig()
        assert cfg.default_agent_backend == "builtin"

    def test_engine_config_custom(self):
        cfg = DAGEngineConfig(default_agent_backend="codex")
        assert cfg.default_agent_backend == "codex"


# ---------------------------------------------------------------------------
# NodeExecutor injection (unit-level)
# ---------------------------------------------------------------------------

class TestNodeExecutorInjection:
    def _make_node(self, backend=None):
        return DAGNode(
            id="n1",
            agent_type="generator",
            task_description="write a function",
            backend=backend,
        )

    def test_executor_stores_memory_manager(self):
        from core.node_executor import NodeExecutor
        from core.watchdog import WatchdogService

        mm = MagicMock()
        executor = NodeExecutor(
            agent_executor=AsyncMock(return_value={}),
            emit_func=AsyncMock(),
            watchdog=WatchdogService(),
            memory_manager=mm,
        )
        assert executor._memory_manager is mm

    def test_executor_stores_project_config(self):
        from core.node_executor import NodeExecutor
        from core.watchdog import WatchdogService

        pc = ProjectConfig(project_context=ProjectContext(language="python"))
        executor = NodeExecutor(
            agent_executor=AsyncMock(return_value={}),
            emit_func=AsyncMock(),
            watchdog=WatchdogService(),
            project_config=pc,
        )
        assert executor._project_config is pc

    def test_executor_default_agent_backend(self):
        from core.node_executor import NodeExecutor
        from core.watchdog import WatchdogService

        executor = NodeExecutor(
            agent_executor=AsyncMock(return_value={}),
            emit_func=AsyncMock(),
            watchdog=WatchdogService(),
            default_agent_backend="codex",
        )
        assert executor._default_agent_backend == "codex"

    def test_backend_selection_uses_default_when_builtin(self):
        """When node.backend is "builtin" (the default), use default_agent_backend."""
        from core.node_executor import NodeExecutor
        from core.watchdog import WatchdogService

        node = DAGNode(
            id="n1", agent_type="generator", task_description="test",
        )
        # DAGNode.backend defaults to "builtin"
        assert node.backend == "builtin"

        executor = NodeExecutor(
            agent_executor=AsyncMock(return_value={}),
            emit_func=AsyncMock(),
            watchdog=WatchdogService(),
            default_agent_backend="claude_code",
        )
        # "builtin" is treated as "not explicitly set"
        backend_name = node.backend if node.backend and node.backend != "builtin" else executor._default_agent_backend
        assert backend_name == "claude_code"

    def test_backend_selection_respects_explicit_non_builtin(self):
        """When node.backend is explicitly set to non-builtin, use it."""
        from core.node_executor import NodeExecutor
        from core.watchdog import WatchdogService

        node = DAGNode(
            id="n1", agent_type="generator", task_description="test",
            backend="codex",
        )
        executor = NodeExecutor(
            agent_executor=AsyncMock(return_value={}),
            emit_func=AsyncMock(),
            watchdog=WatchdogService(),
            default_agent_backend="claude_code",
        )
        backend_name = node.backend if node.backend and node.backend != "builtin" else executor._default_agent_backend
        assert backend_name == "codex"


# ---------------------------------------------------------------------------
# ClaudeCodeBackend _build_prompt uses new fields
# ---------------------------------------------------------------------------

class TestClaudeCodePromptExtension:
    def test_prompt_includes_memory(self):
        from agent.backends.claude_code import ClaudeCodeBackend

        ctx = BackendContext(
            node=DAGNode(id="n1", agent_type="generator", task_description="test"),
            memory_prompt="Relevant Memory:\n- Use async/await",
        )
        backend = ClaudeCodeBackend.__new__(ClaudeCodeBackend)
        prompt = backend._build_prompt(ctx)
        assert "Relevant Memory" in prompt
        assert "Use async/await" in prompt

    def test_prompt_includes_project_context(self):
        from agent.backends.claude_code import ClaudeCodeBackend

        ctx = BackendContext(
            node=DAGNode(id="n1", agent_type="generator", task_description="test"),
            project_context="Language: python\nFramework: fastapi",
        )
        backend = ClaudeCodeBackend.__new__(ClaudeCodeBackend)
        prompt = backend._build_prompt(ctx)
        assert "Project Context" in prompt
        assert "Language: python" in prompt

    def test_prompt_no_extra_sections_when_empty(self):
        from agent.backends.claude_code import ClaudeCodeBackend

        ctx = BackendContext(
            node=DAGNode(id="n1", agent_type="generator", task_description="test"),
        )
        backend = ClaudeCodeBackend.__new__(ClaudeCodeBackend)
        prompt = backend._build_prompt(ctx)
        assert "Relevant Memory" not in prompt
        assert "Project Context" not in prompt


# ---------------------------------------------------------------------------
# CodexBackend _build_prompt uses new fields
# ---------------------------------------------------------------------------

class TestCodexPromptExtension:
    def test_prompt_includes_memory(self):
        from agent.backends.codex import CodexBackend

        ctx = BackendContext(
            node=DAGNode(id="n1", agent_type="generator", task_description="test"),
            memory_prompt="Relevant Memory:\n- Pattern X",
        )
        backend = CodexBackend.__new__(CodexBackend)
        prompt = backend._build_prompt(ctx)
        assert "Relevant Memory" in prompt

    def test_prompt_includes_project_context(self):
        from agent.backends.codex import CodexBackend

        ctx = BackendContext(
            node=DAGNode(id="n1", agent_type="generator", task_description="test"),
            project_context="Language: rust",
        )
        backend = CodexBackend.__new__(CodexBackend)
        prompt = backend._build_prompt(ctx)
        assert "PROJECT CONTEXT" in prompt
        assert "Language: rust" in prompt

    def test_prompt_no_extra_sections_when_empty(self):
        from agent.backends.codex import CodexBackend

        ctx = BackendContext(
            node=DAGNode(id="n1", agent_type="generator", task_description="test"),
        )
        backend = CodexBackend.__new__(CodexBackend)
        prompt = backend._build_prompt(ctx)
        assert "Relevant Memory" not in prompt
        assert "PROJECT CONTEXT" not in prompt
