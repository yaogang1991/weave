"""Tests for planning response truncation fixes (#561)."""

from __future__ import annotations

import json

from core.dag_models import OrchestratorPlan
from orchestrator.llm_utils import extract_json, repair_truncated_json


# -- OrchestratorPlan edge inference tests --


class TestOrchestratorPlanEdgeInference:
    def test_edges_from_dependencies_when_missing(self):
        """When edges is empty, infer from node dependencies (#561)."""
        plan = OrchestratorPlan(
            nodes=[
                {"id": "plan", "agent_type": "planner", "task": "Plan"},
                {"id": "impl", "agent_type": "generator", "task": "Impl",
                 "dependencies": ["plan"]},
                {"id": "eval", "agent_type": "evaluator", "task": "Eval",
                 "dependencies": ["impl"]},
            ],
        )
        assert len(plan.edges) == 2
        assert {"from": "plan", "to": "impl"} in plan.edges
        assert {"from": "impl", "to": "eval"} in plan.edges

    def test_edges_preserved_when_provided(self):
        """When edges are present, don't override them."""
        plan = OrchestratorPlan(
            nodes=[
                {"id": "a", "agent_type": "generator", "task": "A"},
                {"id": "b", "agent_type": "generator", "task": "B",
                 "dependencies": ["a"]},
            ],
            edges=[{"from": "a", "to": "b", "dependency_type": "soft"}],
        )
        assert len(plan.edges) == 1
        assert plan.edges[0]["dependency_type"] == "soft"

    def test_empty_plan_no_edges(self):
        """Empty plan with no nodes produces no edges."""
        plan = OrchestratorPlan()
        assert plan.nodes == []
        assert plan.edges == []

    def test_no_dependencies_produces_no_edges(self):
        """Nodes without dependencies produce no inferred edges."""
        plan = OrchestratorPlan(
            nodes=[
                {"id": "a", "agent_type": "generator", "task": "A"},
                {"id": "b", "agent_type": "generator", "task": "B"},
            ],
        )
        assert plan.edges == []

    def test_parallel_dependencies(self):
        """Multiple nodes depending on same parent."""
        plan = OrchestratorPlan(
            nodes=[
                {"id": "foundation", "agent_type": "generator", "task": "Base"},
                {"id": "impl_a", "agent_type": "generator", "task": "A",
                 "dependencies": ["foundation"]},
                {"id": "impl_b", "agent_type": "generator", "task": "B",
                 "dependencies": ["foundation"]},
            ],
        )
        assert len(plan.edges) == 2
        from_ids = {e["from"] for e in plan.edges}
        assert from_ids == {"foundation"}

    def test_invalid_dependency_ignored(self):
        """Dependencies referencing non-existent node IDs are ignored."""
        plan = OrchestratorPlan(
            nodes=[
                {"id": "a", "agent_type": "generator", "task": "A",
                 "dependencies": ["nonexistent"]},
            ],
        )
        assert plan.edges == []

    def test_reasoning_defaults_empty(self):
        """Reasoning field has default empty string."""
        plan = OrchestratorPlan(
            nodes=[{"id": "a", "agent_type": "generator", "task": "A"}],
        )
        assert plan.reasoning == ""

    def test_truncated_json_missing_edges(self):
        """Simulate truncated JSON: nodes present, edges missing."""
        raw = json.dumps({
            "nodes": [
                {"id": "plan", "agent_type": "planner", "task": "Plan"},
                {"id": "impl", "agent_type": "generator", "task": "Impl",
                 "dependencies": ["plan"]},
            ],
        })
        data = json.loads(raw)
        plan = OrchestratorPlan(**data)
        assert len(plan.edges) == 1
        assert plan.edges[0]["from"] == "plan"


# -- JSON repair tests --


