"""
Tests for #145: evaluator deduplication via handoff of auto-eval results.

Verifies that auto-evaluation results are passed to downstream evaluator
agent nodes and that the evaluator prompt guides supplementary review.
"""
import pytest
from unittest.mock import MagicMock

from core.models import DAG, DAGNode, NodeStatus, HandoffArtifact


class TestAutoEvalResultStorage:
    """auto_eval_result is stored on DAGNode for downstream handoff."""

    def test_node_has_auto_eval_result_field(self):
        node = DAGNode(
            id="a",
            agent_type="generator",
            task_description="impl",
        )
        assert node.auto_eval_result is None

    def test_auto_eval_result_stores_dict(self):
        node = DAGNode(
            id="a",
            agent_type="generator",
            task_description="impl",
        )
        node.auto_eval_result = {"passed": True, "score": 10.0}
        assert node.auto_eval_result["passed"] is True


class TestAutoEvalHandoff:
    """Auto-eval results are passed as HandoffArtifact to evaluator nodes."""

    def test_eval_result_handoff_to_evaluator(self):
        """Downstream evaluator node receives auto-eval results."""
        from core.dag_engine import DAGExecutionEngine
        import asyncio

        dag = DAG(reasoning="test")
        gen_node = DAGNode(
            id="gen",
            agent_type="generator",
            task_description="impl",
            success_criteria=["tests pass"],
        )
        gen_node.status = NodeStatus.SUCCESS
        gen_node.result = {"summary": "done", "artifacts": ["main.py"]}
        gen_node.output_artifacts = ["main.py"]
        gen_node.auto_eval_result = {
            "passed": True,
            "score": 10.0,
            "criteria_results": {"tests pass": True},
            "feedback": "All good",
        }

        eval_node = DAGNode(
            id="eval",
            agent_type="evaluator",
            task_description="review",
        )

        dag.add_node(gen_node)
        dag.add_node(eval_node)
        dag.add_edge("gen", "eval")

        async def noop_executor(node, artifacts):
            return {"status": "completed", "summary": "ok", "artifacts": []}

        async def noop_failure_handler(dag, node_id, error):
            from core.models import FailureDecision
            return FailureDecision(action="abort", reasoning="test")

        engine = DAGExecutionEngine(noop_executor, noop_failure_handler)
        artifacts = engine._collect_input_artifacts(dag, "eval")

        # Should have 2 artifacts: generator output + auto-eval result
        assert len(artifacts) == 2
        eval_handoff = [a for a in artifacts if a.from_agent == "auto_evaluator"]
        assert len(eval_handoff) == 1
        assert eval_handoff[0].to_agent == "evaluator"
        assert eval_handoff[0].metadata["type"] == "evaluation_result"
        assert eval_handoff[0].metadata["passed"] is True
        assert "AUTOMATED EVALUATION RESULTS" in eval_handoff[0].content

    def test_no_handoff_to_non_evaluator(self):
        """Non-evaluator nodes don't receive auto-eval handoff."""
        from core.dag_engine import DAGExecutionEngine

        dag = DAG(reasoning="test")
        gen_node = DAGNode(
            id="gen",
            agent_type="generator",
            task_description="impl",
        )
        gen_node.status = NodeStatus.SUCCESS
        gen_node.result = {"summary": "done", "artifacts": ["main.py"]}
        gen_node.output_artifacts = ["main.py"]
        gen_node.auto_eval_result = {"passed": True, "score": 10.0}

        # Another generator, not an evaluator
        gen2_node = DAGNode(
            id="gen2",
            agent_type="generator",
            task_description="more impl",
        )

        dag.add_node(gen_node)
        dag.add_node(gen2_node)
        dag.add_edge("gen", "gen2")

        async def noop_executor(node, artifacts):
            return {"status": "completed", "summary": "ok", "artifacts": []}

        async def noop_failure_handler(dag, node_id, error):
            from core.models import FailureDecision
            return FailureDecision(action="abort", reasoning="test")

        engine = DAGExecutionEngine(noop_executor, noop_failure_handler)
        artifacts = engine._collect_input_artifacts(dag, "gen2")

        # Should only have generator output, NOT auto-eval result
        auto_eval_artifacts = [a for a in artifacts if a.from_agent == "auto_evaluator"]
        assert len(auto_eval_artifacts) == 0

    def test_no_handoff_when_no_auto_eval(self):
        """No auto-eval result → no handoff artifact."""
        from core.dag_engine import DAGExecutionEngine

        dag = DAG(reasoning="test")
        gen_node = DAGNode(
            id="gen",
            agent_type="generator",
            task_description="impl",
        )
        gen_node.status = NodeStatus.SUCCESS
        gen_node.result = {"summary": "done", "artifacts": ["main.py"]}
        gen_node.output_artifacts = ["main.py"]
        gen_node.auto_eval_result = None  # No auto-eval

        eval_node = DAGNode(
            id="eval",
            agent_type="evaluator",
            task_description="review",
        )

        dag.add_node(gen_node)
        dag.add_node(eval_node)
        dag.add_edge("gen", "eval")

        async def noop_executor(node, artifacts):
            return {"status": "completed", "summary": "ok", "artifacts": []}

        async def noop_failure_handler(dag, node_id, error):
            from core.models import FailureDecision
            return FailureDecision(action="abort", reasoning="test")

        engine = DAGExecutionEngine(noop_executor, noop_failure_handler)
        artifacts = engine._collect_input_artifacts(dag, "eval")

        auto_eval_artifacts = [a for a in artifacts if a.from_agent == "auto_evaluator"]
        assert len(auto_eval_artifacts) == 0


