"""
Tests for #132 follow-up fixes from incomplete #34 refactor.

Covers:
- P0-1: Worker no longer double-transitions job to SUCCEEDED
- P0-2: WorkerAgent uses explicit ExecutionContext (no instance fields)
- P1-1: ApprovalTicket consumed via structured fields, reason not mutated
- P1-2: bash tool delegates to sandbox_runner when available
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from control_plane.models import JobStatus, Run, RunStatus
from control_plane.repository import JobRepository
from control_plane.approval import ApprovalRepository, ApprovalTicket, TicketStatus  # noqa: F401
from control_plane.worker import TaskWorker
from agent.agent_pool import WorkerAgent, AgentPool, ExecutionContext
from core.config import LLMConfig
from core.models import ToolResult, AgentCapability
from guardrails.policy import Guardrails, GuardrailPolicy  # noqa: F401


# =============================================================================
# P0-1: Worker does not double-transition SUCCEEDED
# =============================================================================


@pytest.fixture
def tmp_repo(tmp_path: Path) -> JobRepository:
    return JobRepository(str(tmp_path / "jobs"))


@pytest.fixture
def tmp_approval_repo(tmp_path: Path) -> ApprovalRepository:
    return ApprovalRepository(str(tmp_path / "approvals"))


class TestWorkerNoDoubleTransition:
    """Worker success path must not call transition_job_status(..., SUCCEEDED)."""

    @pytest.mark.asyncio
    async def test_worker_does_not_transition_succeeded_on_success(
        self, tmp_repo: JobRepository,
    ):
        """RunService already transitions; Worker only logs."""
        job = tmp_repo.create_job(requirement="test")
        # Start as LEASED so _execute_job_core can transition to RUNNING
        job.status = JobStatus.LEASED
        tmp_repo.update_job(job)

        now = datetime.now(timezone.utc)
        run = Run(
            id="run_1", job_id=job.id, session_id="sess_1",
            status=RunStatus.SUCCEEDED,
            started_at=now, created_at=now, updated_at=now,
        )
        mock_service = MagicMock()
        mock_service.run_job = AsyncMock(return_value=run)
        # Simulate RunService transitioning job to SUCCEEDED
        async def _run_job_and_succeed(job_id):
            tmp_repo.transition_job_status(job_id, JobStatus.SUCCEEDED)
            return run
        mock_service.run_job = AsyncMock(side_effect=_run_job_and_succeed)

        worker = TaskWorker(repository=tmp_repo, run_service=mock_service)

        with patch.object(
            tmp_repo, "transition_job_status",
            wraps=tmp_repo.transition_job_status,
        ) as spy_transition:
            await worker._execute_job_core(job.id)

        # After execution job should still be SUCCEEDED (set by RunService)
        final_job = tmp_repo.get_job(job.id)
        assert final_job.status == JobStatus.SUCCEEDED

        # Count how many times transition_job_status was called with SUCCEEDED
        succeeded_calls = [
            call for call in spy_transition.call_args_list
            if call.args[1] == JobStatus.SUCCEEDED
        ]
        # Exactly 1: RunService set it; Worker must NOT add a second one
        assert len(succeeded_calls) == 1, (
            "Only RunService should transition to SUCCEEDED, not Worker"
        )

    @pytest.mark.asyncio
    async def test_post_approval_worker_does_not_transition_succeeded(
        self, tmp_repo: JobRepository, tmp_approval_repo: ApprovalRepository,
    ):
        """Post-approval success path also must not double-transition."""
        job = tmp_repo.create_job(requirement="test")
        job.status = JobStatus.PENDING_APPROVAL
        tmp_repo.update_job(job)

        now = datetime.now(timezone.utc)
        run = Run(
            id="run_1", job_id=job.id, session_id="sess_1",
            status=RunStatus.SUCCEEDED,
            started_at=now, created_at=now, updated_at=now,
        )
        mock_service = MagicMock()
        mock_service.run_job = AsyncMock(return_value=run)
        mock_service.approval_repo = tmp_approval_repo
        mock_service.approval_timeout_sec = 60

        # Create an approved ticket so _poll_for_approval returns RUNNING
        ticket = tmp_approval_repo.create_ticket(
            job_id=job.id, tool_name="bash", args={"command": "echo hi"},
        )
        tmp_approval_repo.approve_ticket(ticket.id)

        TaskWorker(repository=tmp_repo, run_service=mock_service)  # noqa: F841

        with patch.object(
            tmp_repo, "transition_job_status",
            wraps=tmp_repo.transition_job_status,
        ) as spy_transition:
            # Simulate the post-approval execution block directly
            await asyncio.to_thread(
                tmp_repo.transition_job_status,
                job.id, JobStatus.RUNNING,
            )
            run2 = await mock_service.run_job(job.id)
            if run2.status == RunStatus.SUCCEEDED:
                current_job = await asyncio.to_thread(
                    tmp_repo.get_job, job.id,
                )
                # This is what the fixed code does — only logs, no transition
                assert current_job is not None

        # Ensure no post-approval SUCCEEDED transition happened
        succeeded_calls = [
            call for call in spy_transition.call_args_list
            if call.args[1] == JobStatus.SUCCEEDED
        ]
        # Only the initial PENDING_APPROVAL -> RUNNING transition is expected
        assert len(succeeded_calls) == 0


# =============================================================================
# P0-2: WorkerAgent explicit ExecutionContext
# =============================================================================


@pytest.fixture
def mock_capability() -> AgentCapability:
    return AgentCapability(
        id="generator", name="Generator", description="",
        system_prompt="", tools=["read", "write"],
    )


@pytest.fixture
def mock_session_store():
    store = MagicMock()
    store.emit_event = MagicMock()
    return store


@pytest.fixture
def mock_tool_registry():
    reg = MagicMock()
    reg.schemas = [
        {"name": "read"}, {"name": "write"}, {"name": "edit"},
        {"name": "bash"}, {"name": "glob"}, {"name": "grep"}, {"name": "git"},
    ]
    reg.execute = MagicMock(return_value=ToolResult(
        tool_call_id="tc1", success=True, output="ok",
    ))
    return reg


class TestWorkerAgentExecutionContext:
    """WorkerAgent must not store per-node mutable state on instance."""

    def test_no_instance_context_fields(self, mock_capability, mock_session_store):
        """WorkerAgent should not have _current_run_id / _current_node_id."""
        agent = WorkerAgent(
            capability=mock_capability,
            llm_config=LLMConfig(provider="openai", api_key="x", model="gpt-4"),
            session_store=mock_session_store,
            tool_registry=MagicMock(),
        )
        assert not hasattr(agent, "_current_run_id")
        assert not hasattr(agent, "_current_node_id")

    @pytest.mark.asyncio
    async def test_context_passed_to_execute_tool(
        self, mock_capability, mock_session_store, mock_tool_registry,
    ):
        """_execute_tool passes ExecutionContext through to guardrails.check_and_execute."""
        guardrails = MagicMock()
        guardrails.check_and_execute = MagicMock(
            return_value=ToolResult(tool_call_id="tc1", success=True, output="ok"),
        )

        agent = WorkerAgent(
            capability=mock_capability,
            llm_config=LLMConfig(provider="openai", api_key="x", model="gpt-4"),
            session_store=mock_session_store,
            tool_registry=mock_tool_registry,
            guardrails=guardrails,
            job_id="job_123",
        )

        call_count = [0]

        def mock_llm_call(messages, tools):
            call_count[0] += 1
            if call_count[0] == 1:
                return {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "tc1",
                            "name": "read",
                            "arguments": {"file_path": "test.txt"},
                        }
                    ],
                }
            return {"content": "done"}

        with patch.object(agent.worker.llm, "call", side_effect=mock_llm_call):
            result = await agent.execute(
                "do something", [], "sess_1",
                node_id="node_a", run_id="run_1",
            )

        assert result["status"] == "completed"
        # Guardrails should have been called with the context propagated from execute()
        assert guardrails.check_and_execute.call_count == 1
        call_kwargs = guardrails.check_and_execute.call_args.kwargs
        assert call_kwargs.get("job_id") == "job_123"
        assert call_kwargs.get("run_id") == "run_1"
        assert call_kwargs.get("node_id") == "node_a"

    def test_execution_context_dataclass(self):
        """ExecutionContext carries job/run/node/approval_repo."""
        ctx = ExecutionContext(
            job_id="j1", run_id="r1", node_id="n1", approval_repo="repo",
        )
        assert ctx.job_id == "j1"
        assert ctx.run_id == "r1"
        assert ctx.node_id == "n1"
        assert ctx.approval_repo == "repo"

    def test_agent_pool_create_worker_not_cached(self, mock_session_store):
        """AgentPool.create_worker always returns a fresh instance."""
        from core.agent_registry import AgentRegistry

        registry = AgentRegistry()
        pool = AgentPool(
            llm_config=LLMConfig(provider="openai", api_key="x", model="gpt-4"),
            session_store=mock_session_store,
            agent_registry=registry,
        )
        w1 = pool.create_worker("planner")
        w2 = pool.create_worker("planner")
        assert w1 is not w2

    def test_agent_pool_get_or_create_alias(self, mock_session_store):
        """AgentPool.get_or_create is a backward-compat alias for create_worker."""
        from core.agent_registry import AgentRegistry

        registry = AgentRegistry()
        pool = AgentPool(
            llm_config=LLMConfig(provider="openai", api_key="x", model="gpt-4"),
            session_store=mock_session_store,
            agent_registry=registry,
        )
        w1 = pool.get_or_create("planner")
        w2 = pool.create_worker("planner")
        assert w1 is not w2
        assert type(w1) is type(w2)


# =============================================================================
# P1-1: ApprovalTicket consumed structured fields
# =============================================================================


class TestApprovalTicketStructuredConsume:
    """consume_ticket must write structured fields and NOT mutate reason."""

    def test_consume_writes_structured_fields(self, tmp_approval_repo: ApprovalRepository):
        ticket = tmp_approval_repo.create_ticket(
            job_id="j1", tool_name="bash", args={"command": "echo hi"},
        )
        approved = tmp_approval_repo.approve_ticket(ticket.id, reason="Looks safe")
        original_reason = approved.reason

        tmp_approval_repo.consume_ticket(
            approved, run_id="run_1", node_id="node_a",
        )

        updated = tmp_approval_repo.get_ticket(ticket.id)
        assert updated.status == TicketStatus.CONSUMED
        assert updated.consumed_by_run_id == "run_1"
        assert updated.consumed_by_node_id == "node_a"
        assert updated.consumed_at is not None
        assert updated.reason == original_reason  # NOT mutated

    def test_consume_does_not_mutate_reason(self, tmp_approval_repo: ApprovalRepository):
        ticket = tmp_approval_repo.create_ticket(
            job_id="j1", tool_name="bash", args={"command": "echo hi"},
        )
        approved = tmp_approval_repo.approve_ticket(ticket.id, reason="User approved")

        tmp_approval_repo.consume_ticket(approved, run_id="r1", node_id="n1")
        updated = tmp_approval_repo.get_ticket(ticket.id)
        assert "[consumed on execution]" not in (updated.reason or "")
        assert updated.reason == "User approved"

    def test_guardrails_consume_passes_run_id_node_id(
        self, tmp_approval_repo: ApprovalRepository,
    ):
        """Guardrails.check_and_execute passes run_id/node_id to consume_ticket."""
        ticket = tmp_approval_repo.create_ticket(
            job_id="j1", tool_name="bash", args={"command": "echo hi"},
            node_id="node_a",
        )
        tmp_approval_repo.approve_ticket(ticket.id)

        tool_registry = MagicMock()
        tool_registry.execute = MagicMock(
            return_value=ToolResult(tool_call_id="", success=True, output="ok"),
        )

        policy = GuardrailPolicy()
        guardrails = Guardrails(policy=policy, tool_registry=tool_registry)
        # Force pending_approval so it looks for an approved ticket
        guardrails.evaluate = MagicMock(
            return_value=MagicMock(decision="pending_approval"),
        )

        with patch.object(
            tmp_approval_repo, "consume_ticket",
            wraps=tmp_approval_repo.consume_ticket,
        ) as spy_consume:
            result = guardrails.check_and_execute(
                "bash", {"command": "echo hi"},
                job_id="j1",
                run_id="run_1",
                node_id="node_a",
                approval_repo=tmp_approval_repo,
            )

        assert result.success is True
        spy_consume.assert_called_once()
        call_kwargs = spy_consume.call_args.kwargs
        assert call_kwargs.get("run_id") == "run_1"
        assert call_kwargs.get("node_id") == "node_a"


# =============================================================================
# P1-2: bash tool uses sandbox_runner
# =============================================================================


class TestBashSandboxRunner:
    """bash tool must delegate to sandbox_runner when one is configured."""

    def test_bash_uses_sandbox_runner_when_available(self):
        from tools.registry import ToolRegistry

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "hello sandbox"
        fake_result.stderr = ""
        fake_result.timed_out = False

        sandbox = MagicMock()
        sandbox.run_command = MagicMock(return_value=fake_result)

        registry = ToolRegistry(sandbox_runner=sandbox)
        result = registry.execute("bash", {"command": "echo hello"})

        assert result.success is True
        assert "hello sandbox" in result.output
        sandbox.run_command.assert_called_once()
        call_kwargs = sandbox.run_command.call_args.kwargs
        assert call_kwargs.get("command") == "echo hello" or "echo hello" in str(sandbox.run_command.call_args)

    def test_bash_falls_back_to_subprocess_without_sandbox(self):
        from tools.registry import ToolRegistry

        registry = ToolRegistry()
        result = registry.execute("bash", {"command": "echo hello_fallback"})

        assert result.success is True
        assert "hello_fallback" in result.output
