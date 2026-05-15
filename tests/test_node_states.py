"""
Tests for #270: DAG node execution states and propagation rules.

Validates:
- NodeStatus enum includes PARTIAL_PASS and WARNED
- EvalStatus maps correctly to NodeStatus
- Propagation rules: PARTIAL_PASS/WARNED allow downstream to continue
- Propagation rules: FAILED only blocks hard downstream
- Independent branches not skipped on unrelated failure
- _collect_input_artifacts includes PARTIAL_PASS/WARNED upstream
- _merge_dag_results preserves PARTIAL_PASS/WARNED states
- get_execution_summary includes new status counts
"""

import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock
from datetime import datetime, timezone

from core.models import (
    DAG, DAGNode, DAGEdge, NodeStatus, EvalStatus, EvaluationResult,
    HandoffArtifact, SuccessCriterion, CriterionType,
)
from core.dag_engine import DAGExecutionEngine


def _make_node(nid: str, agent_type: str = "generator", **kw) -> DAGNode:
    return DAGNode(id=nid, agent_type=agent_type, task_description=f"task-{nid}", **kw)


def _make_dag(nodes: dict[str, DAGNode], edges: list[DAGEdge] | None = None) -> DAG:
    return DAG(nodes=nodes, edges=edges or [])


def _make_engine(**overrides) -> DAGExecutionEngine:
    defaults = {
        "agent_executor": AsyncMock(return_value={"artifacts": []}),
        "failure_handler": AsyncMock(return_value=MagicMock(action="abort", reasoning="test")),
        "evaluator": None,
        "enable_watchdog": False,
    }
    defaults.update(overrides)
    return DAGExecutionEngine(**defaults)


# =====================================================================
# NodeStatus enum
# =====================================================================

class TestNodeStatusEnum:
    """NodeStatus includes all required states."""

    def test_has_partial_pass(self):
        assert NodeStatus.PARTIAL_PASS == "partial_pass"

    def test_has_warned(self):
        assert NodeStatus.WARNED == "warned"

    def test_has_all_required_states(self):
        required = {"pending", "running", "success", "partial_pass", "warned",
                    "failed", "skipped", "retrying", "pending_approval"}
        actual = {s.value for s in NodeStatus}
        assert required <= actual


# =====================================================================
# EvalStatus → NodeStatus mapping
# =====================================================================

class TestEvalStatusMapping:
    """EvalStatus maps to correct NodeStatus."""

    def test_clean_pass_maps_to_success(self):
        assert DAGExecutionEngine._eval_status_to_node_status(
            EvalStatus.CLEAN_PASS,
        ) == NodeStatus.SUCCESS

    def test_partial_pass_maps_to_partial_pass(self):
        assert DAGExecutionEngine._eval_status_to_node_status(
            EvalStatus.PARTIAL_PASS,
        ) == NodeStatus.PARTIAL_PASS

    def test_warned_maps_to_warned(self):
        assert DAGExecutionEngine._eval_status_to_node_status(
            EvalStatus.WARNED,
        ) == NodeStatus.WARNED

    def test_failed_maps_to_failed(self):
        assert DAGExecutionEngine._eval_status_to_node_status(
            EvalStatus.FAILED,
        ) == NodeStatus.FAILED


# =====================================================================
# _is_terminal_success
# =====================================================================

class TestIsTerminalSuccess:
    """SUCCESS, PARTIAL_PASS, WARNED are all terminal success states."""

    def test_success_is_terminal(self):
        assert DAGExecutionEngine._is_terminal_success(NodeStatus.SUCCESS)

    def test_partial_pass_is_terminal(self):
        assert DAGExecutionEngine._is_terminal_success(NodeStatus.PARTIAL_PASS)

    def test_warned_is_terminal(self):
        assert DAGExecutionEngine._is_terminal_success(NodeStatus.WARNED)

    def test_failed_is_not_terminal(self):
        assert not DAGExecutionEngine._is_terminal_success(NodeStatus.FAILED)

    def test_skipped_is_not_terminal(self):
        assert not DAGExecutionEngine._is_terminal_success(NodeStatus.SKIPPED)

    def test_pending_is_not_terminal(self):
        assert not DAGExecutionEngine._is_terminal_success(NodeStatus.PENDING)


# =====================================================================
# Propagation: PARTIAL_PASS allows downstream
# =====================================================================

