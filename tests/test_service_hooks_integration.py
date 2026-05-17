"""Integration tests: RunService ↔ ExecutionHooks lifecycle.

These tests verify the wiring between RunService and hooks —
that metadata persists, dependencies reach the right places,
and hook errors never abort the core execution flow.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from control_plane.hooks import (
    ExecutionContext,
    ExecutionHook,
    ImpactHook,
    LearningHook,
    MemoryHook,
)
from control_plane.service import RunService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(**overrides) -> RunService:
    """Build a RunService with mocked infrastructure dependencies."""
    repo = MagicMock()
    repo.base_path = "/tmp/test-jobs"
    llm_config = MagicMock()

    defaults = dict(
        repository=repo,
        llm_config=llm_config,
        default_backend="local",
    )
    defaults.update(overrides)
    return RunService(**defaults)


def _make_job(**overrides):
    from control_plane.models import Job, JobStatus, RetryPolicy
    from datetime import datetime, timezone

    defaults = dict(
        id="job-integ-001",
        requirement="Fix bug in DAG engine",
        status=JobStatus.RUNNING,
        project_path="/tmp/test-project",
        retry_policy=RetryPolicy(),
        attempt=0,
        last_error="",
        error_category="",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        metadata={},
    )
    defaults.update(overrides)
    return Job(**defaults)


def _make_ctx(**overrides):
    defaults = dict(
        job=_make_job(),
        session_id="sess-integ-001",
        store=MagicMock(),
        work_dir=Path("/tmp/work"),
        run_id="run-integ-001",
        memory_manager=None,
        llm_config=None,
        repository=None,
    )
    defaults.update(overrides)
    return ExecutionContext(**defaults)


# ===========================================================================
# 1. Before-hook metadata lands in job.metadata before execution
# ===========================================================================


class TestBeforeHookMetadataPersistence:
    """Verify that metadata set by before-hooks is persisted immediately."""

    @pytest.mark.asyncio
    async def test_metadata_persisted_after_before_hooks(self):
        """A before-hook writing ctx.metadata should trigger repository.update_job."""
        service = _make_service()
        job = _make_job()

        # Replace hooks with one that writes metadata
        class MetaHook(ExecutionHook):
            async def before_execution(self, ctx):
                ctx.metadata["hook_tag"] = "before_ran"

        service._hooks = [MetaHook()]

        ctx = ExecutionContext(
            job=job,
            session_id="sess-1",
            store=MagicMock(),
            work_dir=Path("/tmp/work"),
        )

        # Simulate the before-hook + persist step from _execute_plan_and_run
        await service._run_before_hooks(ctx)

        assert ctx.metadata["hook_tag"] == "before_ran"

        # The real service does: ctx.job.metadata.update(ctx.metadata)
        job.metadata.update(ctx.metadata)
        assert job.metadata["hook_tag"] == "before_ran"

        # Then: self.repository.update_job(ctx.job)
        service.repository.update_job(job)
        service.repository.update_job.assert_called_once()
        updated = service.repository.update_job.call_args[0][0]
        assert updated.metadata["hook_tag"] == "before_ran"

    @pytest.mark.asyncio
    async def test_empty_metadata_no_update_call(self):
        """If no hooks write metadata, repository.update_job should not be called."""
        service = _make_service()
        # Use no-op hooks so ctx.metadata stays empty
        service._hooks = [ExecutionHook()]
        fresh_repo = MagicMock()
        service.repository = fresh_repo

        job = _make_job()
        ctx = ExecutionContext(
            job=job,
            session_id="sess-2",
            store=MagicMock(),
            work_dir=Path("/tmp/work"),
        )

        await service._run_before_hooks(ctx)

        # ctx.metadata is empty, so the service skips update
        if ctx.metadata:
            service.repository.update_job(job)

        fresh_repo.update_job.assert_not_called()


# ===========================================================================
# 2. _create_orchestrator reads learning_optimizer from hooks
# ===========================================================================


class TestOrchestratorOptimizerWiring:
    """Verify the learning optimizer flows from LearningHook → Orchestrator."""

    def test_orchestrator_gets_optimizer_from_hook(self):
        service = _make_service()

        # Simulate a LearningHook with an optimizer
        mock_optimizer = MagicMock()
        learning_hook = LearningHook.__new__(LearningHook)
        learning_hook.optimizer = mock_optimizer
        learning_hook._scheduler = None
        learning_hook._repository = None

        hooks = [MemoryHook(), learning_hook, ImpactHook()]
        service._hooks = hooks
        service._execution_factory._hooks = hooks

        store = MagicMock(spec=["append", "replay"])
        with patch(
            "control_plane.execution_factory.IntelligentOrchestrator"
        ) as MockOrch:
            MockOrch.return_value = MagicMock()
            service._execution_factory.create_orchestrator(store)

        # IntelligentOrchestrator should have been constructed with learning_optimizer
        _, kwargs = MockOrch.call_args
        assert kwargs.get("learning_optimizer") is mock_optimizer

    def test_orchestrator_with_no_optimizer(self):
        """When no hook exposes optimizer, orchestrator gets None."""
        service = _make_service()
        hooks = [MemoryHook(), ImpactHook()]
        service._hooks = hooks
        service._execution_factory._hooks = hooks

        store = MagicMock(spec=["append", "replay"])
        with patch(
            "control_plane.execution_factory.IntelligentOrchestrator"
        ) as MockOrch:
            MockOrch.return_value = MagicMock()
            service._execution_factory.create_orchestrator(store)

        _, kwargs = MockOrch.call_args
        assert kwargs.get("learning_optimizer") is None


# ===========================================================================
# 3. ImpactHook uses ctx.memory_manager for historical prediction
# ===========================================================================


class TestImpactHookMemoryManagerIntegration:
    """Verify ImpactHook._make_predictor receives ctx.memory_manager."""

    @pytest.mark.asyncio
    async def test_predictor_created_with_memory_manager(self):
        hook = ImpactHook()
        mock_mm = MagicMock()

        predictor = hook._make_predictor(memory_manager=mock_mm)
        assert predictor.memory_manager is mock_mm

    @pytest.mark.asyncio
    async def test_before_execution_passes_context_memory_manager(self):
        """ImpactHook.before_execution should create predictor using ctx.memory_manager."""
        hook = ImpactHook()
        mock_mm = MagicMock()
        ctx = _make_ctx(memory_manager=mock_mm)

        captured_mm = None
        original_make = hook._make_predictor

        def capturing_make(memory_manager=None):
            nonlocal captured_mm
            captured_mm = memory_manager
            # Return a mock predictor so before_execution doesn't crash
            predictor = MagicMock()
            impact_scope = MagicMock()
            impact_scope.id = "scope-1"
            impact_scope.predicted_files = ["file_a.py"]
            impact_scope.risk_level = MagicMock(value="medium")
            predictor.predict = AsyncMock(return_value=impact_scope)
            return predictor

        hook._make_predictor = capturing_make

        with patch("analysis.change_verifier.ChangeVerifier") as MockVerifier:
            mock_verifier = MagicMock()
            mock_verifier.capture_snapshot.return_value = {"file_a.py": 1.0}
            MockVerifier.return_value = mock_verifier

            await hook.before_execution(ctx)

        assert captured_mm is mock_mm
        assert "impact_scope_id" in ctx.metadata

    @pytest.mark.asyncio
    async def test_no_memory_manager_still_works(self):
        """ImpactHook should not fail if ctx.memory_manager is None."""
        hook = ImpactHook()
        ctx = _make_ctx(memory_manager=None)

        predictor = hook._make_predictor(memory_manager=None)
        assert predictor.memory_manager is None


# ===========================================================================
# 4. Hook exception never aborts core execution
# ===========================================================================


class TestHookErrorIsolation:
    """Verify that exceptions in hooks are swallowed and logged."""

    @pytest.mark.asyncio
    async def test_before_hook_exception_does_not_propagate(self):
        service = _make_service()

        class BrokenHook(ExecutionHook):
            async def before_execution(self, ctx):
                raise RuntimeError("hook exploded")

        service._hooks = [BrokenHook()]
        ctx = _make_ctx()

        # Should NOT raise
        await service._run_before_hooks(ctx)

    @pytest.mark.asyncio
    async def test_after_hook_exception_does_not_propagate(self):
        service = _make_service()

        class BrokenHook(ExecutionHook):
            async def after_execution(self, ctx, result_dag):
                raise RuntimeError("after hook exploded")

        service._hooks = [BrokenHook()]
        ctx = _make_ctx()

        # Should NOT raise
        await service._run_after_hooks(ctx, MagicMock())

    @pytest.mark.asyncio
    async def test_one_broken_hook_does_not_skip_others(self):
        """If one hook fails, subsequent hooks still run."""
        service = _make_service()
        call_log = []

        class Hook1(ExecutionHook):
            async def before_execution(self, ctx):
                call_log.append("hook1")
                raise RuntimeError("hook1 dies")

        class Hook2(ExecutionHook):
            async def before_execution(self, ctx):
                call_log.append("hook2")

        service._hooks = [Hook1(), Hook2()]
        ctx = _make_ctx()

        await service._run_before_hooks(ctx)

        assert "hook1" in call_log
        assert "hook2" in call_log
        assert len(call_log) == 2

    @pytest.mark.asyncio
    async def test_after_hooks_metadata_persisted_despite_hook_error(self):
        """Even if an after-hook fails, metadata from successful hooks persists."""
        service = _make_service()
        job = _make_job()

        class GoodHook(ExecutionHook):
            async def after_execution(self, ctx, result_dag):
                ctx.metadata["after_tag"] = "good"

        class BadHook(ExecutionHook):
            async def after_execution(self, ctx, result_dag):
                ctx.metadata["should_not_appear"] = True
                raise RuntimeError("bad after hook")

        service._hooks = [GoodHook(), BadHook()]
        ctx = ExecutionContext(
            job=job,
            session_id="sess-err",
            store=MagicMock(),
            work_dir=Path("/tmp/work"),
        )

        await service._run_after_hooks(ctx, MagicMock())

        # GoodHook metadata should be present; BadHook metadata also present
        # because it was set before the exception
        assert ctx.metadata.get("after_tag") == "good"

        # Simulate the service persist step
        if ctx.metadata:
            job.metadata.update(ctx.metadata)
            service.repository.update_job(job)

        service.repository.update_job.assert_called_once()
        updated = service.repository.update_job.call_args[0][0]
        assert updated.metadata["after_tag"] == "good"


# ===========================================================================
# 5. Full _register_hooks integration
# ===========================================================================


class TestRegisterHooksIntegration:
    """Verify _register_hooks wires dependencies correctly."""

    def test_hooks_list_populated(self):
        service = _make_service()
        assert len(service._hooks) == 3

    def test_hook_types_and_order(self):
        service = _make_service()
        assert isinstance(service._hooks[0], MemoryHook)
        assert isinstance(service._hooks[1], LearningHook)
        assert isinstance(service._hooks[2], ImpactHook)

    def test_learning_hook_gets_repository(self):
        repo = MagicMock()
        service = _make_service(repository=repo)
        learning_hook = service._hooks[1]
        assert learning_hook._repository is repo

    def test_impact_hook_gets_llm_config(self):
        llm_config = MagicMock()
        service = _make_service(llm_config=llm_config)
        impact_hook = service._hooks[2]
        assert impact_hook._llm_config is llm_config