class TestEvaluatorPrompt:
    """Evaluator agent prompt includes supplementary review guidance."""

    def test_prompt_mentions_automated_results(self):
        from agent.agent_pool import WorkerAgent
        prompt = WorkerAgent.SYSTEM_PROMPTS.get("evaluator", "")
        assert "AUTOMATED EVALUATION RESULTS" in prompt

    def test_prompt_guides_supplementary_review(self):
        from agent.agent_pool import WorkerAgent
        prompt = WorkerAgent.SYSTEM_PROMPTS.get("evaluator", "")
        assert "architecture" in prompt.lower() or "Architecture" in prompt
        # Should mention NOT blindly re-running
        assert "do not" in prompt.lower() or "Do NOT" in prompt


class TestAutoEvalResultAlignment:
    """auto_eval_result must align with final retained artifacts/best attempt."""

    def test_no_handoff_when_eval_result_not_passed(self):
        """auto_eval_result with passed=False is NOT handed to evaluator."""
        from core.dag_engine import DAGExecutionEngine

        dag = DAG(reasoning="test")
        gen_node = DAGNode(
            id="gen",
            agent_type="generator",
            task_description="impl",
        )
        gen_node.status = NodeStatus.SUCCESS
        gen_node.result = {"summary": "done", "artifacts": ["main.py"]}
        gen_node.output_artifacts = ["main.py"]
        gen_node.auto_eval_result = {
            "passed": False,
            "score": 5.0,
            "feedback": "lint issues",
        }

        eval_node = DAGNode(
            id="eval",
            agent_type="evaluator",
            task_description="review",
        )

        dag.add_node(gen_node)
        dag.add_node(eval_node)
        dag.add_edge("gen", "eval")

        async def noop_executor(node, artifacts):
            return {"status": "completed", "summary": "ok", "artifacts": []}

        async def noop_failure_handler(dag, node_id, error):
            from core.models import FailureDecision
            return FailureDecision(action="abort", reasoning="test")

        engine = DAGExecutionEngine(noop_executor, noop_failure_handler)
        artifacts = engine._collect_input_artifacts(dag, "eval")

        auto_eval_artifacts = [
            a for a in artifacts if a.from_agent == "auto_evaluator"
        ]
        assert len(auto_eval_artifacts) == 0, (
            "Should NOT pass auto_eval_result when passed=False"
        )

    def test_threshold_pass_with_warnings_handoff(self):
        """auto_eval_result passed=True via threshold but with WARN criteria.

        When pass_threshold is used and some soft criteria fail but score
        meets threshold, the handoff must indicate warnings exist so the
        downstream evaluator doesn't assume everything is clean (#202, #145).
        """
        from core.dag_engine import DAGExecutionEngine

        dag = DAG(reasoning="test")
        gen_node = DAGNode(
            id="gen",
            agent_type="generator",
            task_description="impl",
            success_criteria=["tests pass", "lint clean"],
        )
        gen_node.status = NodeStatus.SUCCESS
        gen_node.result = {"summary": "done", "artifacts": ["main.py"]}
        gen_node.output_artifacts = ["main.py"]
        # Threshold-assisted pass: passed=True but lint failed
        gen_node.auto_eval_result = {
            "passed": True,
            "score": 8.0,
            "criteria_results": {"tests pass": True, "lint clean": False},
            "feedback": "WARN lint clean: 2 style issues found",
        }

        eval_node = DAGNode(
            id="eval",
            agent_type="evaluator",
            task_description="review",
        )

        dag.add_node(gen_node)
        dag.add_node(eval_node)
        dag.add_edge("gen", "eval")

        async def noop_executor(node, artifacts):
            return {"status": "completed", "summary": "ok", "artifacts": []}

        async def noop_failure_handler(dag, node_id, error):
            from core.models import FailureDecision
            return FailureDecision(action="abort", reasoning="test")

        engine = DAGExecutionEngine(noop_executor, noop_failure_handler)
        artifacts = engine._collect_input_artifacts(dag, "eval")

        eval_handoff = [a for a in artifacts if a.from_agent == "auto_evaluator"]
        assert len(eval_handoff) == 1
        # Metadata must flag warnings
        assert eval_handoff[0].metadata["has_warnings"] is True
        # Content must indicate threshold-assisted pass, not "already verified"
        assert "WARNINGS" in eval_handoff[0].content
        assert "threshold" in eval_handoff[0].content.lower()

    def test_clean_pass_no_warnings(self):
        """Clean pass (all criteria True) has has_warnings=False."""
        from core.dag_engine import DAGExecutionEngine

        dag = DAG(reasoning="test")
        gen_node = DAGNode(
            id="gen",
            agent_type="generator",
            task_description="impl",
            success_criteria=["tests pass"],
        )
        gen_node.status = NodeStatus.SUCCESS
        gen_node.result = {"summary": "done", "artifacts": ["main.py"]}
        gen_node.output_artifacts = ["main.py"]
        gen_node.auto_eval_result = {
            "passed": True,
            "score": 10.0,
            "criteria_results": {"tests pass": True},
            "feedback": "All good",
        }

        eval_node = DAGNode(
            id="eval",
            agent_type="evaluator",
            task_description="review",
        )

        dag.add_node(gen_node)
        dag.add_node(eval_node)
        dag.add_edge("gen", "eval")

        async def noop_executor(node, artifacts):
            return {"status": "completed", "summary": "ok", "artifacts": []}

        async def noop_failure_handler(dag, node_id, error):
            from core.models import FailureDecision
            return FailureDecision(action="abort", reasoning="test")

        engine = DAGExecutionEngine(noop_executor, noop_failure_handler)
        artifacts = engine._collect_input_artifacts(dag, "eval")

        eval_handoff = [a for a in artifacts if a.from_agent == "auto_evaluator"]
        assert len(eval_handoff) == 1
        assert eval_handoff[0].metadata["has_warnings"] is False
        # Clean pass should use "already verified" header
        assert "already verified" in eval_handoff[0].content

    def test_regression_restore_updates_auto_eval_to_best(self):
        """After regression, auto_eval_result reflects the best attempt.

        Simulates: gen node succeeds with score 8.0, then retries and
        regresses to score 3.0. The auto_eval_result should be updated to
        the best attempt's result (score 8.0).
        """
        from core.dag_engine import DAGExecutionEngine

        dag = DAG(reasoning="test")
        gen_node = DAGNode(
            id="gen",
            agent_type="generator",
            task_description="impl",
            success_criteria=["tests pass"],
            max_retries=3,
        )
        # Node ultimately succeeds after regression restore
        gen_node.status = NodeStatus.SUCCESS
        gen_node.result = {"summary": "done", "artifacts": ["main.py"]}
        gen_node.output_artifacts = ["main.py"]
        # After regression, auto_eval_result should reflect best attempt
        gen_node.auto_eval_result = {
            "passed": True,
            "score": 8.0,
            "criteria_results": {"tests pass": True},
            "feedback": "Best attempt feedback",
            "_note": "Updated to best-attempt result (regression detected)",
        }

        eval_node = DAGNode(
            id="eval",
            agent_type="evaluator",
            task_description="review",
        )

        dag.add_node(gen_node)
        dag.add_node(eval_node)
        dag.add_edge("gen", "eval")

        async def noop_executor(node, artifacts):
            return {"status": "completed", "summary": "ok", "artifacts": []}

        async def noop_failure_handler(dag, node_id, error):
            from core.models import FailureDecision
            return FailureDecision(action="abort", reasoning="test")

        engine = DAGExecutionEngine(noop_executor, noop_failure_handler)
        artifacts = engine._collect_input_artifacts(dag, "eval")

        eval_handoff = [a for a in artifacts if a.from_agent == "auto_evaluator"]
        assert len(eval_handoff) == 1
        assert eval_handoff[0].metadata["score"] == 8.0
        assert eval_handoff[0].metadata["passed"] is True

    def test_failed_node_no_auto_eval_leak(self):
        """Failed nodes must not leak auto_eval_result to downstream.

        When a generator node exhausts retries and fails, its
        auto_eval_result should be cleared so _collect_input_artifacts
        (which checks dep_node.status == SUCCESS) would never pass it,
        but also as a belt-and-suspenders measure.
        """
        from core.dag_engine import DAGExecutionEngine

        dag = DAG(reasoning="test")
        gen_node = DAGNode(
            id="gen",
            agent_type="generator",
            task_description="impl",
            max_retries=1,
        )
        gen_node.status = NodeStatus.FAILED
        gen_node.result = {"summary": "failed", "artifacts": ["main.py"]}
        gen_node.output_artifacts = ["main.py"]
        # On terminal failure, auto_eval_result should have been cleared
        gen_node.auto_eval_result = None

        eval_node = DAGNode(
            id="eval",
            agent_type="evaluator",
            task_description="review",
        )

        dag.add_node(gen_node)
        dag.add_node(eval_node)
        dag.add_edge("gen", "eval")

        async def noop_executor(node, artifacts):
            return {"status": "completed", "summary": "ok", "artifacts": []}

        async def noop_failure_handler(dag, node_id, error):
            from core.models import FailureDecision
            return FailureDecision(action="abort", reasoning="test")

        engine = DAGExecutionEngine(noop_executor, noop_failure_handler)
        artifacts = engine._collect_input_artifacts(dag, "eval")

        # Failed dep node -> no artifacts at all (not even regular ones)
        assert len(artifacts) == 0, (
            "Failed dep node should produce no handoff artifacts"
        )