class TestPartialPassPropagation:
    """PARTIAL_PASS upstream → downstream continues."""

    @pytest.mark.asyncio
    async def test_partial_pass_upstream_allows_downstream(self):
        """Node with PARTIAL_PASS upstream should NOT be skipped."""
        upstream = _make_node("gen", status=NodeStatus.PARTIAL_PASS,
                              result={"summary": "ok"}, output_artifacts=["a.py"])
        downstream = _make_node("eval", agent_type="evaluator")
        dag = _make_dag(
            {"gen": upstream, "eval": downstream},
            [DAGEdge(from_node="gen", to_node="eval")],
        )

        engine = _make_engine()
        await engine._execute_single_node(dag, "eval")
        # Downstream should NOT be skipped
        assert dag.nodes["eval"].status != NodeStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_partial_pass_collects_artifacts(self):
        """_collect_input_artifacts collects from PARTIAL_PASS upstream."""
        upstream = _make_node("gen", status=NodeStatus.PARTIAL_PASS,
                              result={"summary": "partial"}, output_artifacts=["a.py"])
        downstream = _make_node("eval", agent_type="evaluator")
        dag = _make_dag(
            {"gen": upstream, "eval": downstream},
            [DAGEdge(from_node="gen", to_node="eval")],
        )

        engine = _make_engine()
        artifacts = engine._collect_input_artifacts(dag, "eval")
        assert len(artifacts) >= 1
        assert any(a.from_agent == "generator" for a in artifacts)


# =====================================================================
# Propagation: WARNED allows downstream
# =====================================================================

class TestWarnedPropagation:
    """WARNED upstream → downstream continues."""

    @pytest.mark.asyncio
    async def test_warned_upstream_allows_downstream(self):
        """Node with WARNED upstream should NOT be skipped."""
        upstream = _make_node("gen", status=NodeStatus.WARNED,
                              result={"summary": "warned"}, output_artifacts=["a.py"])
        downstream = _make_node("eval", agent_type="evaluator")
        dag = _make_dag(
            {"gen": upstream, "eval": downstream},
            [DAGEdge(from_node="gen", to_node="eval")],
        )

        engine = _make_engine()
        await engine._execute_single_node(dag, "eval")
        assert dag.nodes["eval"].status != NodeStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_warned_collects_artifacts(self):
        """_collect_input_artifacts collects from WARNED upstream."""
        upstream = _make_node("gen", status=NodeStatus.WARNED,
                              result={"summary": "warned"}, output_artifacts=["a.py"])
        downstream = _make_node("eval", agent_type="evaluator")
        dag = _make_dag(
            {"gen": upstream, "eval": downstream},
            [DAGEdge(from_node="gen", to_node="eval")],
        )

        engine = _make_engine()
        artifacts = engine._collect_input_artifacts(dag, "eval")
        assert len(artifacts) >= 1


# =====================================================================
# Propagation: FAILED blocks downstream
# =====================================================================

class TestFailedPropagation:
    """FAILED upstream → downstream skipped."""

    @pytest.mark.asyncio
    async def test_failed_upstream_skips_downstream(self):
        """Node with FAILED upstream should be SKIPPED."""
        upstream = _make_node("gen", status=NodeStatus.FAILED, error="boom")
        downstream = _make_node("eval", agent_type="evaluator")
        dag = _make_dag(
            {"gen": upstream, "eval": downstream},
            [DAGEdge(from_node="gen", to_node="eval")],
        )

        engine = _make_engine()
        await engine._execute_single_node(dag, "eval")
        assert dag.nodes["eval"].status == NodeStatus.SKIPPED


# =====================================================================
# Independent branches
# =====================================================================

class TestIndependentBranches:
    """Independent branches not skipped on unrelated failure."""

    @pytest.mark.asyncio
    async def test_independent_branch_not_skipped(self):
        """Node in branch B should not be skipped when branch A fails."""
        # Branch A: gen_a → eval_a (gen_a failed)
        # Branch B: gen_b (independent, should continue)
        gen_a = _make_node("gen_a", status=NodeStatus.FAILED, error="boom")
        gen_b = _make_node("gen_b")
        eval_a = _make_node("eval_a", agent_type="evaluator")
        dag = _make_dag(
            {"gen_a": gen_a, "gen_b": gen_b, "eval_a": eval_a},
            [DAGEdge(from_node="gen_a", to_node="eval_a")],
        )

        engine = _make_engine()
        await engine._execute_single_node(dag, "gen_b")
        # gen_b has no dependency on gen_a → should NOT be skipped
        assert dag.nodes["gen_b"].status != NodeStatus.SKIPPED
        assert dag.nodes["gen_b"].status in (NodeStatus.RUNNING, NodeStatus.SUCCESS)


