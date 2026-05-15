"""Tests for #144: runtime context injection to prevent path guessing.

Covers:
- _build_runtime_context() includes OS, CWD, PROJECT_ROOT, PYTHON
- Runtime context is injected into agent prompt in _execute_inner
- Bash tool description mentions PROJECT_ROOT and relative paths
- Bash tool output includes [cwd] prefix
- ToolRegistry(base_cwd=...) sets project root
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))  # noqa: E402

from tools.registry import ToolRegistry  # noqa: E402


# =============================================================================
# _build_runtime_context
# =============================================================================


class TestBuildRuntimeContext:
    """Runtime context includes OS, CWD, PROJECT_ROOT, PYTHON."""

    def _make_agent(self, base_cwd: str | None = None):
        from agent.agent_pool import WorkerAgent
        from core.config import LLMConfig
        from session.store import SessionStore

        store = MagicMock(spec=SessionStore)
        tool_reg = ToolRegistry(base_cwd=base_cwd)
        with patch("agent.worker.LLMClient"):
            agent = WorkerAgent(
                capability=MagicMock(system_prompt=""),
                llm_config=MagicMock(spec=LLMConfig),
                session_store=store,
                tool_registry=tool_reg,
                guardrails=MagicMock(),
            )
        return agent

    def test_includes_os(self):
        import platform
        agent = self._make_agent()
        ctx = agent._build_runtime_context()
        assert platform.system() in ctx

    def test_includes_cwd(self):
        agent = self._make_agent()
        ctx = agent._build_runtime_context()
        assert "CWD:" in ctx

    def test_includes_project_root(self):
        agent = self._make_agent(base_cwd="/tmp/my-project")
        ctx = agent._build_runtime_context()
        assert "PROJECT_ROOT:" in ctx
        assert "my-project" in ctx

    def test_includes_python(self):
        agent = self._make_agent()
        ctx = agent._build_runtime_context()
        assert "PYTHON:" in ctx

    def test_includes_path_rules(self):
        agent = self._make_agent()
        ctx = agent._build_runtime_context()
        assert "relative paths" in ctx.lower()
        assert "PROJECT_ROOT" in ctx

    def test_project_root_falls_back_to_cwd(self):
        agent = self._make_agent(base_cwd=None)
        ctx = agent._build_runtime_context()
        assert "PROJECT_ROOT:" in ctx

    def test_runtime_env_appears_exactly_once_in_prompt(self):
        """## Runtime Environment must appear exactly once in the final prompt
        built by _execute_inner (no duplicate method definitions or calls).
        """
        import asyncio
        from core.models import HandoffArtifact

        agent = self._make_agent(base_cwd="/tmp/project")

        # Monkey-patch _run_with_tools to capture the prompt instead of
        # actually running the LLM loop.
        captured_prompt: dict[str, str] = {}

        async def _fake_run(prompt, session_id, context=None, node_id=""):
            captured_prompt["value"] = prompt
            return {"status": "completed", "summary": "", "artifacts": [], "output": ""}

        agent._run_with_tools = _fake_run

        asyncio.run(
            agent._execute_inner(
                task="do something",
                input_artifacts=[],
                session_id="test-session",
                node_id="n1",
            )
        )

        prompt = captured_prompt["value"]
        count = prompt.count("## Runtime Environment")
        assert count == 1, (
            f"Expected exactly 1 '## Runtime Environment' in prompt, "
            f"found {count}. Prompt:\n{prompt}"
        )


# =============================================================================
# Bash tool schema and output
# =============================================================================


class TestBashToolContext:
    """Bash tool description and output include workspace context."""

    def test_bash_schema_mentions_project_root(self):
        reg = ToolRegistry()
        schema = reg.get_schema("bash")
        desc = schema["description"]
        assert "PROJECT_ROOT" in desc

    def test_bash_schema_cwd_description(self):
        reg = ToolRegistry()
        schema = reg.get_schema("bash")
        cwd_desc = schema["input_schema"]["properties"]["cwd"]["description"]
        assert "PROJECT_ROOT" in cwd_desc

    def test_bash_output_includes_cwd(self, tmp_path):
        reg = ToolRegistry(base_cwd=str(tmp_path))
        result = reg.execute("bash", {"command": "echo hello"})
        assert result.success
        assert "[cwd]" in result.output
        assert "hello" in result.output

    def test_bash_output_shows_actual_cwd(self, tmp_path):
        reg = ToolRegistry(base_cwd=str(tmp_path))
        result = reg.execute("bash", {"command": "pwd"})
        assert result.success
        # [cwd] line should show the resolved cwd
        lines = result.output.split("\n")
        cwd_line = lines[0]
        assert "[cwd]" in cwd_line


# =============================================================================
# ToolRegistry base_cwd
# =============================================================================


class TestToolRegistryBaseCwd:
    """ToolRegistry correctly stores and uses base_cwd."""

    def test_base_cwd_set(self):
        reg = ToolRegistry(base_cwd="/tmp/project")
        assert reg.base_cwd == Path("/tmp/project").resolve()

    def test_base_cwd_none(self):
        reg = ToolRegistry()
        assert reg.base_cwd is None

    def test_base_cwd_resolved(self, tmp_path):
        reg = ToolRegistry(base_cwd=str(tmp_path / "subdir"))
        assert reg.base_cwd.is_absolute()
