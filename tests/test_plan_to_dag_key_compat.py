"""Tests for _plan_to_dag key name compatibility (#577)."""
from unittest.mock import MagicMock, patch

from core.models import OrchestratorPlan
from orchestrator.intelligent_orchestrator import IntelligentOrchestrator


def _make_plan(nodes: list[dict], edges: list[dict] | None = None) -> OrchestratorPlan:
    return OrchestratorPlan(
        reasoning="test",
        nodes=nodes,
        edges=edges or [],
    )


def _make_orchestrator() -> IntelligentOrchestrator:
    """Create orchestrator with mocked LLM client to avoid real API init."""
    with patch("orchestrator.intelligent_orchestrator.LLMClient"):
        return IntelligentOrchestrator(
            llm_config=MagicMock(),
            session_store=MagicMock(),
            agent_registry=MagicMock(),
        )


class TestPlanToDagTaskKeyCompat:
    """_plan_to_dag should accept task, task_description, or description keys."""

    def test_task_key_works(self):
        """Traditional 'task' key still works."""
        orch = _make_orchestrator()
        plan = _make_plan([
            {"id": "n1", "agent_type": "generator", "task": "build feature"},
        ])
        dag = orch._plan_to_dag(plan)
        assert dag.nodes["n1"].task_description == "build feature"

    def test_task_description_key_works(self):
        """'task_description' key from structured output path works."""
        orch = _make_orchestrator()
        plan = _make_plan([
            {"id": "n1", "agent_type": "generator", "task_description": "build feature"},
        ])
        dag = orch._plan_to_dag(plan)
        assert dag.nodes["n1"].task_description == "build feature"

    def test_description_key_works(self):
        """'description' key from alternative LLM outputs works."""
        orch = _make_orchestrator()
        plan = _make_plan([
            {"id": "n1", "agent_type": "generator", "description": "build feature"},
        ])
        dag = orch._plan_to_dag(plan)
        assert dag.nodes["n1"].task_description == "build feature"

    def test_task_preferred_over_description(self):
        """'task' takes priority when multiple keys present."""
        orch = _make_orchestrator()
        plan = _make_plan([
            {
                "id": "n1",
                "agent_type": "generator",
                "task": "primary task",
                "description": "fallback desc",
            },
        ])
        dag = orch._plan_to_dag(plan)
        assert dag.nodes["n1"].task_description == "primary task"

    def test_task_description_preferred_over_description(self):
        """'task_description' takes priority over 'description'."""
        orch = _make_orchestrator()
        plan = _make_plan([
            {
                "id": "n1",
                "agent_type": "generator",
                "task_description": "structured task",
                "description": "fallback desc",
            },
        ])
        dag = orch._plan_to_dag(plan)
        assert dag.nodes["n1"].task_description == "structured task"

    def test_no_key_gives_empty_string(self):
        """Missing all three keys falls back to empty string."""
        orch = _make_orchestrator()
        plan = _make_plan([
            {"id": "n1", "agent_type": "generator"},
        ])
        dag = orch._plan_to_dag(plan)
        assert dag.nodes["n1"].task_description == ""

    def test_multiple_nodes_mixed_keys(self):
        """Each node can use a different key name."""
        orch = _make_orchestrator()
        plan = _make_plan([
            {"id": "n1", "agent_type": "generator", "task": "task via task"},
            {"id": "n2", "agent_type": "generator", "task_description": "via task_description"},
            {"id": "n3", "agent_type": "generator", "description": "via description"},
        ])
        dag = orch._plan_to_dag(plan)
        assert dag.nodes["n1"].task_description == "task via task"
        assert dag.nodes["n2"].task_description == "via task_description"
        assert dag.nodes["n3"].task_description == "via description"