# =====================================================================
# _merge_dag_results preserves PARTIAL_PASS/WARNED
# =====================================================================

class TestMergeDagResults:
    """Replan merge preserves PARTIAL_PASS and WARNED states."""

    def test_merge_preserves_partial_pass(self):
        old_dag = _make_dag({"n1": _make_node("n1", status=NodeStatus.PARTIAL_PASS,
                                               result={"s": 1}, output_artifacts=["a.py"])})
        new_dag = _make_dag({"n1": _make_node("n1"), "n2": _make_node("n2")})
        engine = _make_engine()
        merged = engine._merge_dag_results(old_dag, new_dag)
        assert merged.nodes["n1"].status == NodeStatus.PARTIAL_PASS
        assert merged.nodes["n1"].output_artifacts == ["a.py"]

    def test_merge_preserves_warned(self):
        old_dag = _make_dag({"n1": _make_node("n1", status=NodeStatus.WARNED,
                                               result={"s": 1}, output_artifacts=["b.py"])})
        new_dag = _make_dag({"n1": _make_node("n1")})
        engine = _make_engine()
        merged = engine._merge_dag_results(old_dag, new_dag)
        assert merged.nodes["n1"].status == NodeStatus.WARNED

    def test_merge_does_not_preserve_failed(self):
        old_dag = _make_dag({"n1": _make_node("n1", status=NodeStatus.FAILED,
                                               result={"s": 1})})
        new_dag = _make_dag({"n1": _make_node("n1")})
        engine = _make_engine()
        merged = engine._merge_dag_results(old_dag, new_dag)
        # Failed nodes should NOT be preserved — they get re-executed
        assert merged.nodes["n1"].status == NodeStatus.PENDING


# =====================================================================
# get_execution_summary includes new states
# =====================================================================

class TestExecutionSummary:
    """get_execution_summary includes partial_pass and warned counts."""

    def test_summary_includes_partial_pass(self):
        dag = _make_dag({
            "n1": _make_node("n1", status=NodeStatus.SUCCESS),
            "n2": _make_node("n2", status=NodeStatus.PARTIAL_PASS),
            "n3": _make_node("n3", status=NodeStatus.FAILED),
        })
        engine = _make_engine()
        summary = engine.get_execution_summary(dag)
        assert summary["success"] == 1
        assert summary["partial_pass"] == 1
        assert summary["failed"] == 1

    def test_summary_includes_warned(self):
        dag = _make_dag({
            "n1": _make_node("n1", status=NodeStatus.WARNED),
            "n2": _make_node("n2", status=NodeStatus.SKIPPED),
        })
        engine = _make_engine()
        summary = engine.get_execution_summary(dag)
        assert summary["warned"] == 1
        assert summary["skipped"] == 1

    def test_all_succeeded_with_partial_pass(self):
        """PARTIAL_PASS + SUCCESS with no failures = all_succeeded."""
        dag = _make_dag({
            "n1": _make_node("n1", status=NodeStatus.SUCCESS),
            "n2": _make_node("n2", status=NodeStatus.PARTIAL_PASS),
        })
        engine = _make_engine()
        summary = engine.get_execution_summary(dag)
        assert summary["all_succeeded"]


# =====================================================================
# Evaluator → NodeStatus mapping in execution
# =====================================================================

