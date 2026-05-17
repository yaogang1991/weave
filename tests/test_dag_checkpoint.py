"""Tests for DAG execution state persistence (#455).

Verifies:
1. _persist_node_completion writes checkpoint entries
2. _load_completed_nodes reads and returns completed node IDs
3. execute() skips checkpointed nodes on recovery
4. Checkpoint file is cleaned up after successful DAG completion
5. Partial completion: some nodes checkpointed, rest executed
6. Corrupt checkpoint entries are skipped gracefully
7. No session_id: checkpoint operations are no-ops
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

from core.models import DAG, DAGNode, NodeStatus, FailureDecision  # noqa: E402
from core.dag_engine import DAGExecutionEngine  # noqa: E402


async def _noop_executor(node, artifacts, **kwargs):
    return {"status": "completed", "summary": "done", "artifacts": []}


async def _noop_failure_handler(dag, node_id, error):
    return FailureDecision(action="abort", reasoning="test")


def _make_engine(tmp_path, session_id="test-session"):
    return DAGExecutionEngine(
        _noop_executor,
        _noop_failure_handler,
        session_id=session_id,
        checkpoint_dir=str(tmp_path / "dag_progress"),
    )


def _make_three_node_dag():
    dag = DAG(reasoning="test")
    dag.add_node(DAGNode(id="a", agent_type="planner", task_description="plan"))
    dag.add_node(DAGNode(id="b", agent_type="generator", task_description="impl"))
    dag.add_node(DAGNode(id="c", agent_type="evaluator", task_description="eval"))
    dag.add_edge("a", "b")
    dag.add_edge("b", "c")
    return dag


class TestPersistNodeCompletion:
    def test_creates_checkpoint_file(self, tmp_path):
        engine = _make_engine(tmp_path)
        engine._persist_node_completion("node_a", {"status": "completed"})

        path = engine._checkpoint_file()
        assert path.exists()

    def test_writes_valid_jsonl(self, tmp_path):
        engine = _make_engine(tmp_path)
        engine._persist_node_completion("node_a", {"status": "ok"})
        engine._persist_node_completion("node_b", None)

        lines = engine._checkpoint_file().read_text().strip().splitlines()
        assert len(lines) == 2

        entry_a = json.loads(lines[0])
        assert entry_a["node_id"] == "node_a"
        assert entry_a["status"] == "completed"
        assert "timestamp" in entry_a

        entry_b = json.loads(lines[1])
        assert entry_b["node_id"] == "node_b"
        assert entry_b["status"] == "completed"

    def test_result_summary_filtered(self, tmp_path):
        engine = _make_engine(tmp_path)
        engine._persist_node_completion("node_a", {
            "status": "completed",
            "summary": "done",
            "output": "file.py",
            "internal_data": "should_be_excluded",
        })

        entry = json.loads(
            engine._checkpoint_file().read_text().strip()
        )
        assert "internal_data" not in entry.get("result_summary", {})
        assert "summary" in entry["result_summary"]

    def test_noop_without_session_id(self, tmp_path):
        engine = _make_engine(tmp_path, session_id=None)
        engine._persist_node_completion("node_a", {"status": "ok"})

        assert not engine._checkpoint_file().exists()


class TestLoadCompletedNodes:
    def test_returns_completed_node_ids(self, tmp_path):
        engine = _make_engine(tmp_path)
        path = engine._checkpoint_file()
        path.write_text(
            '{"node_id": "a", "status": "completed"}\n'
            '{"node_id": "b", "status": "completed"}\n'
        )

        result = engine._load_completed_nodes()
        assert result == {"a", "b"}

    def test_skips_non_completed_entries(self, tmp_path):
        engine = _make_engine(tmp_path)
        path = engine._checkpoint_file()
        path.write_text(
            '{"node_id": "a", "status": "completed"}\n'
            '{"node_id": "b", "status": "failed"}\n'
        )

        result = engine._load_completed_nodes()
        assert result == {"a"}

    def test_returns_empty_for_missing_file(self, tmp_path):
        engine = _make_engine(tmp_path)
        result = engine._load_completed_nodes()
        assert result == set()

    def test_skips_corrupt_lines(self, tmp_path):
        engine = _make_engine(tmp_path)
        path = engine._checkpoint_file()
        path.write_text(
            '{"node_id": "a", "status": "completed"}\n'
            'NOT VALID JSON\n'
            '{"node_id": "c", "status": "completed"}\n'
        )

        result = engine._load_completed_nodes()
        assert result == {"a", "c"}

    def test_noop_without_session_id(self, tmp_path):
        engine = _make_engine(tmp_path, session_id=None)
        result = engine._load_completed_nodes()
        assert result == set()


class TestCleanupCheckpoint:
    def test_removes_checkpoint_file(self, tmp_path):
        engine = _make_engine(tmp_path)
        engine._persist_node_completion("node_a", None)
        assert engine._checkpoint_file().exists()

        engine._cleanup_checkpoint()
        assert not engine._checkpoint_file().exists()

    def test_noop_when_no_file(self, tmp_path):
        engine = _make_engine(tmp_path)
        engine._cleanup_checkpoint()  # Should not raise

    def test_noop_without_session_id(self, tmp_path):
        engine = _make_engine(tmp_path, session_id=None)
        engine._cleanup_checkpoint()  # Should not raise


class TestCrashRecovery:
    @pytest.mark.asyncio
    async def test_skips_completed_nodes(self, tmp_path):
        """execute() skips nodes that were completed before crash."""
        engine = _make_engine(tmp_path)

        # Pre-populate checkpoint: node "a" already completed
        engine._persist_node_completion("a", {"status": "completed"})

        # Create engine with tracking executor
        executed_nodes = []

        async def tracking_executor(node, artifacts, **kwargs):
            executed_nodes.append(node.id)
            return {"status": "completed", "summary": "done", "artifacts": []}

        engine2 = DAGExecutionEngine(
            tracking_executor,
            _noop_failure_handler,
            session_id="test-session",
            checkpoint_dir=str(tmp_path / "dag_progress"),
        )

        dag = _make_three_node_dag()
        await engine2.execute(dag)

        # "a" was checkpointed, should not be re-executed
        assert "a" not in executed_nodes
        assert "b" in executed_nodes
        assert "c" in executed_nodes
        # All nodes should be SUCCESS
        assert dag.nodes["a"].status == NodeStatus.SUCCESS
        assert dag.nodes["b"].status == NodeStatus.SUCCESS
        assert dag.nodes["c"].status == NodeStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_partial_completion(self, tmp_path):
        """Only some nodes checkpointed — rest execute normally."""
        engine = _make_engine(tmp_path)

        # Pre-populate: "a" and "b" completed, "c" still pending
        engine._persist_node_completion("a", {"status": "completed"})
        engine._persist_node_completion("b", {"status": "completed"})

        executed_nodes = []

        async def tracking_executor(node, artifacts, **kwargs):
            executed_nodes.append(node.id)
            return {"status": "completed", "summary": "done", "artifacts": []}

        engine2 = DAGExecutionEngine(
            tracking_executor,
            _noop_failure_handler,
            session_id="test-session",
            checkpoint_dir=str(tmp_path / "dag_progress"),
        )

        dag = _make_three_node_dag()
        await engine2.execute(dag)

        assert executed_nodes == ["c"]
        assert dag.nodes["a"].status == NodeStatus.SUCCESS
        assert dag.nodes["b"].status == NodeStatus.SUCCESS
        assert dag.nodes["c"].status == NodeStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_cleanup_after_full_completion(self, tmp_path):
        """Checkpoint file is removed after all nodes complete."""
        engine = _make_engine(tmp_path)

        # Pre-populate checkpoint
        engine._persist_node_completion("a", {"status": "completed"})

        checkpoint_path = engine._checkpoint_file()
        assert checkpoint_path.exists()

        async def tracking_executor(node, artifacts, **kwargs):
            return {"status": "completed", "summary": "done", "artifacts": []}

        engine2 = DAGExecutionEngine(
            tracking_executor,
            _noop_failure_handler,
            session_id="test-session",
            checkpoint_dir=str(tmp_path / "dag_progress"),
        )

        dag = _make_three_node_dag()
        await engine2.execute(dag)

        # Checkpoint should be cleaned up
        assert not checkpoint_path.exists()

    @pytest.mark.asyncio
    async def test_no_checkpoint_without_session(self, tmp_path):
        """No checkpoint file created when session_id is None."""
        executed_nodes = []

        async def tracking_executor(node, artifacts, **kwargs):
            executed_nodes.append(node.id)
            return {"status": "completed", "summary": "done", "artifacts": []}

        engine = DAGExecutionEngine(
            tracking_executor,
            _noop_failure_handler,
            session_id=None,
            checkpoint_dir=str(tmp_path / "dag_progress"),
        )

        dag = _make_three_node_dag()
        await engine.execute(dag)

        # All nodes executed normally
        assert executed_nodes == ["a", "b", "c"]

        # No checkpoint directory created (mkdir happens but no files)
        checkpoint_dir = tmp_path / "dag_progress"
        jsonl_files = list(checkpoint_dir.glob("*.jsonl"))
        assert len(jsonl_files) == 0

    @pytest.mark.asyncio
    async def test_checkpoint_preserved_on_failure(self, tmp_path):
        """Checkpoint NOT cleaned up when DAG fails (for crash recovery)."""
        engine = _make_engine(tmp_path)
        engine._persist_node_completion("a", {"status": "completed"})

        checkpoint_path = engine._checkpoint_file()
        assert checkpoint_path.exists()

        async def fail_executor(node, artifacts, **kwargs):
            if node.id == "b":
                raise RuntimeError("boom")
            return {"status": "completed", "summary": "done", "artifacts": []}

        engine2 = DAGExecutionEngine(
            fail_executor,
            _noop_failure_handler,
            session_id="test-session",
            checkpoint_dir=str(tmp_path / "dag_progress"),
        )

        dag = _make_three_node_dag()
        await engine2.execute(dag)

        # Checkpoint still exists because DAG didn't fully complete
        assert checkpoint_path.exists()

    @pytest.mark.asyncio
    async def test_unknown_checkpoint_nodes_ignored(self, tmp_path):
        """Checkpoint entries for nodes not in DAG are safely ignored."""
        engine = _make_engine(tmp_path)

        # Checkpoint includes a node that doesn't exist in the DAG
        engine._persist_node_completion("a", {"status": "completed"})
        engine._persist_node_completion("ghost_node", {"status": "completed"})

        executed_nodes = []

        async def tracking_executor(node, artifacts, **kwargs):
            executed_nodes.append(node.id)
            return {"status": "completed", "summary": "done", "artifacts": []}

        engine2 = DAGExecutionEngine(
            tracking_executor,
            _noop_failure_handler,
            session_id="test-session",
            checkpoint_dir=str(tmp_path / "dag_progress"),
        )

        dag = _make_three_node_dag()
        await engine2.execute(dag)

        assert "a" not in executed_nodes
        assert "b" in executed_nodes
        assert "c" in executed_nodes
