"""
Tests for orchestrator/intelligent_orchestrator.py -- thin facade tests.

Validates that IntelligentOrchestrator:
- Wires _Planner and _Adapter correctly in __init__
- Delegates public async methods to submodules
- Delegates static/backward-compat methods to the right targets
- Handles constructor parameter variations
"""
from __future__ import annotations

import json
import logging
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.models import (
    DAG,
    DAGEdge,
    DAGNode,
    DependencyType,
    FailureDecision,
    NodeStatus,
    OrchestratorPlan,
    SuccessCriterion,
)
from core.config import LLMConfig
from core.agent_registry import AgentRegistry
from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
from orchestrator.llm_utils import is_response_truncated


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dag(nodes=None, edges=None) -> DAG:
    """Build a minimal DAG for testing."""
    dag = DAG()
    if nodes:
        for n in nodes:
            dag.add_node(n)
    if edges:
        for from_id, to_id in edges:
            dag.add_edge(from_id, to_id)
    return dag


def _make_node(node_id: str, agent_type: str = "generator",
               task_description: str = "do something") -> DAGNode:
    return DAGNode(id=node_id, agent_type=agent_type,
                   task_description=task_description)


# ---------------------------------------------------------------------------
# Constructor tests
# ---------------------------------------------------------------------------

