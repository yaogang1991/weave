"""Unit tests for agent/agent_pool.py — AgentPool and WorkerAgent.

Covers: worker creation, tool filtering, guardrails integration,
execution context isolation, artifact formatting, runtime context.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from core.config import LLMConfig
from core.models import (
    AgentCapability,
    DAGNode,
    HandoffArtifact,
    NodeStatus,
    ToolResult,
)
from agent.agent_pool import AgentPool, WorkerAgent, ExecutionContext, _inject_file_path_constraints
from tools.registry import ToolRegistry
from guardrails.policy import Guardrails, GuardrailPolicy, PermissionMode


# -- Helpers ---------------------------------------------------------------

def _make_llm_config() -> LLMConfig:
    return LLMConfig(api_key="test-key", model="test-model", provider="anthropic")


def _make_registry() -> MagicMock:
    reg = MagicMock()
    reg.list_agents.return_value = [
        AgentCapability(id="generator", name="Generator", description="writes code"),
        AgentCapability(id="evaluator", name="Evaluator", description="reviews code"),
        AgentCapability(id="planner", name="Planner", description="plans tasks"),
    ]
    reg.get.side_effect = lambda t: next(
        (a for a in reg.list_agents.return_value if a.id == t), None
    )
    return reg


def _make_tool_registry() -> ToolRegistry:
    tr = ToolRegistry()
    return tr


def _make_pool(**overrides) -> AgentPool:
    store = MagicMock()
    with patch("agent.agent_pool.AgentWorker"):
        pool = AgentPool(
            llm_config=_make_llm_config(),
            session_store=store,
            agent_registry=_make_registry(),
            tool_registry=_make_tool_registry(),
            **overrides,
        )
    return pool


# -- Test Classes -----------------------------------------------------------

class TestAgentPoolCreateWorker:
    """AgentPool.create_worker creates isolated WorkerAgent instances."""

    def test_create_worker_for_known_type(self):
        pool = _make_pool()
        worker = pool.create_worker("generator")

        assert isinstance(worker, WorkerAgent)
        assert worker.capability.id == "generator"

    def test_create_worker_unknown_type_raises(self):
        pool = _make_pool()
        with pytest.raises(ValueError, match="Unknown agent type"):
            pool.create_worker("nonexistent")

    def test_create_worker_always_new_instance(self):
        pool = _make_pool()
        w1 = pool.create_worker("generator")
        w2 = pool.create_worker("generator")

        assert w1 is not w2

    def test_backward_compat_alias(self):
        pool = _make_pool()
        # get_or_create is a class-level alias, verify it delegates
        assert pool.get_or_create.__func__ is AgentPool.create_worker

    def test_create_worker_with_custom_registry(self):
        pool = _make_pool()
        custom_tr = _make_tool_registry()
        worker = pool.create_worker("generator", tool_registry=custom_tr)

        assert worker.tool_registry is custom_tr


class TestExecutionContext:
    """ExecutionContext dataclass defaults."""

    def test_defaults(self):
        ctx = ExecutionContext()
        assert ctx.job_id == ""
        assert ctx.run_id is None
        assert ctx.node_id is None
        assert ctx.approval_repo is None

    def test_custom_values(self):
        ctx = ExecutionContext(job_id="j1", run_id="r1", node_id="n1")
        assert ctx.job_id == "j1"
        assert ctx.run_id == "r1"
        assert ctx.node_id == "n1"


class TestWorkerAgentToolFiltering:
    """WorkerAgent filters tools by agent type allowlist."""

    def test_generator_gets_allowed_tools(self):
        reg = _make_registry()
        tr = _make_tool_registry()
        cap = reg.get("generator")
        with patch("agent.agent_pool.AgentWorker"):
            agent = WorkerAgent(
                capability=cap,
                llm_config=_make_llm_config(),
                session_store=MagicMock(),
                tool_registry=tr,
            )
        tool_names = {s["name"] for s in agent.tools}
        assert len(agent.tools) > 0

    def test_unknown_agent_gets_default_tools(self):
        reg = MagicMock()
        cap = AgentCapability(id="custom_agent", name="Custom", description="custom")
        reg.get.return_value = cap
        tr = _make_tool_registry()
        with patch("agent.agent_pool.AgentWorker"):
            agent = WorkerAgent(
                capability=cap,
                llm_config=_make_llm_config(),
                session_store=MagicMock(),
                tool_registry=tr,
            )
        tool_names = {s["name"] for s in agent.tools}
        assert tool_names == {"read", "glob", "grep"}


class TestWorkerAgentGuardrails:
    """_execute_tool routes through guardrails when configured."""

    def test_guardrails_blocks_tool(self):
        cap = AgentCapability(id="generator", name="Gen", description="gen")
        tr = _make_tool_registry()
        guardrails = MagicMock(spec=Guardrails)
        from guardrails.policy import GuardrailResult
        guardrails.check_and_execute.return_value = GuardrailResult(
            decision="blocked",
            reason="blocked by policy",
        )

        with patch("agent.agent_pool.AgentWorker"):
            agent = WorkerAgent(
                capability=cap,
                llm_config=_make_llm_config(),
                session_store=MagicMock(),
                tool_registry=tr,
                guardrails=guardrails,
            )

        ctx = ExecutionContext()
        result = agent._execute_tool("bash", {"command": "rm -rf /"}, ctx)

        assert result.success is False
        assert "blocked" in result.error.lower()

    def test_no_guardrails_calls_registry_directly(self):
        cap = AgentCapability(id="generator", name="Gen", description="gen")
        tr = _make_tool_registry()
        with patch("agent.agent_pool.AgentWorker"):
            agent = WorkerAgent(
                capability=cap,
                llm_config=_make_llm_config(),
                session_store=MagicMock(),
                tool_registry=tr,
            )

        ctx = ExecutionContext()
        result = agent._execute_tool("read", {"file_path": "/nonexistent"}, ctx)
        assert isinstance(result, ToolResult)

    def test_pending_approval_raises_exception(self):
        cap = AgentCapability(id="generator", name="Gen", description="gen")
        tr = _make_tool_registry()
        guardrails = MagicMock(spec=Guardrails)
        from guardrails.policy import GuardrailResult
        from core.exceptions import PendingApprovalError
        guardrails.check_and_execute.return_value = GuardrailResult(
            decision="pending_approval",
            reason="high-risk operation",
            ticket_id="ticket-123",
        )

        with patch("agent.agent_pool.AgentWorker"):
            agent = WorkerAgent(
                capability=cap,
                llm_config=_make_llm_config(),
                session_store=MagicMock(),
                tool_registry=tr,
                guardrails=guardrails,
            )

        ctx = ExecutionContext()
        with pytest.raises(PendingApprovalError) as exc_info:
            agent._execute_tool("bash", {"command": "rm -rf /"}, ctx)

        assert exc_info.value.ticket_id == "ticket-123"


class TestArtifactFormatting:
    """_format_artifacts builds context from HandoffArtifact list."""

    def test_empty_artifacts(self):
        cap = AgentCapability(id="generator", name="Gen", description="gen")
        with patch("agent.agent_pool.AgentWorker"):
            agent = WorkerAgent(
                capability=cap,
                llm_config=_make_llm_config(),
                session_store=MagicMock(),
                tool_registry=_make_tool_registry(),
            )

        assert agent._format_artifacts([]) == ""

    def test_single_artifact(self):
        cap = AgentCapability(id="generator", name="Gen", description="gen")
        with patch("agent.agent_pool.AgentWorker"):
            agent = WorkerAgent(
                capability=cap,
                llm_config=_make_llm_config(),
                session_store=MagicMock(),
                tool_registry=_make_tool_registry(),
            )

        art = HandoffArtifact(
            from_agent="planner",
            to_agent="generator",
            content="Created API module",
            file_paths=["src/api.py"],
        )
        result = agent._format_artifacts([art])

        assert "planner" in result
        assert "Created API module" in result
        assert "src/api.py" in result


class TestRuntimeContext:
    """_build_runtime_context injects environment info."""

    def test_includes_os_and_python(self):
        cap = AgentCapability(id="generator", name="Gen", description="gen")
        with patch("agent.agent_pool.AgentWorker"):
            agent = WorkerAgent(
                capability=cap,
                llm_config=_make_llm_config(),
                session_store=MagicMock(),
                tool_registry=_make_tool_registry(),
            )

        ctx = agent._build_runtime_context()

        assert "OS:" in ctx
        assert "PYTHON:" in ctx
        assert "PROJECT_ROOT:" in ctx
        assert "Path rules:" in ctx


class TestInjectFilePathConstraints:
    """_inject_file_path_constraints prepends file path constraints."""

    def test_non_generator_unchanged(self):
        node = DAGNode(
            id="n1",
            agent_type="evaluator",
            task_description="check tests",
        )
        result = _inject_file_path_constraints(node)
        assert result == "check tests"

    def test_generator_without_criteria_unchanged(self):
        node = DAGNode(
            id="n1",
            agent_type="generator",
            task_description="build API",
        )
        result = _inject_file_path_constraints(node)
        assert result == "build API"


class TestGetExecutor:
    """AgentPool.get_executor returns a callable node executor."""

    @pytest.mark.asyncio
    async def test_executor_returns_callable(self):
        pool = _make_pool()
        executor = pool.get_executor("sess1")

        assert callable(executor)

    @pytest.mark.asyncio
    async def test_executor_creates_worker_per_call(self):
        pool = _make_pool()
        original_create = pool.create_worker
        create_count = 0

        def counting_create(agent_type, **kw):
            nonlocal create_count
            create_count += 1
            return original_create(agent_type, **kw)

        pool.create_worker = counting_create
        executor = pool.get_executor("sess1")

        node = DAGNode(
            id="n1",
            agent_type="generator",
            task_description="build API",
        )

        with patch.object(WorkerAgent, "execute", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = {"status": "completed", "output": "ok", "artifacts": []}
            result = await executor(node, [])

        assert create_count == 1
        assert result["status"] == "completed"


class TestIsApiError:
    """AgentPool._is_api_error classifies exceptions by type name."""

    def test_auth_error(self):
        pool = _make_pool()
        exc = type("AuthenticationError", (Exception,), {})("auth failed")
        assert pool._is_api_error(exc) is True

    def test_rate_limit(self):
        pool = _make_pool()
        exc = type("RateLimitError", (Exception,), {})("rate limited")
        assert pool._is_api_error(exc) is True

    def test_connection_error(self):
        pool = _make_pool()
        exc = type("ConnectionError", (Exception,), {})("conn failed")
        assert pool._is_api_error(exc) is True

    def test_generic_error(self):
        pool = _make_pool()
        assert pool._is_api_error(ValueError("bad value")) is False

    def test_runtime_error(self):
        pool = _make_pool()
        assert pool._is_api_error(RuntimeError("task failed")) is False
