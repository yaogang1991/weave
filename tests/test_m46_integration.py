"""M4.6 Phase 6: End-to-end integration tests for token-aware DAG pipeline."""
import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

from core.config import TokenEstimationConfig
from core.dag_models import DAG, DAGNode
from core.token_estimator import (
    NodeTokenContext,
    TokenEstimator,
    build_node_context,
)
from orchestrator.plan_validator import PlanValidator, PlanValidationError
from agent.prompts import SYSTEM_PROMPTS


class TestTokenEstimationPipeline:
    def test_estimate_all_agent_types(self):
        cfg = TokenEstimationConfig()
        est = TokenEstimator(cfg, client=None)
        loop = asyncio.get_event_loop()
        for agent_type in ["generator", "evaluator", "planner"]:
            node = DAGNode(
                id=f"test_{agent_type}",
                agent_type=agent_type,
                task_description="Implement user authentication module",
            )
            ctx = build_node_context(node, SYSTEM_PROMPTS)
            result = loop.run_until_complete(
                est.estimate_node_tokens(node.id, ctx),
            )
            assert result.estimated_tokens > 0
            assert result.breakdown["system"] > 0

    def test_batch_estimation_sets_estimated_tokens(self):
        cfg = TokenEstimationConfig()
        est = TokenEstimator(cfg, client=None)
        dag = DAG()
        for i in range(5):
            dag.add_node(DAGNode(
                id=f"gen_{i}", agent_type="generator",
                task_description=f"Build module {i} with CRUD",
            ))
        nodes = [
            (nid, build_node_context(node, SYSTEM_PROMPTS))
            for nid, node in dag.nodes.items()
        ]
        results = asyncio.get_event_loop().run_until_complete(
            est.estimate_nodes_batch(nodes),
        )
        assert len(results) == 5
        for r in results:
            dag.update_node(r.node_id, estimated_tokens=r.estimated_tokens)
        for node in dag.nodes.values():
            assert node.estimated_tokens > 0

    def test_api_estimation_with_mock(self):
        mock_result = MagicMock(input_tokens=3500)
        client = MagicMock()
        client.messages.count_tokens = AsyncMock(return_value=mock_result)
        cfg = TokenEstimationConfig()
        est = TokenEstimator(cfg, client=client)
        ctx = NodeTokenContext(
            system_prompt="You are a generator.",
            task_description="Build auth",
            agent_type="generator",
        )
        result = asyncio.get_event_loop().run_until_complete(
            est.estimate_node_tokens("gen_1", ctx),
        )
        assert result.estimation_method == "api"
        assert result.estimated_tokens == 3500


class TestPlanValidationTokenBudget:
    def test_max_nodes_default_25(self):
        assert PlanValidator().max_nodes == 25

    def test_max_nodes_custom_raises(self):
        v = PlanValidator(max_nodes=3)
        nodes = [
            {"id": f"n{i}", "agent_type": "gen", "task": f"t{i}"}
            for i in range(4)
        ]
        with pytest.raises(PlanValidationError, match="3"):
            v.validate({"nodes": nodes, "edges": []})

    def test_token_budget_warning(self):
        v = PlanValidator()
        nodes = [{
            "id": "big", "agent_type": "gen", "task": "huge",
            "estimated_tokens": 10000, "token_budget": 8192,
        }]
        v.validate({"nodes": nodes, "edges": []})
        assert any("exceeds budget" in w for w in v.warnings)

    def test_no_warning_within_budget(self):
        v = PlanValidator()
        nodes = [{
            "id": "ok", "agent_type": "gen", "task": "ok",
            "estimated_tokens": 5000, "token_budget": 8192,
        }]
        v.validate({"nodes": nodes, "edges": []})
        assert not any("exceeds budget" in w for w in v.warnings)


class TestDAGNodeTokenRoundTrip:
    def test_create_estimate_execute_track(self):
        node = DAGNode(id="gen", agent_type="gen", task_description="Build auth")
        assert node.token_budget == 8192
        assert node.estimated_tokens == 0
        assert node.actual_tokens == 0

        dag = DAG()
        dag.add_node(node)
        dag.update_node("gen", estimated_tokens=4500)
        dag.update_node("gen", actual_tokens=4200, token_usage={
            "input_tokens": 3200, "output_tokens": 1000, "total_tokens": 4200,
        })
        updated = dag.nodes["gen"]
        assert updated.actual_tokens == 4200
        pct = abs(updated.estimated_tokens - updated.actual_tokens) / updated.actual_tokens * 100
        assert pct <= 30

    def test_dag_total_budget(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", agent_type="gen", task_description="t", token_budget=4096))
        dag.add_node(DAGNode(id="b", agent_type="gen", task_description="t"))
        assert dag.total_token_budget == 12288


class TestCrossComponentIntegration:
    def test_full_pipeline(self):
        dag = DAG()
        for i in range(4):
            dag.add_node(DAGNode(
                id=f"gen_{i}", agent_type="generator",
                task_description=f"Implement module {i} with CRUD and tests",
            ))
        cfg = TokenEstimationConfig()
        est = TokenEstimator(cfg, client=None)
        nodes = [
            (nid, build_node_context(node, SYSTEM_PROMPTS))
            for nid, node in dag.nodes.items()
        ]
        results = asyncio.get_event_loop().run_until_complete(
            est.estimate_nodes_batch(nodes),
        )
        for r in results:
            dag.update_node(r.node_id, estimated_tokens=r.estimated_tokens)

        plan_data = {
            "nodes": [
                {"id": nid, "agent_type": n.agent_type, "task": n.task_description,
                 "estimated_tokens": n.estimated_tokens, "token_budget": n.token_budget}
                for nid, n in dag.nodes.items()
            ],
            "edges": [],
        }
        validator = PlanValidator()
        validator.validate(plan_data)
        for node in dag.nodes.values():
            assert node.estimated_tokens > 0

    def test_config_margins_cover_agent_types(self):
        cfg = TokenEstimationConfig()
        for t in ["generator", "evaluator", "planner"]:
            assert t in cfg.overhead_margins
            assert cfg.overhead_margins[t] > 0
