"""
Tests for #212: restore best-attempt artifacts when retry produces regression.

When a retry scores worse than the best attempt, the engine restores the
best attempt's file contents and artifact list instead of using the worse
result.
"""
import pytest
from unittest.mock import MagicMock

from core.dag_engine import DAGExecutionEngine
from core.retry_policy import RetryPolicyEngine


class TestFileSnapshot:
    """_capture_file_snapshot and _restore_file_snapshot unit tests."""

    def test_capture_and_restore(self, tmp_path):
        """Capture files, modify them, then restore from snapshot."""
        work_dir = str(tmp_path)
        (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("y = 2\n", encoding="utf-8")

        snapshot = RetryPolicyEngine.capture_file_snapshot(
            work_dir, ["a.py", "b.py"],
        )
        assert snapshot == {"a.py": "x = 1\n", "b.py": "y = 2\n"}

        # Modify files
        (tmp_path / "a.py").write_text("BROKEN\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("ALSO BROKEN\n", encoding="utf-8")

        # Restore
        RetryPolicyEngine.restore_file_snapshot(work_dir, snapshot)
        assert (tmp_path / "a.py").read_text() == "x = 1\n"
        assert (tmp_path / "b.py").read_text() == "y = 2\n"

    def test_capture_ignores_missing_files(self, tmp_path):
        """Missing artifacts are silently skipped."""
        snapshot = RetryPolicyEngine.capture_file_snapshot(
            str(tmp_path), ["nonexistent.py"],
        )
        assert snapshot == {}

    def test_restore_creates_parent_dirs(self, tmp_path):
        """Restore creates intermediate directories if needed."""
        work_dir = str(tmp_path)
        snapshot = {"sub/deep/file.py": "content"}
        RetryPolicyEngine.restore_file_snapshot(work_dir, snapshot)
        assert (tmp_path / "sub" / "deep" / "file.py").read_text() == "content"

    def test_capture_empty_artifacts(self, tmp_path):
        """Empty artifact list produces empty snapshot."""
        snapshot = RetryPolicyEngine.capture_file_snapshot(
            str(tmp_path), [],
        )
        assert snapshot == {}

    def test_capture_ignores_read_errors(self, tmp_path):
        """Permission errors on read are silently skipped."""
        # Create a file and make it unreadable
        f = tmp_path / "secret.py"
        f.write_text("data", encoding="utf-8")
        f.chmod(0o000)
        try:
            snapshot = RetryPolicyEngine.capture_file_snapshot(
                str(tmp_path), ["secret.py"],
            )
            # Should not crash, may be empty or contain the file
            # depending on OS permissions
            assert isinstance(snapshot, dict)
        finally:
            f.chmod(0o644)


class TestRegressionRestore:
    """Integration test: regression in execute_node restores files."""

    @pytest.mark.asyncio
    async def test_regression_restores_best_files(self, tmp_path):
        """When retry produces regression, best attempt files are restored."""
        from unittest.mock import AsyncMock

        work_dir = str(tmp_path)

        # First attempt: write good files
        (tmp_path / "main.py").write_text("def hello(): return 'hello'\n", encoding="utf-8")
        (tmp_path / "test_main.py").write_text("from main import hello\n", encoding="utf-8")

        mock_evaluator = MagicMock()
        # First eval: score 6.7 (not passed)
        mock_evaluator.evaluate_stage.return_value = MagicMock(
            passed=False, score=6.7,
            feedback="Files missing",
            metadata={"lint_new_issues": [], "lint_all_issues": []},
        )

        async def mock_executor(node, artifacts, **kwargs):
            return {"artifacts": ["main.py", "test_main.py"]}

        engine = DAGExecutionEngine(
            agent_executor=mock_executor,
            failure_handler=AsyncMock(),
            work_dir=work_dir,
            evaluator=mock_evaluator,
        )

        from core.models import DAG, DAGNode, NodeStatus

        dag = DAG(reasoning="test")
        node = DAGNode(
            id="gen_1",
            agent_type="generator",
            task_description="test",
            status=NodeStatus.PENDING,
            success_criteria=["tests pass"],
        )
        dag.add_node(node)

        await engine._node_executor.execute_node(dag, "gen_1")

        # After first attempt: files should be good, best attempt captured
        # With immutable nodes, use dag.nodes to get updated state (#486)
        assert dag.nodes["gen_1"].status == NodeStatus.FAILED
        best = engine._best_attempts.get("gen_1")
        assert best is not None
        assert best["score"] == 6.7
        assert "file_snapshot" in best
        assert "main.py" in best["file_snapshot"]
        assert best["file_snapshot"]["main.py"] == "def hello(): return 'hello'\n"

        # Now corrupt the files (simulating a bad retry)
        (tmp_path / "main.py").write_text("BROKEN CODE!!!\n", encoding="utf-8")

        # Second eval: score 3.3 (worse — regression)
        mock_evaluator.evaluate_stage.return_value = MagicMock(
            passed=False, score=3.3,
            feedback="Code is broken",
            metadata={"lint_new_issues": ["E999"], "lint_all_issues": ["E999"]},
        )

        # Reset node for retry using immutable update (#486)
        dag.update_node("gen_1", status=NodeStatus.RETRYING, error="", retry_count=0)

        await engine._node_executor.execute_node(dag, "gen_1")

        # After regression: files should be restored to best attempt
        assert dag.nodes["gen_1"].status == NodeStatus.FAILED  # Still failed but...
        # Files restored from snapshot
        assert (tmp_path / "main.py").read_text() == "def hello(): return 'hello'\n"
        # output_artifacts restored to best
        assert "main.py" in dag.nodes["gen_1"].output_artifacts

    @pytest.mark.asyncio
    async def test_regression_removes_extra_files(self, tmp_path):
        """When retry adds extra files and regresses, those files are deleted."""
        from unittest.mock import AsyncMock

        work_dir = str(tmp_path)

        # First attempt: write good files
        (tmp_path / "main.py").write_text("def hello(): return 'hello'\n", encoding="utf-8")
        (tmp_path / "test_main.py").write_text("from main import hello\n", encoding="utf-8")

        mock_evaluator = MagicMock()
        # First eval: score 6.7 (not passed)
        mock_evaluator.evaluate_stage.return_value = MagicMock(
            passed=False, score=6.7,
            feedback="Files missing",
            metadata={"lint_new_issues": [], "lint_all_issues": []},
        )

        async def mock_executor(node, artifacts, **kwargs):
            return {"artifacts": ["main.py", "test_main.py"]}

        engine = DAGExecutionEngine(
            agent_executor=mock_executor,
            failure_handler=AsyncMock(),
            work_dir=work_dir,
            evaluator=mock_evaluator,
        )

        from core.models import DAG, DAGNode, NodeStatus

        dag = DAG(reasoning="test")
        node = DAGNode(
            id="gen_1",
            agent_type="generator",
            task_description="test",
            status=NodeStatus.PENDING,
            success_criteria=["tests pass"],
        )
        dag.add_node(node)

        await engine._node_executor.execute_node(dag, "gen_1")

        # Verify best attempt captured with artifact_set
        best = engine._best_attempts.get("gen_1")
        assert best is not None
        assert "artifact_set" in best
        assert "main.py" in best["artifact_set"]
        assert "test_main.py" in best["artifact_set"]

        # Now simulate a bad retry that corrupts main.py AND adds an extra file
        (tmp_path / "main.py").write_text("BROKEN CODE!!!\n", encoding="utf-8")
        (tmp_path / "extra_module.py").write_text("# unwanted file\n", encoding="utf-8")

        # Override executor to return artifacts including the extra file
        async def mock_executor_extra(node, artifacts, **kwargs):
            return {"artifacts": ["main.py", "test_main.py", "extra_module.py"]}

        engine.agent_executor = mock_executor_extra

        # Second eval: score 3.3 (worse — regression)
        mock_evaluator.evaluate_stage.return_value = MagicMock(
            passed=False, score=3.3,
            feedback="Code is broken",
            metadata={"lint_new_issues": ["E999"], "lint_all_issues": ["E999"]},
        )

        # Reset node for retry using immutable update (#486)
        dag.update_node("gen_1", status=NodeStatus.RETRYING, error="", retry_count=0)

        await engine._node_executor.execute_node(dag, "gen_1")

        # After regression restore:
        # 1. main.py should be restored to best content
        assert (tmp_path / "main.py").read_text() == "def hello(): return 'hello'\n"
        # 2. extra_module.py should be DELETED (not in best artifact set)
        assert not (tmp_path / "extra_module.py").exists(), (
            "extra_module.py should have been deleted during regression restore"
        )
        # 3. output_artifacts should be restored to best (without extra_module.py)
        updated_node = dag.nodes["gen_1"]
        assert "extra_module.py" not in (updated_node.output_artifacts or [])
        assert "main.py" in updated_node.output_artifacts
