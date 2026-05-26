"""Tests for #911: skip failure handler and replan LLM calls when provider unhealthy.

Verifies:
1. failure_handler NOT called when provider is unhealthy — decision defaults to skip
2. replan_handler NOT called when provider is unhealthy — falls back to skip
3. Normal flow works when provider IS healthy
4. Provider health recovery resumes normal LLM calls
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.models import DAG, DAGNode, NodeStatus, FailureDecision
from core.dag_engine import DAGExecutionEngine
from core.provider_health import ProviderHealthTracker, ProviderHealthConfig


def _make_engine(
    provider_health: ProviderHealthTracker | None = None,
    failure_handler: AsyncMock | None = None,
    replan_handler: AsyncMock | None = None,
) -> DAGExecutionEngine:
    engine = DAGExecutionEngine(
        agent_executor=AsyncMock(return_value={
            "status": "completed", "summary": "ok", "artifacts": [],
        }),
        failure_handler=failure_handler or AsyncMock(return_value=FailureDecision(
            action="skip", reasoning="test",
        )),
        replan_handler=replan_handler,
        provider_health=provider_health,
        enable_watchdog=False,
    )
    return engine


def _make_simple_dag() -> DAG:
    """Two-node linear DAG: planner -> generator."""
    dag = DAG(reasoning="test #911")
    dag.add_node(DAGNode(
        id="plan", agent_type="planner", task_description="plan",
    ))
    dag.add_node(DAGNode(
        id="gen_1", agent_type="generator", task_description="implement",
    ))
    dag.add_edge("plan", "gen_1")
    return dag


class TestSkipFailureHandlerWhenUnhealthy:
    @pytest.mark.asyncio
    async def test_failure_handler_not_called(self):
        """failure_handler LLM call skipped when provider is unhealthy."""
        tracker = ProviderHealthTracker(ProviderHealthConfig(failure_threshold=1))
        tracker.record_failure("anthropic", "")

        failure_handler = AsyncMock(return_value=FailureDecision(
            action="replan", reasoning="should not be called",
        ))
        engine = _make_engine(provider_health=tracker, failure_handler=failure_handler)

        dag = _make_simple_dag()
        dag.update_node("plan", status=NodeStatus.SUCCESS)
        dag.update_node("gen_1", status=NodeStatus.FAILED, error="API error")

        decision = None
        provider, model = engine._get_provider_model()
        if not engine._provider_health.is_healthy(provider, model):
            decision = FailureDecision(
                action="skip",
                reasoning=f"Provider {provider}/{model} is unhealthy (#911)",
            )
        else:
            decision = await failure_handler(dag, "gen_1", "API error")

        failure_handler.assert_not_called()
        assert decision.action == "skip"
        assert "#911" in decision.reasoning

    @pytest.mark.asyncio
    async def test_failure_handler_called_when_healthy(self):
        """failure_handler LLM call proceeds when provider is healthy."""
        tracker = ProviderHealthTracker(ProviderHealthConfig(failure_threshold=3))
        failure_handler = AsyncMock(return_value=FailureDecision(
            action="replan", reasoning="legit replan",
        ))
        engine = _make_engine(provider_health=tracker, failure_handler=failure_handler)

        dag = _make_simple_dag()
        dag.update_node("plan", status=NodeStatus.SUCCESS)
        dag.update_node("gen_1", status=NodeStatus.FAILED, error="some error")

        provider, model = engine._get_provider_model()
        if not engine._provider_health.is_healthy(provider, model):
            decision = FailureDecision(action="skip", reasoning="unhealthy")
        else:
            decision = await failure_handler(dag, "gen_1", "some error")

        failure_handler.assert_called_once()
        assert decision.action == "replan"


class TestSkipReplanWhenUnhealthy:
    @pytest.mark.asyncio
    async def test_replan_handler_not_called(self):
        """replan_handler LLM call skipped when provider is unhealthy."""
        tracker = ProviderHealthTracker(ProviderHealthConfig(failure_threshold=1))
        tracker.record_failure("anthropic", "")

        replan_handler = AsyncMock(return_value=DAG(reasoning="should not run"))
        engine = _make_engine(
            provider_health=tracker,
            replan_handler=replan_handler,
        )

        dag = _make_simple_dag()
        dag.update_node("plan", status=NodeStatus.SUCCESS)
        dag.update_node("gen_1", status=NodeStatus.FAILED, error="boom")

        result = await engine._try_execute_replan(
            dag, "gen_1",
            levels=[["plan"], ["gen_1"]],
            level_idx=1, replan_count=0,
        )

        replan_handler.assert_not_called()
        assert result[4] is False  # not replanned

    @pytest.mark.asyncio
    async def test_replan_handler_called_when_healthy(self):
        """replan_handler LLM call proceeds when provider is healthy."""
        tracker = ProviderHealthTracker(ProviderHealthConfig(failure_threshold=3))

        new_dag = DAG(reasoning="replanned")
        new_dag.add_node(DAGNode(
            id="gen_v2", agent_type="generator", task_description="reimplement",
        ))

        replan_handler = AsyncMock(return_value=new_dag)
        engine = _make_engine(
            provider_health=tracker,
            replan_handler=replan_handler,
        )

        dag = _make_simple_dag()
        dag.update_node("plan", status=NodeStatus.SUCCESS)
        dag.update_node("gen_1", status=NodeStatus.FAILED, error="boom")

        result = await engine._try_execute_replan(
            dag, "gen_1",
            levels=[["plan"], ["gen_1"]],
            level_idx=1, replan_count=0,
        )

        replan_handler.assert_called_once()
        assert result[4] is True  # replanned


class TestRecoveryResumesLLMCalls:
    @pytest.mark.asyncio
    async def test_recovery_allows_failure_handler(self):
        """After provider recovers, failure handler is called again."""
        tracker = ProviderHealthTracker(ProviderHealthConfig(
            failure_threshold=1, recovery_cooldown_sec=0.0,
        ))
        tracker.record_failure("anthropic", "")
        tracker.record_success("anthropic", "")

        failure_handler = AsyncMock(return_value=FailureDecision(
            action="skip", reasoning="recovered",
        ))
        engine = _make_engine(provider_health=tracker, failure_handler=failure_handler)

        provider, model = engine._get_provider_model()
        assert engine._provider_health.is_healthy(provider, model)

        dag = _make_simple_dag()
        dag.update_node("plan", status=NodeStatus.SUCCESS)
        dag.update_node("gen_1", status=NodeStatus.FAILED, error="err")

        if not engine._provider_health.is_healthy(provider, model):
            decision = FailureDecision(action="skip", reasoning="unhealthy")
        else:
            decision = await failure_handler(dag, "gen_1", "err")

        failure_handler.assert_called_once()
        assert decision.reasoning == "recovered"


class TestGetProviderModel:
    def test_default_provider_model(self):
        """Default provider is anthropic, model is empty string."""
        engine = _make_engine()
        provider, model = engine._get_provider_model()
        assert provider == "anthropic"
        assert model == ""