class TestConstructor:
    """Verify __init__ wiring of _Planner, _Adapter, and LLM client."""

    def test_creates_planner_and_adapter(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        assert orch._planner is not None
        assert orch._adapter is not None

    def test_stores_dependencies(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        assert orch.llm_config is llm_config
        assert orch.session_store is tmp_store
        assert orch.agent_registry is registry

    def test_default_llm_client_created_when_no_router(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        assert orch.llm is not None

    def test_llm_router_overrides_client(self, tmp_store, llm_config):
        registry = AgentRegistry()
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

    def test_optional_dependencies_default_to_none(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        assert orch.learning_optimizer is None
        assert orch.skill_registry is None

    def test_optional_dependencies_are_stored(self, tmp_store, llm_config):
        registry = AgentRegistry()
        mock_optimizer = MagicMock()
        mock_skill_reg = MagicMock()

        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
            learning_optimizer=mock_optimizer,
            skill_registry=mock_skill_reg,
        )
        assert orch.learning_optimizer is mock_optimizer
        assert orch.skill_registry is mock_skill_reg

    def test_prompt_registry_defaults_to_global(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        assert orch._prompt_registry is not None

    def test_custom_prompt_registry_is_used(self, tmp_store, llm_config):
        registry = AgentRegistry()
        custom_pr = MagicMock()

        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
            prompt_registry=custom_pr,
        )
        assert orch._prompt_registry is custom_pr

    def test_planner_receives_correct_args(self, tmp_store, llm_config):
        registry = AgentRegistry()
        with patch(
            "orchestrator.intelligent_orchestrator._Planner"
        ) as MockPlanner:
            mock_planner_instance = MagicMock()
            MockPlanner.return_value = mock_planner_instance

            orch = IntelligentOrchestrator(
                llm_config=llm_config,
                session_store=tmp_store,
                agent_registry=registry,
            )
            MockPlanner.assert_called_once()
            call_kwargs = MockPlanner.call_args[1]
            assert call_kwargs["llm_config"] is llm_config
            assert call_kwargs["agent_registry"] is registry

    def test_adapter_receives_plan_to_dag_fn(self, tmp_store, llm_config):
        registry = AgentRegistry()
        with patch(
            "orchestrator.intelligent_orchestrator._Adapter"
        ) as MockAdapter:
            mock_adapter_instance = MagicMock()
            MockAdapter.return_value = mock_adapter_instance

            orch = IntelligentOrchestrator(
                llm_config=llm_config,
                session_store=tmp_store,
                agent_registry=registry,
            )
            MockAdapter.assert_called_once()
            call_kwargs = MockAdapter.call_args[1]
            assert callable(call_kwargs["plan_to_dag_fn"])


# ---------------------------------------------------------------------------
# Delegation: plan()
# ---------------------------------------------------------------------------

class TestPlanDelegation:
    """Verify plan() delegates to _planner.plan."""

    @pytest.mark.asyncio
    async def test_plan_delegates(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        expected_dag = _make_dag()
        orch._planner.plan = AsyncMock(return_value=expected_dag)

        result = await orch.plan("build a REST API")
        assert result is expected_dag
        orch._planner.plan.assert_awaited_once_with("build a REST API", None)

    @pytest.mark.asyncio
    async def test_plan_with_project_context(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        expected_dag = _make_dag()
        orch._planner.plan = AsyncMock(return_value=expected_dag)
        ctx = {"files": ["main.py"]}

        result = await orch.plan("refactor", project_context=ctx)
        assert result is expected_dag
        orch._planner.plan.assert_awaited_once_with("refactor", ctx)


# ---------------------------------------------------------------------------
# Delegation: adapt_to_failure()
# ---------------------------------------------------------------------------

class TestAdaptToFailureDelegation:

    @pytest.mark.asyncio
    async def test_adapt_delegates(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        dag = _make_dag()
        decision = FailureDecision(action="retry", reasoning="transient")
        orch._adapter.adapt_to_failure = AsyncMock(return_value=decision)

        result = await orch.adapt_to_failure(dag, "node_1", "timeout")
        assert result is decision
        orch._adapter.adapt_to_failure.assert_awaited_once_with(
            dag, "node_1", "timeout"
        )

    @pytest.mark.asyncio
    async def test_adapt_default_error(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        decision = FailureDecision(action="abort", reasoning="bad")
        orch._adapter.adapt_to_failure = AsyncMock(return_value=decision)
        dag = _make_dag()

        await orch.adapt_to_failure(dag, "node_1")
        orch._adapter.adapt_to_failure.assert_awaited_once_with(
            dag, "node_1", ""
        )


# ---------------------------------------------------------------------------
# Delegation: replan()
# ---------------------------------------------------------------------------

class TestReplanDelegation:

    @pytest.mark.asyncio
    async def test_replan_delegates(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        old_dag = _make_dag()
        new_dag = _make_dag(nodes=[_make_node("new_node")])
        orch._adapter.replan = AsyncMock(return_value=new_dag)

        result = await orch.replan(old_dag, "node_1", "original requirement")
        assert result is new_dag
        orch._adapter.replan.assert_awaited_once_with(
            old_dag, "node_1", "original requirement"
        )

    @pytest.mark.asyncio
    async def test_replan_default_requirement(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        new_dag = _make_dag()
        orch._adapter.replan = AsyncMock(return_value=new_dag)
        old_dag = _make_dag()

        await orch.replan(old_dag, "node_1")
        orch._adapter.replan.assert_awaited_once_with(
            old_dag, "node_1", ""
        )


# ---------------------------------------------------------------------------
# Delegation: plan_from_template()
# ---------------------------------------------------------------------------

class TestPlanFromTemplateDelegation:

    @pytest.mark.asyncio
    async def test_plan_from_template_delegates(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        expected = _make_dag()
        orch._planner.plan_from_template = AsyncMock(return_value=expected)

        result = await orch.plan_from_template("build_api", {"feature": "Todo"})
        assert result is expected
        orch._planner.plan_from_template.assert_awaited_once_with(
            "build_api", {"feature": "Todo"}
        )

    @pytest.mark.asyncio
    async def test_plan_from_template_no_vars(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        expected = _make_dag()
        orch._planner.plan_from_template = AsyncMock(return_value=expected)

        result = await orch.plan_from_template("build_api")
        assert result is expected
        orch._planner.plan_from_template.assert_awaited_once_with(
            "build_api", None
        )


# ---------------------------------------------------------------------------
# Delegation: backward-compat internal methods
# ---------------------------------------------------------------------------

class TestBackwardCompatDelegation:

    def test_plan_to_dag_delegates(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        plan = OrchestratorPlan(
            reasoning="test",
            nodes=[{"id": "n1", "agent_type": "generator", "task_description": "t"}],
            edges=[],
        )
        expected = _make_dag()
        orch._planner._plan_to_dag = MagicMock(return_value=expected)

        result = orch._plan_to_dag(plan)
        assert result is expected
        orch._planner._plan_to_dag.assert_called_once_with(plan)

    def test_plan_structured_output_delegates(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        messages = [{"role": "user", "content": "plan"}]
        expected = {"nodes": [], "edges": []}
        orch._planner._plan_structured_output = MagicMock(return_value=expected)

        result = orch._plan_structured_output(messages)
        assert result is expected
        orch._planner._plan_structured_output.assert_called_once_with(messages)

    def test_plan_free_text_delegates(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        messages = [{"role": "user", "content": "plan"}]
        expected = {"nodes": [], "edges": []}
        orch._planner._plan_free_text = MagicMock(return_value=expected)

        result = orch._plan_free_text(messages)
        assert result is expected
        orch._planner._plan_free_text.assert_called_once_with(messages)


# ---------------------------------------------------------------------------
# Static methods
# ---------------------------------------------------------------------------

class TestStaticMethods:
    """Test static delegation methods that do not need a constructed orchestrator."""

    def test_infer_fallback_edges_with_planner_generator_evaluator(self):
        dag = _make_dag(
            nodes=[
                _make_node("plan_1", "planner"),
                _make_node("gen_1", "generator"),
                _make_node("eval_1", "evaluator"),
            ],
        )
        result = IntelligentOrchestrator._infer_fallback_edges(dag)
        edge_pairs = {(e.from_node, e.to_node) for e in result.edges}
        # planner -> generator, planner -> evaluator
        assert ("plan_1", "gen_1") in edge_pairs
        assert ("plan_1", "eval_1") in edge_pairs
        # generator -> evaluator
        assert ("gen_1", "eval_1") in edge_pairs

    def test_infer_fallback_edges_empty_dag(self):
        dag = DAG()
        result = IntelligentOrchestrator._infer_fallback_edges(dag)
        assert result.edges == []

    def test_infer_fallback_edges_no_planner(self):
        dag = _make_dag(
            nodes=[
                _make_node("gen_1", "generator"),
                _make_node("eval_1", "evaluator"),
            ],
        )
        result = IntelligentOrchestrator._infer_fallback_edges(dag)
        edge_pairs = {(e.from_node, e.to_node) for e in result.edges}
        assert ("gen_1", "eval_1") in edge_pairs

    def test_infer_fallback_edges_existing_edges_preserved(self):
        dag = _make_dag(
            nodes=[
                _make_node("plan_1", "planner"),
                _make_node("gen_1", "generator"),
            ],
            edges=[("plan_1", "gen_1")],
        )
        result = IntelligentOrchestrator._infer_fallback_edges(dag)
        edge_pairs = {(e.from_node, e.to_node) for e in result.edges}
        assert ("plan_1", "gen_1") in edge_pairs

    def test_apply_rename_map_string_criteria(self):
        dag = _make_dag(
            nodes=[
                DAGNode(
                    id="n1",
                    agent_type="generator",
                    task_description="check /old_module.py for bugs",
                    success_criteria=["file exists at /old_module/test.py"],
                ),
            ],
        )
        IntelligentOrchestrator._apply_rename_map(
            dag, {"old_module": "new_module"}
        )
        node = dag.nodes["n1"]
        assert "new_module" in node.task_description
        assert "old_module" not in node.task_description

    def test_apply_rename_map_success_criterion(self):
        dag = _make_dag(
            nodes=[
                DAGNode(
                    id="n1",
                    agent_type="generator",
                    task_description="check old_module.py",
                    success_criteria=[
                        SuccessCriterion(
                            type="file_exists",
                            description="output file",
                            path="/old_module/output.py",
                        ),
                    ],
                ),
            ],
        )
        IntelligentOrchestrator._apply_rename_map(
            dag, {"old_module": "new_module"}
        )
        crit = dag.nodes["n1"].success_criteria[0]
        assert isinstance(crit, SuccessCriterion)
        assert "new_module" in crit.path
        assert "old_module" not in crit.path

    def test_apply_rename_map_empty_map_is_noop(self):
        dag = _make_dag(
            nodes=[
                DAGNode(
                    id="n1",
                    agent_type="generator",
                    task_description="check foo.py",
                    success_criteria=["file at /foo/bar.py"],
                ),
            ],
        )
        original_desc = dag.nodes["n1"].task_description
        original_criteria = list(dag.nodes["n1"].success_criteria)
        IntelligentOrchestrator._apply_rename_map(dag, {})
        assert dag.nodes["n1"].task_description == original_desc
        assert dag.nodes["n1"].success_criteria == original_criteria

    def test_count_features_enumerated(self):
        desc = "1) apply_patch, 2) create_patch, 3) merge, 4) revert"
        count = IntelligentOrchestrator._count_features(desc)
        assert count >= 4

    def test_count_features_simple_text(self):
        desc = "Build a simple API"
        count = IntelligentOrchestrator._count_features(desc)
        assert count == 0

    def test_count_features_empty_string(self):
        assert IntelligentOrchestrator._count_features("") == 0

    def test_is_response_truncated_unclosed_brace(self):
        assert is_response_truncated(
            '{"nodes": [{"id": "n1"'
        ) is True

    def test_is_response_truncated_complete_json(self):
        assert is_response_truncated(
            '{"nodes": []}'
        ) is False

    def test_is_response_truncated_empty_string(self):
        assert is_response_truncated("") is False

    def test_is_response_truncated_non_json(self):
        assert is_response_truncated(
            "just some text"
        ) is False

    def test_is_response_truncated_balanced_but_no_closing_brace(self):
        # Starts with { and does NOT end with }
        assert is_response_truncated(
            '{"nodes": [{"id": "n1"}]'
        ) is True


# ---------------------------------------------------------------------------
# LLM utils delegation
# ---------------------------------------------------------------------------

class TestLlmUtilsDelegation:

    def test_estimate_tokens(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        text = "a" * 350
        tokens = orch._estimate_tokens(text)
        assert tokens == 100  # 350 / 3.5

    def test_estimate_tokens_empty(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        assert orch._estimate_tokens("") == 0

    def test_get_context_window_known_model(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        window = orch._get_context_window()
        # Default model in llm_config fixture is "test-model", falls back to 200k
        assert window == 200_000

    def test_get_context_window_claude_model(self, tmp_store):
        config = LLMConfig(api_key="k", model="claude-sonnet-4-6")
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        assert orch._get_context_window() == 200_000

    def test_get_context_window_gpt_model(self, tmp_store):
        config = LLMConfig(api_key="k", model="gpt-4o")
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        assert orch._get_context_window() == 128_000

    def test_estimate_messages_bytes_static(self):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
        size = IntelligentOrchestrator._estimate_messages_bytes(msgs)
        # "hello" (5) + overhead (50) + "world" (5) + overhead (50) = 110
        assert size == 110

    def test_estimate_messages_bytes_empty(self):
        assert IntelligentOrchestrator._estimate_messages_bytes([]) == 0

    def test_extract_json_valid(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        result = orch._extract_json('```json\n{"key": "value"}\n```')
        assert result == {"key": "value"}

    def test_extract_json_none_for_invalid(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        result = orch._extract_json("not json at all")
        assert result is None

    def test_repair_truncated_json(self):
        # Truncated after complete value but missing closing brace
        result = IntelligentOrchestrator._repair_truncated_json(
            '{"nodes": []', 1
        )
        parsed = json.loads(result)
        assert "nodes" in parsed
        assert parsed["nodes"] == []

    def test_repair_truncated_json_after_colon(self):
        # Truncated right after a colon -- repair inserts empty string value
        result = IntelligentOrchestrator._repair_truncated_json(
            '{"nodes":', 1
        )
        parsed = json.loads(result)
        assert "nodes" in parsed

    def test_repair_truncated_json_multiple_fields(self):
        # Truncated with multiple fields, complete array
        result = IntelligentOrchestrator._repair_truncated_json(
            '{"nodes": [], "edges": [', 1
        )
        parsed = json.loads(result)
        assert "nodes" in parsed
        assert "edges" in parsed

    def test_prune_messages_for_size_static(self):
        msgs = [{"role": "user", "content": "hello"}]
        result = IntelligentOrchestrator._prune_messages_for_size_static(msgs)
        assert isinstance(result, list)
        assert len(result) >= 1


# ---------------------------------------------------------------------------
# _check_post_estimation_budget
# ---------------------------------------------------------------------------

class TestCheckPostEstimationBudget:

    def test_logs_warning_when_over_budget(self, tmp_store, llm_config, caplog):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        node = DAGNode(
            id="n1",
            agent_type="generator",
            task_description="test",
            token_budget=1000,
            estimated_tokens=5000,
        )
        dag = _make_dag(nodes=[node])

        with caplog.at_level(logging.WARNING, logger="orchestrator.intelligent_orchestrator"):
            orch._check_post_estimation_budget(dag)

        assert any("exceeds budget" in r.message for r in caplog.records)

    def test_no_warning_when_within_budget(self, tmp_store, llm_config, caplog):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        node = DAGNode(
            id="n1",
            agent_type="generator",
            task_description="test",
            token_budget=10000,
            estimated_tokens=5000,
        )
        dag = _make_dag(nodes=[node])

        with caplog.at_level(logging.WARNING, logger="orchestrator.intelligent_orchestrator"):
            orch._check_post_estimation_budget(dag)

        assert not any("exceeds budget" in r.message for r in caplog.records)

    def test_no_warning_when_estimated_zero(self, tmp_store, llm_config, caplog):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        node = DAGNode(
            id="n1",
            agent_type="generator",
            task_description="test",
            token_budget=1000,
            estimated_tokens=0,
        )
        dag = _make_dag(nodes=[node])

        with caplog.at_level(logging.WARNING, logger="orchestrator.intelligent_orchestrator"):
            orch._check_post_estimation_budget(dag)

        assert not any("exceeds budget" in r.message for r in caplog.records)

    def test_no_warning_on_empty_dag(self, tmp_store, llm_config, caplog):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        dag = DAG()

        with caplog.at_level(logging.WARNING, logger="orchestrator.intelligent_orchestrator"):
            orch._check_post_estimation_budget(dag)

        assert not any("exceeds budget" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Delegation: _estimate_dag_tokens (async)
# ---------------------------------------------------------------------------

class TestEstimateDagTokens:

    @pytest.mark.asyncio
    async def test_estimate_dag_tokens_delegates(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        dag = _make_dag(nodes=[_make_node("n1")])
        expected = _make_dag(nodes=[
            DAGNode(id="n1", agent_type="generator",
                    task_description="do something", estimated_tokens=500)
        ])
        orch._planner._estimate_dag_tokens = AsyncMock(return_value=expected)

        result = await orch._estimate_dag_tokens(dag)
        assert result is expected
        orch._planner._estimate_dag_tokens.assert_awaited_once_with(dag)


# ---------------------------------------------------------------------------
# Delegation: _truncate_requirement_if_needed
# ---------------------------------------------------------------------------

class TestTruncateRequirement:

    def test_delegates_to_llm_utils(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        with patch(
            "orchestrator.intelligent_orchestrator.truncate_requirement_if_needed",
            return_value="shortened",
        ) as mock_trunc:
            result = orch._truncate_requirement_if_needed(
                "long requirement", "system prompt", {"key": "val"}
            )
            assert result == "shortened"
            mock_trunc.assert_called_once()

    def test_none_project_context(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        with patch(
            "orchestrator.intelligent_orchestrator.truncate_requirement_if_needed",
            return_value="req",
        ) as mock_trunc:
            result = orch._truncate_requirement_if_needed(
                "req", "sys", None
            )
            assert result == "req"
            # Third arg to truncate_requirement_if_needed should be None
            args = mock_trunc.call_args[0]
            assert args[2] is None


# ---------------------------------------------------------------------------
# Class-level constants
# ---------------------------------------------------------------------------

class TestClassConstants:

    def test_prompt_templates_are_empty_strings(self):
        assert IntelligentOrchestrator.PLANNING_PROMPT_TEMPLATE == ""
        assert IntelligentOrchestrator.ADAPTATION_PROMPT_TEMPLATE == ""
        assert IntelligentOrchestrator.REPLAN_PROMPT_TEMPLATE == ""

    def test_model_context_windows_dict(self):
        windows = IntelligentOrchestrator._MODEL_CONTEXT_WINDOWS
        assert "claude-sonnet-4-6" in windows
        assert "gpt-4o" in windows
        assert windows["claude-sonnet-4-6"] == 200_000
        assert windows["gpt-4o"] == 128_000

    def test_default_context_window(self):
        assert IntelligentOrchestrator._DEFAULT_CONTEXT_WINDOW == 200_000

    def test_chars_per_token(self):
        assert IntelligentOrchestrator._CHARS_PER_TOKEN == 3.5

    def test_max_message_bytes(self):
        assert IntelligentOrchestrator._MAX_MESSAGE_BYTES == 2_097_152

    def test_planner_max_tokens(self):
        assert IntelligentOrchestrator._PLANNER_MAX_TOKENS == 8192


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:

    @pytest.mark.asyncio
    async def test_plan_with_empty_requirement(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        expected_dag = _make_dag()
        orch._planner.plan = AsyncMock(return_value=expected_dag)

        result = await orch.plan("")
        assert result is expected_dag
        orch._planner.plan.assert_awaited_once_with("", None)

    @pytest.mark.asyncio
    async def test_adapt_to_failure_with_empty_error(self, tmp_store, llm_config):
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        decision = FailureDecision(action="skip", reasoning="n/a")
        orch._adapter.adapt_to_failure = AsyncMock(return_value=decision)
        dag = _make_dag()

        result = await orch.adapt_to_failure(dag, "node_1", "")
        assert result.action == "skip"

    @pytest.mark.asyncio
    async def test_replan_preserves_return_value_identity(self, tmp_store, llm_config):
        """Verify the facade returns the exact object from the adapter."""
        registry = AgentRegistry()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=tmp_store,
            agent_registry=registry,
        )
        unique_dag = _make_dag(
            nodes=[_make_node("special_node", "planner")]
        )
        orch._adapter.replan = AsyncMock(return_value=unique_dag)

        result = await orch.replan(_make_dag(), "x", "")
        assert result is unique_dag
        assert "special_node" in result.nodes

    def test_apply_rename_map_with_no_matching_paths(self):
        dag = _make_dag(
            nodes=[
                DAGNode(
                    id="n1",
                    agent_type="generator",
                    task_description="build something",
                    success_criteria=["file exists at /foo/bar.py"],
                ),
            ],
        )
        original_desc = dag.nodes["n1"].task_description
        original_criteria = list(dag.nodes["n1"].success_criteria)
        IntelligentOrchestrator._apply_rename_map(
            dag, {"nonexistent": "replacement"}
        )
        # No matches, so nothing should change
        assert dag.nodes["n1"].task_description == original_desc
        assert dag.nodes["n1"].success_criteria == original_criteria

    def test_estimate_messages_bytes_with_unicode(self):
        msgs = [{"role": "user", "content": "hello world"}]
        size = IntelligentOrchestrator._estimate_messages_bytes(msgs)
        assert size > 0

    def test_infer_fallback_edges_single_node(self):
        dag = _make_dag(nodes=[_make_node("gen_1", "generator")])
        result = IntelligentOrchestrator._infer_fallback_edges(dag)
        # Single generator node with nothing to connect to
        assert result.edges == []

    def test_infer_fallback_edges_multiple_generators(self):
        dag = _make_dag(
            nodes=[
                _make_node("gen_1", "generator"),
                _make_node("gen_2", "generator"),
                _make_node("eval_1", "evaluator"),
            ],
        )
        result = IntelligentOrchestrator._infer_fallback_edges(dag)
        edge_pairs = {(e.from_node, e.to_node) for e in result.edges}
        # Both generators should connect to evaluator
        assert ("gen_1", "eval_1") in edge_pairs
        assert ("gen_2", "eval_1") in edge_pairs