class TestRepairTruncatedJson:
    def test_unclosed_string(self):
        text = '{"key": "value'
        result = repair_truncated_json(text, 1)
        parsed = json.loads(result)
        assert parsed["key"] == "value"

    def test_unclosed_array_with_bracket(self):
        """Truncated right after opening bracket for array value."""
        text = '{"items": ['
        result = repair_truncated_json(text, 1)
        parsed = json.loads(result)
        assert parsed["items"] == []

    def test_truncated_mid_value_string(self):
        text = '{"key1": "val1", "key2": "incomplete value'
        result = repair_truncated_json(text, 1)
        parsed = json.loads(result)
        assert parsed["key1"] == "val1"
        assert "key2" in parsed

    def test_complex_truncation(self):
        """Simulate real truncation: nodes array complete, edges missing."""
        text = (
            '{"reasoning": "Plan", "nodes": '
            '[{"id": "a", "task": "do A"}], '
            '"edges": ['
        )
        # brace_depth = 1 (outer object unclosed)
        result = repair_truncated_json(text, 1)
        parsed = json.loads(result)
        assert len(parsed["nodes"]) == 1
        assert parsed["edges"] == []

    def test_truncated_in_string_array(self):
        text = '{"items": ["alpha", "beta", "gam'
        result = repair_truncated_json(text, 1)
        parsed = json.loads(result)
        assert "alpha" in parsed["items"]
        assert "beta" in parsed["items"]

    def test_truncated_after_colon(self):
        """Truncated right after colon — insert empty value."""
        text = '{"key":'
        result = repair_truncated_json(text, 1)
        parsed = json.loads(result)
        assert "key" in parsed


class TestExtractJsonWithTruncation:
    def test_extracts_valid_json_block(self):
        text = '```json\n{"nodes": [], "edges": []}\n```'
        result = extract_json(text)
        assert result is not None
        assert result["nodes"] == []

    def test_repairs_truncated_json(self):
        text = '{"nodes": [{"id": "a", "task": "A"}], "edges": ['
        result = extract_json(text)
        assert result is not None
        assert len(result["nodes"]) == 1

    def test_returns_none_for_garbage(self):
        result = extract_json("not json at all {{{{")
        assert result is None

    def test_truncated_with_code_block(self):
        """Truncated inside a code block — repair should work."""
        text = '```json\n{"nodes": [{"id": "a"}], "reasoning": "test'
        result = extract_json(text)
        assert result is not None
        assert result["nodes"][0]["id"] == "a"


# -- Truncation detection tests (#621) --


class TestTruncationDetection:
    def test_truncated_json_detected(self):
        """Response starting with { but not ending with } is truncated."""
        from orchestrator.intelligent_orchestrator import (
            IntelligentOrchestrator,
        )
        assert IntelligentOrchestrator._is_response_truncated(
            '{"nodes": [{"id": "a"'
        ) is True

    def test_complete_json_not_truncated(self):
        """Complete JSON (starts and ends with braces) is not truncated."""
        from orchestrator.intelligent_orchestrator import (
            IntelligentOrchestrator,
        )
        assert IntelligentOrchestrator._is_response_truncated(
            '{"nodes": []}'
        ) is False

    def test_empty_not_truncated(self):
        """Empty content is not truncated."""
        from orchestrator.intelligent_orchestrator import (
            IntelligentOrchestrator,
        )
        assert IntelligentOrchestrator._is_response_truncated("") is False

    def test_non_json_not_truncated(self):
        """Content not starting with { is not truncated."""
        from orchestrator.intelligent_orchestrator import (
            IntelligentOrchestrator,
        )
        assert IntelligentOrchestrator._is_response_truncated(
            "Here is the plan"
        ) is False

    def test_uneven_braces_truncated(self):
        """More opens than closes indicates truncation."""
        from orchestrator.intelligent_orchestrator import (
            IntelligentOrchestrator,
        )
        content = '{"nodes": [{"id": "a"}, {"id": "b"'
        assert IntelligentOrchestrator._is_response_truncated(content) is True

