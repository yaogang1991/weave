"""Tests for orchestrator/intelligent_orchestrator.py — facade delegation layer."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from core.models import DAG, DAGNode, FailureDecision, OrchestratorPlan, SuccessCriterion
from core.config import LLMConfig
from core.agent_registry import AgentRegistry
from session.store import SessionStore


@pytest.fixture
def llm_config():
    return LLMConfig(api_key="test-key", model="test-model")


@pytest.fixture
def tmp_store(tmp_path):
    return SessionStore(str(tmp_path / "events"))


@pytest.fixture
def registry():
    return AgentRegistry()


@pytest.fixture
def orchestrator(llm_config, tmp_store, registry):
    from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
    return IntelligentOrchestrator(
        llm_config=llm_config,
        session_store=tmp_store,
        agent_registry=registry,
    )


class TestConstructor:
    def test_creates_planner_and_adapter(self, orchestrator):
        assert orchestrator._planner is not None
        assert orchestrator._adapter is not None

    def test_default_llm_client(self, llm_config, tmp_store, registry):
        from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        assert orch.llm is not None

    def test_llm_router_overrides_client(self, llm_config, tmp_store, registry):
        from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
        mock_router = MagicMock()
        mock_client = MagicMock()
        mock_router.get_client.return_value = mock_client
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
            llm_router=mock_router,
        )
        mock_router.get_client.assert_called_once_with("orchestrator")
        assert orch.llm is mock_client

    def test_optional_dependencies_stored(self, llm_config, tmp_store, registry):
        from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
        mock_optimizer = MagicMock()
        mock_skills = MagicMock()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
            learning_optimizer=mock_optimizer,
            skill_registry=mock_skills,
        )
        assert orch.learning_optimizer is mock_optimizer
        assert orch.skill_registry is mock_skills


class TestPlanDelegation:
    @pytest.mark.asyncio
    async def test_plan_delegates_to_planner(self, orchestrator):
        mock_dag = DAG(reasoning="test")
        orchestrator._planner.plan = AsyncMock(return_value=mock_dag)

        result = await orchestrator.plan("build a REST API")

        orchestrator._planner.plan.assert_awaited_once_with("build a REST API", None)
        assert result is mock_dag

    @pytest.mark.asyncio
    async def test_plan_passes_project_context(self, orchestrator):
        mock_dag = DAG(reasoning="test")
        orchestrator._planner.plan = AsyncMock(return_value=mock_dag)
        ctx = {"project_path": "/tmp/proj"}

        await orchestrator.plan("build API", project_context=ctx)

        orchestrator._planner.plan.assert_awaited_once_with("build API", ctx)


class TestAdaptToFailureDelegation:
    @pytest.mark.asyncio
    async def test_adapt_delegates_to_adapter(self, orchestrator):
        dag = DAG(reasoning="test")
        decision = FailureDecision(action="retry", reasoning="test")
        orchestrator._adapter.adapt_to_failure = AsyncMock(return_value=decision)

        result = await orchestrator.adapt_to_failure(dag, "node_1", "error msg")

        orchestrator._adapter.adapt_to_failure.assert_awaited_once_with(
            dag, "node_1", "error msg",
        )
        assert result.action == "retry"


class TestReplanDelegation:
    @pytest.mark.asyncio
    async def test_replan_delegates_to_adapter(self, orchestrator):
        dag = DAG(reasoning="test")
        new_dag = DAG(reasoning="replanned")
        orchestrator._adapter.replan = AsyncMock(return_value=new_dag)

        result = await orchestrator.replan(dag, "node_1", "original requirement")

        orchestrator._adapter.replan.assert_awaited_once_with(
            dag, "node_1", "original requirement",
        )
        assert result is new_dag


class TestPlanToDag:
    def test_delegates_to_planner(self, orchestrator):
        plan = OrchestratorPlan(
            nodes=[{"id": "a", "agent_type": "generator", "task_description": "t"}],
            reasoning="test",
        )
        result = orchestrator._plan_to_dag(plan)
        assert "a" in result.nodes


class TestInferFallbackEdges:
    def test_infers_edges_from_agent_types(self):
        from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
        dag = DAG(reasoning="test")
        dag = dag.add_node(DAGNode(id="p1", agent_type="planner", task_description="plan"))
        dag = dag.add_node(DAGNode(id="g1", agent_type="generator", task_description="gen"))

        result = IntelligentOrchestrator._infer_fallback_edges(dag)
        assert len(result.edges) > 0


class TestApplyRenameMap:
    def test_updates_task_description(self):
        from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
        dag = DAG(reasoning="test")
        dag = dag.add_node(DAGNode(
            id="n1",
            agent_type="generator",
            task_description="Create old_module.py with tests",
        ))

        IntelligentOrchestrator._apply_rename_map(dag, {"old_module": "new_module"})

        assert "new_module.py" in dag.nodes["n1"].task_description
        assert "old_module.py" not in dag.nodes["n1"].task_description


class TestCountFeatures:
    def test_delegates_to_plan_validator(self):
        from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
        count = IntelligentOrchestrator._count_features(
            "implement user auth and login and logout"
        )
        assert isinstance(count, int)
        assert count >= 0


class TestIsResponseTruncated:
    def test_detects_truncation(self):
        from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
        assert IntelligentOrchestrator._is_response_truncated(
            '{"nodes": [{"id": "a'
        )

    def test_non_truncated(self):
        from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
        assert not IntelligentOrchestrator._is_response_truncated(
            '{"nodes": [], "reasoning": "done"}'
        )


class TestPlanStructuredOutput:
    def test_delegates_to_planner(self, orchestrator):
        orchestrator._planner._plan_structured_output = MagicMock(return_value={"nodes": []})
        result = orchestrator._plan_structured_output([{"role": "user", "content": "test"}])
        assert result == {"nodes": []}


class TestPlanFreeText:
    def test_delegates_to_planner(self, orchestrator):
        orchestrator._planner._plan_free_text = MagicMock(return_value={"nodes": [], "reasoning": "r"})
        result = orchestrator._plan_free_text([{"role": "user", "content": "test"}])
        assert "nodes" in result


class TestEstimateTokens:
    def test_returns_int(self, orchestrator):
        result = orchestrator._estimate_tokens("hello world")
        assert isinstance(result, int)
        assert result > 0

    def test_empty_string(self, orchestrator):
        result = orchestrator._estimate_tokens("")
        assert result == 0


class TestGetContextWindow:
    def test_returns_positive_int(self, orchestrator):
        result = orchestrator._get_context_window()
        assert result > 0


class TestExtractJson:
    def test_extracts_valid_json(self, orchestrator):
        text = '```json\n{"nodes": [], "reasoning": "test"}\n```'
        result = orchestrator._extract_json(text)
        assert result is not None
        assert result["nodes"] == []

    def test_returns_none_for_no_json(self, orchestrator):
        result = orchestrator._extract_json("no json here")
        assert result is None


class TestRepairTruncatedJson:
    def test_repairs_truncated_json(self):
        from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
        # repair_truncated_json returns a string, not dict
        result = IntelligentOrchestrator._repair_truncated_json('{"a": 1, "b": 2', 0)
        assert isinstance(result, str)
        assert '"a"' in result


class TestEstimateMessagesBytes:
    def test_returns_positive_int(self, orchestrator):
        msgs = [{"role": "user", "content": "hello" * 100}]
        result = orchestrator._estimate_messages_bytes(msgs)
        assert result > 0


class TestPruneMessages:
    def test_prune_for_size(self, orchestrator):
        msgs = [{"role": "user", "content": "x" * 100}]
        result = orchestrator._prune_messages_for_size(msgs)
        assert isinstance(result, list)

    def test_prune_static(self):
        from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
        msgs = [{"role": "user", "content": "x" * 100}]
        result = IntelligentOrchestrator._prune_messages_for_size_static(msgs)
        assert isinstance(result, list)


class TestCheckPostEstimationBudget:
    def test_logs_warning_for_over_budget(self, orchestrator, caplog):
        import logging
        dag = DAG(reasoning="test")
        node = DAGNode(id="n1", agent_type="generator", task_description="t")
        node.estimated_tokens = 20000
        node.token_budget = 10000
        dag = dag.add_node(node)

        with caplog.at_level(logging.WARNING):
            orchestrator._check_post_estimation_budget(dag)

        assert any("exceeds budget" in r.message for r in caplog.records)

    def test_no_warning_when_within_budget(self, orchestrator, caplog):
        import logging
        dag = DAG(reasoning="test")
        node = DAGNode(id="n1", agent_type="generator", task_description="t")
        node.estimated_tokens = 5000
        node.token_budget = 10000
        dag = dag.add_node(node)

        with caplog.at_level(logging.WARNING):
            orchestrator._check_post_estimation_budget(dag)

        assert not any("exceeds budget" in r.message for r in caplog.records)
