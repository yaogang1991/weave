"""Tests for #328: incremental progress preservation across run retries.

Verifies:
1. Retry context is injected into project_context when job.attempt > 0
2. No retry context on first attempt (attempt 0)
3. Planning prompt includes retry continuation instructions
4. Planning prompt rule 16 exists for retry continuity
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


from control_plane.models import Job, JobStatus, RetryPolicy


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _make_job(attempt: int = 0, project_path: str = "/tmp/test_project") -> Job:
    return Job(
        id="job-test",
        requirement="Build a REST API",
        status=JobStatus.RUNNING,
        project_path=project_path,
        retry_policy=RetryPolicy(max_attempts=3),
        attempt=attempt,
        created_at=_utc_now(),
        updated_at=_utc_now(),
    )


class TestRetryContextInjection:
    """Verify retry_attempt is added to project_context when job has retries."""

    def test_retry_context_injected_on_attempt_1(self, tmp_path):
        """When job.attempt > 0, project_context gets retry_attempt."""
        from control_plane.service import RunService

        job = _make_job(attempt=1)
        # Create some existing files
        (tmp_path / "core.py").write_text("x = 1\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_core.py").write_text("def test_x(): pass\n")

        # We'll trace what gets passed to orchestrator.plan()
        captured_context = {}

        async def fake_plan(requirement, project_context=None):
            captured_context.update(project_context or {})
            dag = MagicMock()
            dag.nodes = {}
            dag.edges = []
            return dag

        with patch.object(RunService, "__init__", lambda self, **kw: None):
            svc = RunService.__new__(RunService)
            svc.repository = MagicMock()
            svc.repository.update_job = MagicMock()
            mock_factory = MagicMock()
            orchestrator = MagicMock()
            orchestrator.plan = fake_plan
            orchestrator.replan = AsyncMock(return_value=MagicMock(nodes={}, edges=[]))
            mock_factory.create_orchestrator.return_value = orchestrator
            svc._execution_factory = mock_factory
            svc._run_before_hooks = AsyncMock()
            svc._run_after_hooks = AsyncMock()

            engine = MagicMock()
            engine.execute = AsyncMock(return_value=MagicMock(nodes={}, edges=[]))
            mock_factory.create_execution_engine.return_value = engine
            svc.artifact_path = tmp_path
            svc.llm_config = MagicMock()
            svc.approval_repo = None
            svc.memory_manager = None
            svc.event_store_path = str(tmp_path / "events")
            svc._running_tasks = {}

            import asyncio
            asyncio.run(
                svc._execute_plan_and_run(
                    job=job,
                    session_id="sess-test",
                    store=MagicMock(),
                    work_dir=tmp_path,
                )
            )

        assert "retry_attempt" in captured_context
        assert captured_context["retry_attempt"] == 1
        assert captured_context["max_attempts"] == 3

    def test_no_retry_context_on_first_attempt(self, tmp_path):
        """When job.attempt == 0, no retry_attempt in project_context."""
        from control_plane.service import RunService

        job = _make_job(attempt=0)

        captured_context = {}

        async def fake_plan(requirement, project_context=None):
            captured_context.update(project_context or {})
            dag = MagicMock()
            dag.nodes = {}
            dag.edges = []
            return dag

        with patch.object(RunService, "__init__", lambda self, **kw: None):
            svc = RunService.__new__(RunService)
            svc.repository = MagicMock()
            svc.repository.update_job = MagicMock()
            mock_factory = MagicMock()
            orchestrator = MagicMock()
            orchestrator.plan = fake_plan
            orchestrator.replan = AsyncMock(return_value=MagicMock(nodes={}, edges=[]))
            mock_factory.create_orchestrator.return_value = orchestrator
            svc._execution_factory = mock_factory
            svc._run_before_hooks = AsyncMock()
            svc._run_after_hooks = AsyncMock()

            engine = MagicMock()
            engine.execute = AsyncMock(return_value=MagicMock(nodes={}, edges=[]))
            mock_factory.create_execution_engine.return_value = engine
            svc.artifact_path = tmp_path
            svc.llm_config = MagicMock()
            svc.approval_repo = None
            svc.memory_manager = None
            svc.event_store_path = str(tmp_path / "events")
            svc._running_tasks = {}

            import asyncio
            asyncio.run(
                svc._execute_plan_and_run(
                    job=job,
                    session_id="sess-test",
                    store=MagicMock(),
                    work_dir=tmp_path,
                )
            )

        assert "retry_attempt" not in captured_context


class TestRetryContextInPrompt:
    """Verify the orchestrator includes retry context in the planning prompt."""

    def test_retry_context_appears_in_prompt(self):
        """When project_context has retry_attempt > 0, prompt includes continuation."""
        from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
        from core.agent_registry import AgentRegistry

        # Capture what gets sent to LLM
        captured_messages = []

        def fake_call(messages, tools=None, **kwargs):
            captured_messages.extend(messages)
            return {"content": json.dumps({
                "reasoning": "test",
                "nodes": [{"id": "n1", "agent_type": "generator", "task": "t"}],
                "edges": [],
            })}

        registry = AgentRegistry()
        orchestrator = IntelligentOrchestrator.__new__(IntelligentOrchestrator)
        orchestrator.agent_registry = registry
        orchestrator.llm = MagicMock()
        orchestrator.llm.call = fake_call
        orchestrator.learning_optimizer = None
        orchestrator.llm_config = MagicMock()
        orchestrator.llm_config.model = "claude-sonnet-4-6"
        orchestrator.skill_registry = None
        orchestrator._prompt_registry = MagicMock()
        orchestrator._prompt_registry.load.return_value = "System prompt: {agent_descriptions}"

        import asyncio
        asyncio.run(
            orchestrator.plan(
                requirement="Build API",
                project_context={
                    "retry_attempt": 1,
                    "max_attempts": 3,
                    "existing_file_count": 5,
                    "project_path": "/tmp/test",
                },
            )
        )

        user_msg = captured_messages[1]["content"]
        assert "Retry Context" in user_msg
        assert "attempt 2** of 3" in user_msg
        assert "DO NOT start from scratch" in user_msg
        assert "5 files already exist" in user_msg

    def test_no_retry_context_on_first_attempt(self):
        """When retry_attempt is 0 or absent, no retry context in prompt."""
        from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
        from core.agent_registry import AgentRegistry

        captured_messages = []

        def fake_call(messages, tools=None, **kwargs):
            captured_messages.extend(messages)
            return {"content": json.dumps({
                "reasoning": "test",
                "nodes": [{"id": "n1", "agent_type": "generator", "task": "t"}],
                "edges": [],
            })}

        registry = AgentRegistry()
        orchestrator = IntelligentOrchestrator.__new__(IntelligentOrchestrator)
        orchestrator.agent_registry = registry
        orchestrator.llm = MagicMock()
        orchestrator.llm.call = fake_call
        orchestrator.learning_optimizer = None
        orchestrator.llm_config = MagicMock()
        orchestrator.llm_config.model = "claude-sonnet-4-6"
        orchestrator.skill_registry = None
        orchestrator._prompt_registry = MagicMock()
        orchestrator._prompt_registry.load.return_value = "System prompt: {agent_descriptions}"

        import asyncio
        asyncio.run(
            orchestrator.plan(
                requirement="Build API",
                project_context={"project_path": "/tmp/test"},
            )
        )

        user_msg = captured_messages[1]["content"]
        assert "Retry Context" not in user_msg


class TestPlanningPromptRule:
    """Verify planning prompt includes retry continuity rule."""

    def test_planning_prompt_has_retry_continuity_rule(self):
        prompt = Path("orchestrator/prompts/planning.md").read_text()
        assert "Retry continuity" in prompt
        assert "completion plan" in prompt
        assert "full rebuild" in prompt
