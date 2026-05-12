"""
Tests for explicit ExecutionContext (#132 P0-2).

Verifies that:
1. WorkerAgent no longer stores per-node mutable context on instance fields
2. ExecutionContext is passed explicitly through the tool call chain
3. Concurrent nodes of the same agent type don't have context cross-talk
4. AgentPool.create_worker() is the canonical name; get_or_create is an alias
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.agent_pool import WorkerAgent, AgentPool, ExecutionContext
from core.models import AgentCapability, ToolResult
from core.config import LLMConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_capability(agent_type: str = "generator") -> AgentCapability:
    return AgentCapability(
        id=agent_type,
        name=f"{agent_type.title()} Agent",
        description=f"{agent_type} agent for testing",
    )


def _make_worker(
    agent_type: str = "generator",
    job_id: str = "job-1",
    approval_repo=None,
) -> WorkerAgent:
    session_store = MagicMock()
    tool_registry = MagicMock()
    tool_registry.schemas = [
        {"name": "read"},
        {"name": "write"},
        {"name": "bash"},
    ]
    return WorkerAgent(
        capability=_make_capability(agent_type),
        llm_config=LLMConfig(api_key="test"),
        session_store=session_store,
        tool_registry=tool_registry,
        job_id=job_id,
        approval_repo=approval_repo,
    )


# ---------------------------------------------------------------------------
# TestExecutionContext
# ---------------------------------------------------------------------------


class TestExecutionContext:
    def test_frozen_dataclass(self):
        ctx = ExecutionContext(job_id="j1", run_id="r1", node_id="n1")
        with pytest.raises(AttributeError):
            ctx.job_id = "j2"  # type: ignore[misc]

    def test_defaults(self):
        ctx = ExecutionContext()
        assert ctx.job_id == ""
        assert ctx.run_id is None
        assert ctx.node_id is None
        assert ctx.approval_repo is None

    def test_all_fields(self):
        repo = MagicMock()
        ctx = ExecutionContext(
            job_id="j1",
            run_id="r1",
            node_id="n1",
            approval_repo=repo,
        )
        assert ctx.job_id == "j1"
        assert ctx.run_id == "r1"
        assert ctx.node_id == "n1"
        assert ctx.approval_repo is repo


# ---------------------------------------------------------------------------
# TestWorkerAgentNoInstanceContext
# ---------------------------------------------------------------------------


class TestWorkerAgentNoInstanceContext:
    def test_no_current_run_id_field(self):
        worker = _make_worker()
        assert not hasattr(worker, "_current_run_id")

    def test_no_current_node_id_field(self):
        worker = _make_worker()
        assert not hasattr(worker, "_current_node_id")


# ---------------------------------------------------------------------------
# TestContextPropagation
# ---------------------------------------------------------------------------


class TestContextPropagation:
    def test_execute_tool_receives_context(self):
        """_execute_tool should receive context with correct values."""
        worker = _make_worker(job_id="job-42")
        guardrails = MagicMock()
        guardrails.check_and_execute.return_value = ToolResult(
            tool_call_id="", success=True, output="ok",
        )
        worker.guardrails = guardrails

        ctx = ExecutionContext(
            job_id="job-42",
            run_id="run-99",
            node_id="node-7",
            approval_repo=None,
        )
        worker._execute_tool("read", {"file_path": "/tmp/test"}, ctx)

        guardrails.check_and_execute.assert_called_once_with(
            "read",
            {"file_path": "/tmp/test"},
            job_id="job-42",
            run_id="run-99",
            approval_repo=None,
            node_id="node-7",
        )

    def test_execute_tool_without_guardrails(self):
        """Without guardrails, tool goes directly to registry."""
        worker = _make_worker()
        worker.tool_registry.execute = MagicMock(return_value=ToolResult(
            tool_call_id="", success=True, output="data",
        ))
        ctx = ExecutionContext(job_id="j1")
        result = worker._execute_tool("read", {"file_path": "/tmp/x"}, ctx)
        assert result.success
        worker.tool_registry.execute.assert_called_once()


# ---------------------------------------------------------------------------
# TestConcurrentContextIsolation
# ---------------------------------------------------------------------------


class TestConcurrentContextIsolation:
    @pytest.mark.asyncio
    async def test_concurrent_nodes_no_context_crosstalk(self):
        """Two concurrent generator nodes must not have context cross-talk."""
        captured_contexts: list[ExecutionContext] = []

        session_store = MagicMock()
        tool_registry = MagicMock()
        tool_registry.schemas = [{"name": "bash"}, {"name": "write"}]

        guardrails = MagicMock()

        def fake_check_and_execute(
            tool_name, arguments, *,
            job_id="", run_id=None, approval_repo=None, node_id=None,
        ):
            captured_contexts.append(ExecutionContext(
                job_id=job_id,
                run_id=run_id,
                node_id=node_id,
                approval_repo=approval_repo,
            ))
            return ToolResult(tool_call_id="", success=True, output="ok")

        guardrails.check_and_execute = fake_check_and_execute

        worker_a = WorkerAgent(
            capability=_make_capability("generator"),
            llm_config=LLMConfig(api_key="test"),
            session_store=session_store,
            tool_registry=tool_registry,
            guardrails=guardrails,
            job_id="job-concurrent",
        )
        worker_b = WorkerAgent(
            capability=_make_capability("generator"),
            llm_config=LLMConfig(api_key="test"),
            session_store=session_store,
            tool_registry=tool_registry,
            guardrails=guardrails,
            job_id="job-concurrent",
        )

        ctx_a = ExecutionContext(
            job_id="job-concurrent",
            run_id="run-1",
            node_id="gen_alpha",
        )
        ctx_b = ExecutionContext(
            job_id="job-concurrent",
            run_id="run-1",
            node_id="gen_beta",
        )

        # Simulate concurrent tool calls
        results = await asyncio.gather(
            asyncio.to_thread(
                worker_a._execute_tool, "bash", {"command": "echo A"}, ctx_a,
            ),
            asyncio.to_thread(
                worker_b._execute_tool, "bash", {"command": "echo B"}, ctx_b,
            ),
        )

        assert len(results) == 2
        assert len(captured_contexts) == 2

        node_ids = {c.node_id for c in captured_contexts}
        assert node_ids == {"gen_alpha", "gen_beta"}


# ---------------------------------------------------------------------------
# TestAgentPoolAPI
# ---------------------------------------------------------------------------


class TestAgentPoolAPI:
    def test_create_worker_exists(self):
        pool = AgentPool(
            llm_config=LLMConfig(api_key="test"),
            session_store=MagicMock(),
            agent_registry=MagicMock(),
        )
        assert hasattr(pool, "create_worker")

    def test_get_or_create_is_alias(self):
        assert AgentPool.get_or_create is AgentPool.create_worker

    def test_create_worker_returns_fresh_instances(self):
        cap = _make_capability("generator")
        registry = MagicMock()
        registry.get.return_value = cap

        pool = AgentPool(
            llm_config=LLMConfig(api_key="test"),
            session_store=MagicMock(),
            agent_registry=registry,
        )

        a = pool.create_worker("generator")
        b = pool.create_worker("generator")
        assert a is not b


# ---------------------------------------------------------------------------
# TestRuntimeContextInjection
# ---------------------------------------------------------------------------


class TestRuntimeContextInjection:
    def test_build_runtime_context_includes_project_root(self):
        """_build_runtime_context should include PROJECT_ROOT from tool_registry.base_cwd."""
        tool_registry = MagicMock()
        tool_registry.schemas = [{"name": "read"}]
        tool_registry.base_cwd = "/custom/project"

        worker = WorkerAgent(
            capability=_make_capability("generator"),
            llm_config=LLMConfig(api_key="test"),
            session_store=MagicMock(),
            tool_registry=tool_registry,
        )
        ctx = worker._build_runtime_context()
        assert "## Runtime Environment" in ctx
        assert "PROJECT_ROOT: /custom/project" in ctx
        assert "Path rules:" in ctx

    def test_build_runtime_context_falls_back_to_cwd(self):
        """Without base_cwd, should fall back to cwd."""
        tool_registry = MagicMock()
        tool_registry.schemas = [{"name": "read"}]
        del tool_registry.base_cwd

        worker = WorkerAgent(
            capability=_make_capability("generator"),
            llm_config=LLMConfig(api_key="test"),
            session_store=MagicMock(),
            tool_registry=tool_registry,
        )
        ctx = worker._build_runtime_context()
        assert "## Runtime Environment" in ctx
        assert "PROJECT_ROOT:" in ctx

    @pytest.mark.asyncio
    async def test_execute_inner_prompt_includes_runtime_context(self):
        """_execute_inner should inject Runtime Environment into the prompt."""
        tool_registry = MagicMock()
        tool_registry.schemas = [{"name": "read"}, {"name": "write"}]
        tool_registry.base_cwd = "/test/project"

        captured_prompts: list[str] = []

        class FakeWorker:
            artifacts = []

            def run(self, session_id, system_prompt, user_message,
                    tools, tool_executor, max_iterations=50):
                captured_prompts.append(user_message)
                return []

        worker = WorkerAgent(
            capability=_make_capability("generator"),
            llm_config=LLMConfig(api_key="test"),
            session_store=MagicMock(),
            tool_registry=tool_registry,
        )
        worker.worker = FakeWorker()

        await worker._execute_inner(
            task="do something",
            input_artifacts=[],
            session_id="test-session",
        )

        assert len(captured_prompts) == 1
        prompt = captured_prompts[0]
        assert "## Runtime Environment" in prompt
        assert "PROJECT_ROOT: /test/project" in prompt
        assert "Your task: do something" in prompt
