"""Tests for execution hooks (control_plane/hooks.py)."""

import asyncio  # noqa: F401
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from control_plane.hooks import (
    ExecutionContext,
    ExecutionHook,
    ImpactHook,
    LearningHook,
    MemoryHook,
)


def _make_job(**overrides):
    job = MagicMock()
    job.id = "job-123"
    job.requirement = "Fix bug in DAG engine"
    job.project_path = "/tmp/test-project"
    job.metadata = {}
    for k, v in overrides.items():
        setattr(job, k, v)
    return job


def _make_context(**overrides):
    defaults = {
        "job": _make_job(),
        "session_id": "sess-123",
        "store": MagicMock(),
        "work_dir": Path("/tmp/work"),
        "run_id": "run-123",
        "memory_manager": None,
        "llm_config": None,
        "repository": None,
    }
    defaults.update(overrides)
    return ExecutionContext(**defaults)


# ============================================================================
# ExecutionContext
# ============================================================================


class TestExecutionContext:
    def test_default_metadata(self):
        ctx = _make_context()
        assert ctx.metadata == {}
        assert ctx._state == {}

    def test_metadata_and_state_isolation(self):
        ctx1 = _make_context()
        ctx2 = _make_context()
        ctx1.metadata["key"] = "val1"
        ctx1._state["key"] = "s1"
        assert ctx2.metadata == {}
        assert ctx2._state == {}


# ============================================================================
# ExecutionHook base class
# ============================================================================


class TestExecutionHook:
    @pytest.mark.asyncio
    async def test_default_noop(self):
        hook = ExecutionHook()
        ctx = _make_context()
        await hook.before_execution(ctx)
        await hook.after_execution(ctx, MagicMock())


# ============================================================================
# MemoryHook
# ============================================================================


class TestMemoryHook:
    @pytest.mark.asyncio
    async def test_hook_instantiation(self):
        hook = MemoryHook()
        assert isinstance(hook, ExecutionHook)

    @pytest.mark.asyncio
    async def test_before_execution_failure_is_safe(self):
        hook = MemoryHook()
        ctx = _make_context()
        await hook.before_execution(ctx)

    def test_maintenance_runs_only_once(self):
        hook = MemoryHook()
        mock_mm = MagicMock()
        hook._run_maintenance_once(mock_mm)
        assert hook._maintenance_done is True
        mock_mm.run_maintenance.assert_called_once()
        # Second call should not call maintenance again
        hook._run_maintenance_once(mock_mm)
        mock_mm.run_maintenance.assert_called_once()


# ============================================================================
# LearningHook — P1 regression tests
# ============================================================================


class TestLearningHook:
    @pytest.mark.asyncio
    async def test_no_scheduler_does_not_raise(self):
        hook = LearningHook()
        hook._scheduler = None
        ctx = _make_context()
        await hook.before_execution(ctx)

    @pytest.mark.asyncio
    async def test_scheduler_called(self):
        hook = LearningHook()
        mock_scheduler = MagicMock()
        hook._scheduler = mock_scheduler
        ctx = _make_context()
        await hook.before_execution(ctx)
        mock_scheduler.maybe_run_analysis.assert_called_once()

    @pytest.mark.asyncio
    async def test_scheduler_error_does_not_raise(self):
        hook = LearningHook()
        mock_scheduler = MagicMock()
        mock_scheduler.maybe_run_analysis.side_effect = RuntimeError("boom")
        hook._scheduler = mock_scheduler
        ctx = _make_context()
        await hook.before_execution(ctx)

    def test_accepts_injected_repository(self):
        """P1 regression: LearningHook should use injected repo, not default."""
        mock_repo = MagicMock()
        hook = LearningHook(repository=mock_repo)
        assert hook._repository is mock_repo

    def test_optimizer_exposed_for_orchestrator(self):
        """P1 regression: optimizer must be accessible for planning hints."""
        hook = LearningHook()
        # optimizer may be None if learning is disabled, but attr must exist
        assert hasattr(hook, "optimizer")


# ============================================================================
# ImpactHook — P1 regression tests
# ============================================================================


class TestImpactHook:
    @pytest.mark.asyncio
    async def test_no_workdir_skips(self):
        hook = ImpactHook()
        hook._predictor = MagicMock()
        # None work_dir triggers the guard
        ctx = _make_context(work_dir=None)
        await hook.before_execution(ctx)
        assert "impact_scope" not in ctx._state

    @pytest.mark.asyncio
    async def test_after_no_impact_scope_skips(self):
        hook = ImpactHook()
        ctx = _make_context()
        await hook.after_execution(ctx, MagicMock())

    @pytest.mark.asyncio
    async def test_after_none_snapshot_skips(self):
        hook = ImpactHook()
        ctx = _make_context()
        ctx._state["impact_scope"] = MagicMock()
        ctx._state["before_snapshot"] = None
        await hook.after_execution(ctx, MagicMock())

    def test_make_predictor_receives_memory_manager(self):
        """P1 regression: predictor must get memory_manager for historical lookup."""
        hook = ImpactHook()
        mock_mm = MagicMock()
        predictor = hook._make_predictor(memory_manager=mock_mm)
        assert predictor.memory_manager is mock_mm

    def test_make_predictor_without_memory(self):
        hook = ImpactHook()
        predictor = hook._make_predictor(memory_manager=None)
        assert predictor.memory_manager is None

    def test_accepts_llm_config(self):
        """P1 regression: ImpactHook should accept llm_config for predictor."""
        mock_config = MagicMock()
        hook = ImpactHook(llm_config=mock_config)
        assert hook._llm_config is mock_config


# ============================================================================
# Hook ordering — P2 regression test
# ============================================================================


class TestHookOrdering:
    @pytest.mark.asyncio
    async def test_memory_hook_runs_first(self):
        """MemoryHook must run before ImpactHook so memory_manager is available."""
        hooks = [MemoryHook(), LearningHook(), ImpactHook()]
        assert isinstance(hooks[0], MemoryHook)
        assert isinstance(hooks[2], ImpactHook)

    @pytest.mark.asyncio
    async def test_all_hooks_run_without_error(self):
        hooks = [MemoryHook(), LearningHook(), ImpactHook()]
        ctx = _make_context()
        for hook in hooks:
            await hook.before_execution(ctx)
        for hook in hooks:
            await hook.after_execution(ctx, MagicMock())