class TestRegressionRestoreEvalResult:
    """Full integration-style test for regression restore + eval alignment.

    After a successful best attempt followed by a regression attempt that
    gets restored, the downstream evaluator receives the best attempt's
    eval result, not the regression attempt's.
    """

    def test_best_attempt_eval_preserved_after_regression(self):
        """Simulates the full regression restore flow within _execute_single_node.

        Scenario:
        1. First attempt: score 6.0, passed=False -> tracked as best
        2. Second attempt: score 3.0, passed=False -> regression detected
           auto_eval_result updated to best attempt (score 6.0)
        3. Node exhausts retries (max_retries=2) -> auto_eval_result cleared
        """
        from core.dag_engine import DAGExecutionEngine
        from unittest.mock import MagicMock
        import asyncio

        def make_eval_result(passed, score, feedback=""):
            result = MagicMock()
            result.passed = passed
            result.score = score
            result.feedback = feedback
            result.metadata = {}
            result.model_dump.return_value = {
                "passed": passed,
                "score": score,
                "feedback": feedback,
                "criteria_results": {},
            }
            return result

        # First attempt fails (score 6.0), second regresses (score 3.0)
        eval_results = [
            make_eval_result(False, 6.0, "First attempt - not great"),
            make_eval_result(False, 3.0, "Second attempt - regression"),
        ]

        mock_evaluator = MagicMock()
        mock_evaluator.evaluate_stage = MagicMock(side_effect=eval_results)

        executor_call_count = 0

        async def counting_executor(node, artifacts):
            nonlocal executor_call_count
            executor_call_count += 1
            return {
                "status": "completed",
                "summary": f"attempt {executor_call_count}",
                "artifacts": [f"file_v{executor_call_count}.py"],
            }

        async def retry_failure_handler(dag, node_id, error):
            from core.models import FailureDecision
            return FailureDecision(action="retry", reasoning="retry")

        dag = DAG(reasoning="test")
        gen_node = DAGNode(
            id="gen",
            agent_type="generator",
            task_description="impl",
            success_criteria=["tests pass"],
            max_retries=2,
        )
        eval_node = DAGNode(
            id="eval",
            agent_type="evaluator",
            task_description="review",
        )
        dag.add_node(gen_node)
        dag.add_node(eval_node)
        dag.add_edge("gen", "eval")

        engine = DAGExecutionEngine(
            counting_executor,
            retry_failure_handler,
            evaluator=mock_evaluator,
            work_dir="/tmp/test_workdir",
        )

        result_dag = asyncio.run(engine.execute(dag))

        gen_result = result_dag.nodes["gen"]
        # Node should have failed after exhausting retries
        assert gen_result.status == NodeStatus.FAILED
        # auto_eval_result should be cleared on terminal failure
        assert gen_result.auto_eval_result is None, (
            "auto_eval_result must be cleared when node ultimately fails"
        )

        # The evaluator node should be skipped (dependency failed)
        eval_result_node = result_dag.nodes["eval"]
        assert eval_result_node.status == NodeStatus.SKIPPED

    def test_regression_then_success_preserves_best_eval(self):
        """Best attempt eval preserved when node ultimately succeeds after regression.

        Scenario:
        1. First attempt: score 6.0, passed=False
        2. Second attempt: score 8.0, passed=True -> node succeeds
        3. auto_eval_result reflects the successful 8.0 attempt
        4. Downstream evaluator receives the 8.0 result
        """
        from core.dag_engine import DAGExecutionEngine
        from unittest.mock import MagicMock
        import asyncio

        def make_eval_result(passed, score, feedback=""):
            result = MagicMock()
            result.passed = passed
            result.score = score
            result.feedback = feedback
            result.metadata = {}
            result.model_dump.return_value = {
                "passed": passed,
                "score": score,
                "feedback": feedback,
                "criteria_results": {},
            }
            return result

        # First eval fails, second succeeds
        eval_results = [
            make_eval_result(False, 6.0, "First attempt - not great"),
            make_eval_result(True, 8.0, "Second attempt - good"),
        ]

        mock_evaluator = MagicMock()
        mock_evaluator.evaluate_stage = MagicMock(side_effect=eval_results)

        executor_call_count = 0

        async def counting_executor(node, artifacts):
            nonlocal executor_call_count
            executor_call_count += 1
            return {
                "status": "completed",
                "summary": f"attempt {executor_call_count}",
                "artifacts": [f"file_v{executor_call_count}.py"],
            }

        async def retry_failure_handler(dag, node_id, error):
            from core.models import FailureDecision
            return FailureDecision(action="retry", reasoning="retry")

        dag = DAG(reasoning="test")
        gen_node = DAGNode(
            id="gen",
            agent_type="generator",
            task_description="impl",
            success_criteria=["tests pass"],
            max_retries=3,
        )
        eval_node = DAGNode(
            id="eval",
            agent_type="evaluator",
            task_description="review",
        )
        dag.add_node(gen_node)
        dag.add_node(eval_node)
        dag.add_edge("gen", "eval")

        engine = DAGExecutionEngine(
            counting_executor,
            retry_failure_handler,
            evaluator=mock_evaluator,
            work_dir="/tmp/test_workdir",
        )

        result_dag = asyncio.run(engine.execute(dag))

        gen_result = result_dag.nodes["gen"]
        assert gen_result.status == NodeStatus.SUCCESS
        # auto_eval_result should reflect the successful 8.0 eval
        assert gen_result.auto_eval_result is not None
        assert gen_result.auto_eval_result["passed"] is True
        assert gen_result.auto_eval_result["score"] == 8.0

        # Now verify downstream evaluator receives correct artifacts
        artifacts = engine._collect_input_artifacts(result_dag, "eval")
        eval_handoff = [
            a for a in artifacts if a.from_agent == "auto_evaluator"
        ]
        assert len(eval_handoff) == 1
        assert eval_handoff[0].metadata["score"] == 8.0
        assert eval_handoff[0].metadata["passed"] is True
