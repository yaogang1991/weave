"""Tests for #913: replan handler uses getattr for requirement attribute.

Verifies:
1. replan handler lambda doesn't crash when args lacks 'requirement'
2. replan handler falls back to DAG reasoning when no requirement
3. replan handler uses args.requirement when available
"""
from argparse import Namespace

import pytest

from core.models import DAG, DAGNode, FailureDecision
from core.dag_engine import DAGExecutionEngine, DAGEngineConfig
from unittest.mock import AsyncMock


def _make_engine(replan_handler):
    return DAGExecutionEngine(
        agent_executor=AsyncMock(return_value={
            "status": "completed", "summary": "ok", "artifacts": [],
        }),
        failure_handler=AsyncMock(return_value=FailureDecision(
            action="skip", reasoning="test",
        )),
        replan_handler=replan_handler,
        config=DAGEngineConfig(enable_watchdog=False),
    )


def _make_dag(reasoning="test reasoning"):
    dag = DAG(reasoning=reasoning)
    dag.add_node(DAGNode(
        id="plan", agent_type="planner", task_description="plan",
    ))
    dag.add_node(DAGNode(
        id="gen_1", agent_type="generator", task_description="impl",
    ))
    dag.add_edge("plan", "gen_1")
    return dag


class TestReplanHandlerRequirement:
    def test_namespace_without_requirement_uses_dag_reasoning(self):
        args = Namespace(plan_file="plan.json", project=".", max_parallel=1)
        dag = _make_dag(reasoning="build a REST API")
        requirement = getattr(args, "requirement", "") or dag.reasoning or ""
        assert requirement == "build a REST API"

    def test_namespace_with_requirement_uses_args(self):
        args = Namespace(requirement="build auth system", project=".")
        dag = _make_dag(reasoning="different")
        requirement = getattr(args, "requirement", "") or dag.reasoning or ""
        assert requirement == "build auth system"

    def test_empty_requirement_uses_dag_reasoning(self):
        args = Namespace(requirement="", project=".")
        dag = _make_dag(reasoning="from dag")
        requirement = getattr(args, "requirement", "") or dag.reasoning or ""
        assert requirement == "from dag"

    @pytest.mark.asyncio
    async def test_replan_does_not_crash_on_namespace_args(self):
        args = Namespace(plan_file="plan.json", project=".", max_parallel=1)
        dag = _make_dag()

        captured_requirement = None

        async def mock_replan(dag_ref, failed_id, requirement=""):
            nonlocal captured_requirement
            captured_requirement = requirement
            return DAG(reasoning="replanned")

        replan_lambda = lambda dag_ref, failed_id: mock_replan(
            dag_ref, failed_id,
            getattr(args, "requirement", "") or dag_ref.reasoning or "",
        )

        engine = _make_engine(replan_handler=replan_lambda)
        result = await engine._try_execute_replan(
            dag, "gen_1",
            levels=[["plan"], ["gen_1"]],
            level_idx=1, replan_count=0,
        )

        assert result[4] is True
        assert captured_requirement == "test reasoning"

    @pytest.mark.asyncio
    async def test_replan_uses_explicit_requirement(self):
        args = Namespace(requirement="explicit requirement", project=".")
        dag = _make_dag(reasoning="fallback")

        captured_requirement = None

        async def mock_replan(dag_ref, failed_id, requirement=""):
            nonlocal captured_requirement
            captured_requirement = requirement
            return DAG(reasoning="replanned")

        replan_lambda = lambda dag_ref, failed_id: mock_replan(
            dag_ref, failed_id,
            getattr(args, "requirement", "") or dag_ref.reasoning or "",
        )

        engine = _make_engine(replan_handler=replan_lambda)
        result = await engine._try_execute_replan(
            dag, "gen_1",
            levels=[["plan"], ["gen_1"]],
            level_idx=1, replan_count=0,
        )

        assert result[4] is True
        assert captured_requirement == "explicit requirement"
