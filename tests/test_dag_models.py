"""Tests for core/dag_models.py — DAG domain models.

Covers:
- DAGNode creation, validation, heartbeat, health check
- DAG construction, topological sort, ready nodes
- DAGEdge, DependencyType, ExecutionEvent
- Immutability: model_copy produces new instances
- Edge cases: empty DAG, cycle detection, soft/hard deps
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.dag_models import (
    AgentCapability,
    DAG,
    DAGEdge,
    DAGNode,
    DAGNodeModel,
    DAGOutputModel,
    DependencyType,
    ExecutionEvent,
    FailureDecision,
    HandoffArtifact,
    NodeHealth,
    NodeStatus,
    NodeWorkspace,
    NodeWorkspaceResult,
    NodeWorkspaceStrategy,
    OrchestratorPlan,
)


# ---------------------------------------------------------------------------
# DAGNode
# ---------------------------------------------------------------------------


class TestDAGNodeCreation:
    """Test DAGNode model creation and defaults."""

    def test_default_status_is_pending(self):
        node = DAGNode(id="n1", agent_type="generator", task_description="Build API")
        assert node.status == NodeStatus.PENDING

    def test_auto_id_generation(self):
        node = DAGNode(id="", agent_type="generator", task_description="Test")
        assert node.id.startswith("node_")
        assert len(node.id) > 5

    def test_default_values(self):
        node = DAGNode(id="n1", agent_type="generator", task_description="Test")
        assert node.retry_count == 0
        assert node.max_retries == 3
        assert node.error == ""
        assert node.result == {}
        assert node.output_artifacts == []
        assert node.token_usage == {}
        assert node.health_status == NodeHealth.HEALTHY
        assert node.heartbeat_count == 0
        assert node.missed_heartbeats == 0
        assert node.token_budget == 8192

    def test_with_all_fields(self):
        node = DAGNode(
            id="n1",
            agent_type="evaluator",
            task_description="Evaluate code",
            status=NodeStatus.RUNNING,
            max_retries=5,
            token_budget=16384,
            backend="codex",
        )
        assert node.agent_type == "evaluator"
        assert node.status == NodeStatus.RUNNING
        assert node.max_retries == 5
        assert node.token_budget == 16384
        assert node.backend == "codex"


class TestDAGNodeHeartbeat:
    """Test DAGNode heartbeat recording."""

    def test_record_heartbeat_updates_timestamp(self):
        node = DAGNode(id="n1", agent_type="gen", task_description="Test")
        before = datetime.now(timezone.utc)
        node.record_heartbeat()
        after = datetime.now(timezone.utc)
        assert before <= node.last_heartbeat_at <= after

    def test_record_heartbeat_increments_count(self):
        node = DAGNode(id="n1", agent_type="gen", task_description="Test")
        assert node.heartbeat_count == 0
        node.record_heartbeat()
        assert node.heartbeat_count == 1
        node.record_heartbeat()
        assert node.heartbeat_count == 2

    def test_record_heartbeat_resets_missed(self):
        node = DAGNode(
            id="n1", agent_type="gen", task_description="Test",
            missed_heartbeats=5,
        )
        node.record_heartbeat()
        assert node.missed_heartbeats == 0

    def test_record_heartbeat_recovers_health(self):
        node = DAGNode(
            id="n1", agent_type="gen", task_description="Test",
            health_status=NodeHealth.UNHEALTHY,
        )
        node.record_heartbeat()
        assert node.health_status == NodeHealth.HEALTHY


class TestDAGNodeHealthCheck:
    """Test DAGNode health checking logic."""

    def test_healthy_node_within_interval(self):
        now = datetime.now(timezone.utc)
        node = DAGNode(
            id="n1", agent_type="gen", task_description="Test",
            status=NodeStatus.RUNNING,
            started_at=now,
            last_heartbeat_at=now,
        )
        result = node.check_health(heartbeat_interval_sec=5.0)
        assert result == NodeHealth.HEALTHY

    def test_unhealthy_after_miss_threshold(self):
        old = datetime.now(timezone.utc) - timedelta(seconds=30)
        node = DAGNode(
            id="n1", agent_type="gen", task_description="Test",
            status=NodeStatus.RUNNING,
            started_at=old,
            last_heartbeat_at=old,
        )
        result = node.check_health(heartbeat_interval_sec=5.0, miss_threshold=3)
        assert result == NodeHealth.UNHEALTHY

    def test_missed_below_threshold(self):
        old = datetime.now(timezone.utc) - timedelta(seconds=8)
        node = DAGNode(
            id="n1", agent_type="gen", task_description="Test",
            status=NodeStatus.RUNNING,
            started_at=old,
            last_heartbeat_at=old,
        )
        result = node.check_health(heartbeat_interval_sec=5.0, miss_threshold=3)
        assert result == NodeHealth.MISSED

    def test_non_running_node_returns_current(self):
        node = DAGNode(
            id="n1", agent_type="gen", task_description="Test",
            status=NodeStatus.PENDING,
        )
        result = node.check_health()
        assert result == NodeHealth.HEALTHY  # Default health status

    def test_no_heartbeat_no_start_returns_current(self):
        node = DAGNode(
            id="n1", agent_type="gen", task_description="Test",
            status=NodeStatus.RUNNING,
            started_at=None,
            last_heartbeat_at=None,
        )
        result = node.check_health()
        assert result == NodeHealth.HEALTHY  # Cannot determine elapsed


class TestDAGNodeSuccessCriteria:
    """Test success_criteria normalization."""

    def test_string_criteria(self):
        node = DAGNode(
            id="n1", agent_type="gen", task_description="Test",
            success_criteria=["File exists", "Tests pass"],
        )
        assert len(node.success_criteria) == 2

    def test_empty_criteria(self):
        node = DAGNode(
            id="n1", agent_type="gen", task_description="Test",
            success_criteria=[],
        )
        assert node.success_criteria == []


# ---------------------------------------------------------------------------
# DAGEdge & DependencyType
# ---------------------------------------------------------------------------


class TestDAGEdge:
    """Test DAGEdge model."""

    def test_default_hard_dependency(self):
        edge = DAGEdge(from_node="a", to_node="b")
        assert edge.dependency_type == DependencyType.HARD

    def test_soft_dependency(self):
        edge = DAGEdge(from_node="a", to_node="b", dependency_type=DependencyType.SOFT)
        assert edge.dependency_type == DependencyType.SOFT


# ---------------------------------------------------------------------------
# DAG
# ---------------------------------------------------------------------------


class TestDAGConstruction:
    """Test DAG construction and basic operations."""

    def test_empty_dag(self):
        dag = DAG()
        assert len(dag.nodes) == 0
        assert len(dag.edges) == 0

    def test_add_node(self):
        dag = DAG()
        node = DAGNode(id="n1", agent_type="gen", task_description="Test")
        dag.add_node(node)
        assert "n1" in dag.nodes
        assert dag.nodes["n1"].task_description == "Test"

    def test_update_node_returns_new_node(self):
        dag = DAG()
        node = DAGNode(id="n1", agent_type="gen", task_description="Test")
        dag.add_node(node)
        updated = dag.update_node("n1", status=NodeStatus.RUNNING, error="test")
        assert updated.status == NodeStatus.RUNNING
        assert updated.error == "test"
        # Updated node is in the DAG
        assert dag.nodes["n1"] is updated

    def test_update_node_preserves_original(self):
        dag = DAG()
        node = DAGNode(id="n1", agent_type="gen", task_description="Test")
        dag.add_node(node)
        old_id = id(node)
        dag.update_node("n1", status=NodeStatus.RUNNING)
        # The old node object is not mutated (model_copy creates new)
        assert id(dag.nodes["n1"]) != old_id

    def test_add_edge(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", agent_type="gen", task_description="A"))
        dag.add_node(DAGNode(id="b", agent_type="gen", task_description="B"))
        dag.add_edge("a", "b")
        assert len(dag.edges) == 1
        assert dag.edges[0].from_node == "a"
        assert dag.edges[0].to_node == "b"

    def test_add_edge_with_type(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", agent_type="gen", task_description="A"))
        dag.add_node(DAGNode(id="b", agent_type="gen", task_description="B"))
        dag.add_edge("a", "b", DependencyType.SOFT)
        assert dag.edges[0].dependency_type == DependencyType.SOFT


class TestDAGDependencies:
    """Test DAG dependency queries."""

    @pytest.fixture
    def linear_dag(self):
        """a -> b -> c"""
        dag = DAG()
        for nid in ("a", "b", "c"):
            dag.add_node(DAGNode(id=nid, agent_type="gen", task_description=f"Node {nid}"))
        dag.add_edge("a", "b")
        dag.add_edge("b", "c")
        return dag

    @pytest.fixture
    def mixed_dag(self):
        """a --hard--> b --soft--> c"""
        dag = DAG()
        for nid in ("a", "b", "c"):
            dag.add_node(DAGNode(id=nid, agent_type="gen", task_description=f"Node {nid}"))
        dag.add_edge("a", "b", DependencyType.HARD)
        dag.add_edge("b", "c", DependencyType.SOFT)
        return dag

    def test_get_dependencies(self, linear_dag):
        assert linear_dag.get_dependencies("b") == ["a"]
        assert linear_dag.get_dependencies("c") == ["b"]
        assert linear_dag.get_dependencies("a") == []

    def test_get_dependents(self, linear_dag):
        assert linear_dag.get_dependents("a") == ["b"]
        assert linear_dag.get_dependents("b") == ["c"]
        assert linear_dag.get_dependents("c") == []

    def test_get_hard_dependencies(self, mixed_dag):
        assert mixed_dag.get_hard_dependencies("b") == ["a"]
        assert mixed_dag.get_hard_dependencies("c") == []  # b->c is SOFT

    def test_get_soft_dependencies(self, mixed_dag):
        assert mixed_dag.get_soft_dependencies("b") == []
        assert mixed_dag.get_soft_dependencies("c") == ["b"]  # b->c is SOFT


class TestDAGTopologicalSort:
    """Test topological level computation."""

    def test_linear_dag(self):
        dag = DAG()
        for nid in ("a", "b", "c"):
            dag.add_node(DAGNode(id=nid, agent_type="gen", task_description=nid))
        dag.add_edge("a", "b")
        dag.add_edge("b", "c")
        levels = dag.topological_levels()
        assert levels == [["a"], ["b"], ["c"]]

    def test_parallel_nodes(self):
        dag = DAG()
        for nid in ("a", "b", "c"):
            dag.add_node(DAGNode(id=nid, agent_type="gen", task_description=nid))
        # a -> c, b -> c (a and b are parallel)
        dag.add_edge("a", "c")
        dag.add_edge("b", "c")
        levels = dag.topological_levels()
        assert len(levels) == 2
        assert set(levels[0]) == {"a", "b"}
        assert levels[1] == ["c"]

    def test_diamond_dag(self):
        dag = DAG()
        for nid in ("a", "b", "c", "d"):
            dag.add_node(DAGNode(id=nid, agent_type="gen", task_description=nid))
        dag.add_edge("a", "b")
        dag.add_edge("a", "c")
        dag.add_edge("b", "d")
        dag.add_edge("c", "d")
        levels = dag.topological_levels()
        assert levels[0] == ["a"]
        assert set(levels[1]) == {"b", "c"}
        assert levels[2] == ["d"]

    def test_cycle_detection(self):
        dag = DAG()
        for nid in ("a", "b", "c"):
            dag.add_node(DAGNode(id=nid, agent_type="gen", task_description=nid))
        dag.add_edge("a", "b")
        dag.add_edge("b", "c")
        dag.add_edge("c", "a")
        with pytest.raises(ValueError, match="Cycle detected"):
            dag.topological_levels()


class TestDAGReadyNodes:
    """Test get_ready_nodes logic."""

    def test_initial_all_pending(self):
        dag = DAG()
        for nid in ("a", "b", "c"):
            dag.add_node(DAGNode(id=nid, agent_type="gen", task_description=nid))
        # No edges, all should be ready
        assert set(dag.get_ready_nodes()) == {"a", "b", "c"}

    def test_dep_must_succeed(self):
        dag = DAG()
        for nid in ("a", "b"):
            dag.add_node(DAGNode(id=nid, agent_type="gen", task_description=nid))
        dag.add_edge("a", "b")
        # a is PENDING, b should not be ready
        assert "b" not in dag.get_ready_nodes()
        # a succeeds, b should be ready
        dag.update_node("a", status=NodeStatus.SUCCESS)
        assert "b" in dag.get_ready_nodes()

    def test_dep_failed_blocks_hard(self):
        dag = DAG()
        for nid in ("a", "b"):
            dag.add_node(DAGNode(id=nid, agent_type="gen", task_description=nid))
        dag.add_edge("a", "b", DependencyType.HARD)
        dag.update_node("a", status=NodeStatus.FAILED)
        assert "b" not in dag.get_ready_nodes()

    def test_dep_failed_allows_soft(self):
        dag = DAG()
        for nid in ("a", "b"):
            dag.add_node(DAGNode(id=nid, agent_type="gen", task_description=nid))
        dag.add_edge("a", "b", DependencyType.SOFT)
        dag.update_node("a", status=NodeStatus.FAILED)
        # Soft dep: upstream FAILED is terminal, so b is ready
        assert "b" in dag.get_ready_nodes()

    def test_running_dep_not_ready(self):
        dag = DAG()
        for nid in ("a", "b"):
            dag.add_node(DAGNode(id=nid, agent_type="gen", task_description=nid))
        dag.add_edge("a", "b")
        dag.update_node("a", status=NodeStatus.RUNNING)
        assert "b" not in dag.get_ready_nodes()


class TestDAGTokenBudget:
    """Test token budget aggregation."""

    def test_total_token_budget(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", agent_type="gen", task_description="A", token_budget=1000))
        dag.add_node(DAGNode(id="b", agent_type="gen", task_description="B", token_budget=2000))
        assert dag.total_token_budget == 3000

    def test_empty_dag_budget(self):
        assert DAG().total_token_budget == 0


# ---------------------------------------------------------------------------
# ExecutionEvent
# ---------------------------------------------------------------------------


class TestExecutionEvent:
    """Test ExecutionEvent model."""

    def test_event_creation(self):
        event = ExecutionEvent(
            node_id="n1",
            event_type="started",
            details={"key": "value"},
        )
        assert event.node_id == "n1"
        assert event.event_type == "started"
        assert event.details == {"key": "value"}
        assert isinstance(event.timestamp, datetime)

    def test_event_with_all_fields(self):
        now = datetime.now(timezone.utc)
        event = ExecutionEvent(
            timestamp=now,
            node_id="n2",
            event_type="failed",
            details={},
        )
        assert event.timestamp == now


# ---------------------------------------------------------------------------
# FailureDecision
# ---------------------------------------------------------------------------


class TestFailureDecision:
    """Test FailureDecision model."""

    def test_retry_decision(self):
        d = FailureDecision(action="retry", reasoning="Transient error")
        assert d.action == "retry"
        assert d.reasoning == "Transient error"

    def test_replan_with_modifications(self):
        d = FailureDecision(
            action="replan",
            modifications={"add_nodes": ["n3"]},
        )
        assert d.action == "replan"
        assert "add_nodes" in d.modifications


# ---------------------------------------------------------------------------
# OrchestratorPlan
# ---------------------------------------------------------------------------


class TestOrchestratorPlan:
    """Test OrchestratorPlan edge inference."""

    def test_infer_edges_from_dependencies(self):
        plan = OrchestratorPlan(
            reasoning="test",
            nodes=[
                {"id": "a", "dependencies": []},
                {"id": "b", "dependencies": ["a"]},
                {"id": "c", "dependencies": ["a", "b"]},
            ],
            edges=[],  # Empty — should infer from dependencies
        )
        assert len(plan.edges) == 3
        edge_pairs = {(e["from"], e["to"]) for e in plan.edges}
        assert ("a", "b") in edge_pairs
        assert ("a", "c") in edge_pairs
        assert ("b", "c") in edge_pairs

    def test_no_infer_when_edges_present(self):
        plan = OrchestratorPlan(
            reasoning="test",
            nodes=[{"id": "a"}, {"id": "b"}],
            edges=[{"from": "a", "to": "b"}],
        )
        assert len(plan.edges) == 1

    def test_infer_ignores_unknown_deps(self):
        plan = OrchestratorPlan(
            reasoning="test",
            nodes=[
                {"id": "a", "dependencies": []},
                {"id": "b", "dependencies": ["a", "unknown"]},
            ],
            edges=[],
        )
        # Only valid deps produce edges
        assert len(plan.edges) == 1
        assert plan.edges[0] == {"from": "a", "to": "b"}


# ---------------------------------------------------------------------------
# HandoffArtifact
# ---------------------------------------------------------------------------


class TestHandoffArtifact:
    """Test HandoffArtifact model."""

    def test_creation(self):
        artifact = HandoffArtifact(
            from_agent="planner",
            to_agent="generator",
            content="Plan summary",
            file_paths=["src/main.py"],
        )
        assert artifact.from_agent == "planner"
        assert artifact.to_agent == "generator"
        assert len(artifact.file_paths) == 1


# ---------------------------------------------------------------------------
# DAGNodeModel / DAGOutputModel (structured output)
# ---------------------------------------------------------------------------


class TestStructuredOutputModels:
    """Test structured output models for DAG generation."""

    def test_dag_node_model(self):
        m = DAGNodeModel(
            id="gen_auth",
            agent_type="generator",
            task_description="Build auth module",
            dependencies=["plan_auth"],
        )
        assert m.id == "gen_auth"
        assert m.dependencies == ["plan_auth"]

    def test_dag_output_model(self):
        m = DAGOutputModel(
            nodes=[
                DAGNodeModel(id="a", agent_type="planner", task_description="Plan"),
            ],
            reasoning="Test plan",
        )
        assert len(m.nodes) == 1
        assert m.reasoning == "Test plan"


# ---------------------------------------------------------------------------
# NodeWorkspace models
# ---------------------------------------------------------------------------


class TestNodeWorkspace:
    """Test workspace isolation models."""

    def test_default_shared_workspace(self):
        ws = NodeWorkspace(node_id="n1")
        assert ws.strategy == NodeWorkspaceStrategy.SHARED

    def test_worktree_workspace(self):
        ws = NodeWorkspace(
            node_id="n1",
            strategy=NodeWorkspaceStrategy.WORKTREE,
            workspace_path="/tmp/worktree_n1",
        )
        assert ws.strategy == NodeWorkspaceStrategy.WORKTREE

    def test_workspace_result(self):
        r = NodeWorkspaceResult(
            node_id="n1",
            changed_files=["a.py", "b.py"],
            merge_status="merged",
        )
        assert len(r.changed_files) == 2
        assert r.merge_status == "merged"


# ---------------------------------------------------------------------------
# AgentCapability
# ---------------------------------------------------------------------------


class TestAgentCapability:
    """Test AgentCapability model."""

    def test_creation(self):
        cap = AgentCapability(
            id="generator",
            name="Code Generator",
            description="Generates code from plans",
            skills=["python", "javascript"],
        )
        assert cap.id == "generator"
        assert len(cap.skills) == 2