class TestEvaluatorToNodeStatus:
    """Evaluator EvalStatus correctly maps to NodeStatus during execution."""

    @pytest.mark.asyncio
    async def test_evaluator_clean_pass_sets_success(self):
        mock_evaluator = MagicMock()
        mock_evaluator.evaluate_stage = MagicMock(
            return_value=EvaluationResult(
                passed=True, score=10.0, eval_status=EvalStatus.CLEAN_PASS,
                criteria_results={"file": True},
            ),
        )
        engine = _make_engine(evaluator=mock_evaluator, work_dir="/tmp")
        node = _make_node("gen", success_criteria=[SuccessCriterion(
            type=CriterionType.FILE_EXISTS, path="a.py", description="a",
        )])
        dag = _make_dag({"gen": node})
        await engine._execute_single_node(dag, "gen")
        assert dag.nodes["gen"].status == NodeStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_evaluator_partial_pass_sets_partial_pass(self):
        mock_evaluator = MagicMock()
        mock_evaluator.evaluate_stage = MagicMock(
            return_value=EvaluationResult(
                passed=True, score=7.5, eval_status=EvalStatus.PARTIAL_PASS,
                criteria_results={"file": True, "lint": False},
            ),
        )
        engine = _make_engine(evaluator=mock_evaluator, work_dir="/tmp")
        node = _make_node("gen", success_criteria=[SuccessCriterion(
            type=CriterionType.FILE_EXISTS, path="a.py", description="a",
        )])
        dag = _make_dag({"gen": node})
        await engine._execute_single_node(dag, "gen")
        assert dag.nodes["gen"].status == NodeStatus.PARTIAL_PASS

    @pytest.mark.asyncio
    async def test_evaluator_warned_sets_warned(self):
        mock_evaluator = MagicMock()
        mock_evaluator.evaluate_stage = MagicMock(
            return_value=EvaluationResult(
                passed=True, score=10.0, eval_status=EvalStatus.WARNED,
                criteria_results={"review": True},
                suggestions=["review"],
            ),
        )
        engine = _make_engine(evaluator=mock_evaluator, work_dir="/tmp")
        node = _make_node("gen", success_criteria=[SuccessCriterion(
            type=CriterionType.CUSTOM, description="manual review",
        )])
        dag = _make_dag({"gen": node})
        await engine._execute_single_node(dag, "gen")
        assert dag.nodes["gen"].status == NodeStatus.WARNED

    @pytest.mark.asyncio
    async def test_evaluator_failed_sets_failed(self):
        mock_evaluator = MagicMock()
        mock_evaluator.evaluate_stage = MagicMock(
            return_value=EvaluationResult(
                passed=False, score=0.0, eval_status=EvalStatus.FAILED,
                criteria_results={"file": False},
            ),
        )
        engine = _make_engine(evaluator=mock_evaluator)
        node = _make_node("gen", success_criteria=[SuccessCriterion(
            type=CriterionType.FILE_EXISTS, path="a.py", description="a",
        )])
        dag = _make_dag({"gen": node})
        await engine._execute_single_node(dag, "gen")
        assert dag.nodes["gen"].status == NodeStatus.FAILED


# =====================================================================
# EvalStatus in evaluator engine
# =====================================================================

class TestEvaluatorEvalStatus:
    """Evaluator engine sets correct EvalStatus in EvaluationResult."""

    def test_clean_pass_no_threshold(self, tmp_path):
        from session.store import SessionStore
        from evaluator.engine import EvaluatorEngine
        store = SessionStore(base_path=str(tmp_path / "events"))
        ev = EvaluatorEngine(store)
        (tmp_path / "a.py").write_text("ok", encoding="utf-8")
        result = ev.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py",
                             description="a"),
        ], str(tmp_path))
        assert result.eval_status == EvalStatus.CLEAN_PASS

    def test_partial_pass_via_threshold(self, tmp_path):
        from session.store import SessionStore
        from evaluator.engine import EvaluatorEngine
        store = SessionStore(base_path=str(tmp_path / "events"))
        ev = EvaluatorEngine(store, pass_threshold=5.0)
        (tmp_path / "a.py").write_text("ok", encoding="utf-8")
        (tmp_path / "b.py").write_text("ok", encoding="utf-8")
        (tmp_path / "c.py").write_text("# TODO\n", encoding="utf-8")
        result = ev.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py",
                             description="a"),
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="b.py",
                             description="b"),
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="c.py",
                             description="c"),
            SuccessCriterion(type=CriterionType.NO_CRITICAL,
                             description="markers"),
        ], str(tmp_path), output_artifacts=["c.py"])
        assert result.eval_status == EvalStatus.PARTIAL_PASS

    def test_warned_with_uncheckable(self, tmp_path):
        from session.store import SessionStore
        from evaluator.engine import EvaluatorEngine
        store = SessionStore(base_path=str(tmp_path / "events"))
        ev = EvaluatorEngine(store)
        result = ev.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.CUSTOM,
                             description="manual review"),
        ], str(tmp_path))
        assert result.eval_status == EvalStatus.WARNED

    def test_failed(self, tmp_path):
        from session.store import SessionStore
        from evaluator.engine import EvaluatorEngine
        store = SessionStore(base_path=str(tmp_path / "events"))
        ev = EvaluatorEngine(store)
        result = ev.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="missing.py",
                             description="missing"),
        ], str(tmp_path))
        assert result.eval_status == EvalStatus.FAILED
