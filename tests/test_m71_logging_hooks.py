"""Tests for M7.1 Phase 2: MEDIUM severity silent failure logging.

Covers:
1. control_plane/hooks.py — LearningHook.before_execution analysis failure
2. control_plane/hooks.py — ImpactHook.before_execution failure
3. control_plane/hooks.py — ImpactHook.after_execution failure
4. control_plane/hooks.py — ImpactHook._persist_record failure
5. orchestrator/planner.py — learning hints unavailable
6. orchestrator/planner.py — skill descriptions unavailable
7. memory/manager.py — access count sync failure
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.job = MagicMock()
    ctx.job.id = "j1"
    ctx.job.requirement = "test"
    ctx.job.project_path = "."
    ctx.run_id = "r1"
    ctx.work_dir = None
    ctx.metadata = {}
    ctx._state = {}
    return ctx


# ---------------------------------------------------------------------------
# Test 1: hooks.py — LearningHook.before_execution analysis failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_learning_hook_analysis_failure_logs_warning(caplog):
    from control_plane.hooks import LearningHook

    hook = LearningHook.__new__(LearningHook)
    hook._scheduler = MagicMock()
    hook._scheduler.maybe_run_analysis.side_effect = RuntimeError("analysis crashed")

    with caplog.at_level(logging.WARNING, logger="control_plane.hooks"):
        await hook.before_execution(_make_ctx())

    assert any(
        "LearningHook analysis failed" in rec.message
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Test 2: hooks.py — ImpactHook.before_execution failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_impact_hook_before_execution_failure_logs_warning(caplog):
    from control_plane.hooks import ImpactHook

    hook = ImpactHook.__new__(ImpactHook)
    hook._llm_config = None
    hook._coverage_threshold = 0.5

    ctx = _make_ctx()
    ctx.work_dir = MagicMock()

    with caplog.at_level(logging.WARNING, logger="control_plane.hooks"), \
         patch("analysis.impact_predictor.ImpactPredictor", side_effect=RuntimeError("predictor fail")):
        await hook.before_execution(ctx)

    assert any(
        "ImpactHook before_execution failed" in rec.message
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Test 3: hooks.py — ImpactHook.after_execution failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_impact_hook_after_execution_failure_logs_warning(caplog):
    from control_plane.hooks import ImpactHook

    hook = ImpactHook.__new__(ImpactHook)
    hook._coverage_threshold = 0.5

    ctx = _make_ctx()
    ctx._state["impact_scope"] = MagicMock()
    ctx._state["before_snapshot"] = MagicMock()
    ctx._state["impact_project_path"] = "."
    ctx.work_dir = MagicMock()

    with caplog.at_level(logging.WARNING, logger="control_plane.hooks"), \
         patch("analysis.change_verifier.ChangeVerifier", side_effect=RuntimeError("verifier fail")):
        await hook.after_execution(ctx, MagicMock())

    assert any(
        "ImpactHook after_execution failed" in rec.message
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Test 4: hooks.py — ImpactHook._persist_record failure
# ---------------------------------------------------------------------------

def test_impact_persist_record_failure_logs_warning(caplog):
    from control_plane.hooks import ImpactHook

    hook = ImpactHook.__new__(ImpactHook)

    ctx = _make_ctx()
    ctx.run_id = "r1"

    with caplog.at_level(logging.WARNING, logger="control_plane.hooks"), \
         patch("core.config.WeaveConfig.from_env", side_effect=RuntimeError("config fail")):
        hook._persist_record(ctx, MagicMock(predicted_files=[]), MagicMock())

    assert any(
        "Impact record persistence failed" in rec.message
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Test 5: planner.py — learning hints unavailable
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_planner_learning_hints_failure_logs_warning(caplog):
    from orchestrator.planner import Planner

    planner = Planner.__new__(Planner)
    planner.learning_optimizer = MagicMock()
    planner.learning_optimizer.get_planning_hints.side_effect = RuntimeError(
        "optimizer broken"
    )
    planner.skill_registry = None
    planner.llm_client = MagicMock()
    planner.llm_config = MagicMock()
    planner.llm_config.model = "test-model"
    planner.agent_registry = MagicMock()
    planner.agent_registry.to_prompt_description.return_value = "agents"
    planner._prompt_registry = MagicMock()

    with caplog.at_level(logging.WARNING, logger="orchestrator.planner"):
        with patch("orchestrator.planner.truncate_requirement_if_needed", return_value="build a feature"):
            try:
                await planner.plan("build a feature")
            except AttributeError:
                pass  # expected — other attrs not mocked

    assert any(
        "Learning hints unavailable" in rec.message
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Test 6: planner.py — skill descriptions unavailable
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_planner_skill_descriptions_failure_logs_warning(caplog):
    from orchestrator.planner import Planner

    planner = Planner.__new__(Planner)
    planner.learning_optimizer = None
    planner.skill_registry = MagicMock()
    planner.skill_registry.to_prompt_description.side_effect = RuntimeError(
        "skill registry broken"
    )
    planner.llm_client = MagicMock()
    planner.llm_config = MagicMock()
    planner.llm_config.model = "test-model"
    planner.agent_registry = MagicMock()
    planner.agent_registry.to_prompt_description.return_value = "agents"
    planner._prompt_registry = MagicMock()

    with caplog.at_level(logging.WARNING, logger="orchestrator.planner"):
        with patch("orchestrator.planner.truncate_requirement_if_needed", return_value="build a feature"):
            try:
                await planner.plan("build a feature")
            except AttributeError:
                pass

    assert any(
        "Skill descriptions unavailable" in rec.message
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Test 7: memory/manager.py — access count sync failure
# ---------------------------------------------------------------------------

def test_memory_access_count_sync_failure_logs_warning(caplog, tmp_path):
    from memory.manager import MemoryManager

    mgr = MemoryManager.__new__(MemoryManager)

    fake_entry = MagicMock()
    fake_entry.id = "test1"
    fake_entry.access_count = 42
    fake_entry.model_dump.return_value = {"id": "test1", "access_count": 42}

    store = MagicMock()
    store.list_entries.return_value = [fake_entry]
    store._find_entry_path.return_value = tmp_path / "test1.json"
    (tmp_path / "test1.json").write_text('{"access_count": 0}')
    mgr.store = store

    with caplog.at_level(logging.WARNING, logger="memory.manager"), \
         patch("memory.store._json_dump_atomic", side_effect=OSError("disk full")):
        mgr._flush_access_updates()

    assert any(
        "Memory access count sync failed" in rec.message
        for rec in caplog.records
    )
