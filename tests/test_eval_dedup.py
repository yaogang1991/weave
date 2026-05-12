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
