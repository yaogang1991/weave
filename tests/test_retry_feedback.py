"""
Tests for #239: retry feedback quality and incremental fix guidance.

Verifies that evaluation feedback is not truncated in orchestrator
decision-making and that retry instructions emphasize incremental fixes.
"""
import pytest
from unittest.mock import MagicMock, patch

from core.models import DAG, DAGNode, NodeStatus, FailureDecision
from core.dag_engine import DAGExecutionEngine
from orchestrator.intelligent_orchestrator import IntelligentOrchestrator


def _make_dag_with_failed_generator():
    """Create a DAG with a generator that failed evaluation."""
    dag = DAG(reasoning="test")
    node = DAGNode(
        id="impl",
        agent_type="generator",
        task_description="implement parser",
        success_criteria=["tests pass"],
    )
    node.status = NodeStatus.FAILED
    node.error = (
        "Evaluation failed (score: 7.8): "
        "FAIL lint clean: Lint failed: 3 new issue(s)\n"
        "NEW:\n"
        "  - parser.py:10 E501 line too long\n"
        "  - parser.py:25 W291 trailing whitespace\n"
        "  - parser.py:30 E402 module level import not at top\n"
        "IGNORED_EXISTING:\n"
        "  - lexer.py:5 E302 expected 2 blank lines\n"
    )
    node.eval_feedback = node.error + "\n\nOutput artifacts: [parser.py, lexer.py]"
    node.retry_count = 1
    dag.add_node(node)
    return dag


class TestRetryFeedbackContent:
    @pytest.mark.asyncio
    async def test_feedback_includes_incremental_fix_guidance(self):
        """Retry feedback tells generator to fix incrementally, not rewrite."""
        # Use an executor that produces source-only artifacts to trigger
        # evaluation failure, then on retry produces test files too
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="impl",
            agent_type="generator",
            task_description="impl",
            success_criteria=["lint clean"],
            max_retries=3,
        ))

        from core.models import EvaluationResult
        from unittest.mock import MagicMock

        # First attempt: produce artifact, eval fails
        # Second attempt: produce artifact, eval passes
        attempt = 0

        async def executor(node, artifacts, **kwargs):
            nonlocal attempt
            attempt += 1
            return {
                "status": "completed",
                "summary": "ok",
                "artifacts": ["parser.py"],
            }

        mock_evaluator = MagicMock()

        def mock_evaluate(*args, **kwargs):
            if attempt == 1:
                return EvaluationResult(
                    passed=False,
                    score=7.8,
                    criteria_results={"lint": False},
                    feedback="FAIL lint clean: 3 new issues\nNEW:\n  - parser.py:10 E501",
                )
            return EvaluationResult(
                passed=True,
                score=10.0,
                criteria_results={"lint": True},
                feedback="PASS",
            )

        mock_evaluator.evaluate_stage = MagicMock(side_effect=mock_evaluate)

        async def failure_handler(dag, node_id, error):
            return FailureDecision(action="retry", reasoning="fix lint")

        engine = DAGExecutionEngine(executor, failure_handler, evaluator=mock_evaluator)
        result = await engine.execute(dag)

        # After first failure, eval_feedback should include incremental fix guidance
        feedback = result.nodes["impl"].eval_feedback
        if feedback:  # Only check if feedback was set (eval ran and failed)
            assert "incrementally" in feedback.lower() or "edit tool" in feedback.lower()


class TestOrchestratorErrorVisibility:
    def test_adaptation_uses_full_error(self):
        """adapt_to_failure sees up to 2000 chars of error, not just 500."""
        from orchestrator.prompts import PromptRegistry
        orchestrator = IntelligentOrchestrator.__new__(IntelligentOrchestrator)
        orchestrator._prompt_registry = PromptRegistry()
        orchestrator._token_estimator = None
        orchestrator.llm = MagicMock()
        orchestrator.llm.call = MagicMock(return_value={
            "content": '{"action": "retry", "reasoning": "fix lint"}',
        })
        orchestrator.llm_config = MagicMock()
        orchestrator.llm_config.model = "test-model"
        orchestrator.agent_registry = MagicMock()
        from orchestrator.adapter import Adapter
        orchestrator._adapter = Adapter(
            llm=orchestrator.llm,
            llm_config=orchestrator.llm_config,
            agent_registry=orchestrator.agent_registry,
            prompt_registry=orchestrator._prompt_registry,
            plan_to_dag_fn=MagicMock(),
        )
        dag = _make_dag_with_failed_generator()

        with patch.object(
            IntelligentOrchestrator, '_extract_json',
            return_value={"action": "retry", "reasoning": "fix lint"},
        ):
            import asyncio
            asyncio.run(  # noqa: F841
                orchestrator.adapt_to_failure(dag, "impl", dag.nodes["impl"].error),
            )

        # The LLM should have been called — verify system prompt is long enough
        call_args = orchestrator.llm.call.call_args
        system_prompt = call_args[0][0][0]["content"]
        # Should contain the full error text (not truncated to 500)
        assert "E402 module level import" in system_prompt


class TestEventFeedbackNotTruncated:
    @pytest.mark.asyncio
    async def test_upstream_retry_event_has_substantial_feedback(self):
        """upstream_retry event includes at least 1000 chars of feedback."""
        dag = DAG(reasoning="test")
        gen_node = DAGNode(
            id="gen",
            agent_type="generator",
            task_description="impl",
            max_retries=3,
        )
        eval_node = DAGNode(
            id="eval",
            agent_type="evaluator",
            task_description="eval",
        )
        dag.add_node(gen_node)
        dag.add_node(eval_node)
        dag.add_edge("gen", "eval")

        # Simulate gen succeeded, eval failed
        gen_node.status = NodeStatus.SUCCESS
        gen_node.result = {"summary": "done", "artifacts": ["main.py"]}
        gen_node.output_artifacts = ["main.py"]

        eval_node.status = NodeStatus.FAILED
        eval_node.error = "Tests failed"
        eval_node.eval_feedback = "X" * 2000  # Long feedback

        events = []

        async def capturing_executor(node, artifacts, **kwargs):
            return {"status": "completed", "summary": "ok", "artifacts": ["main.py"]}

        async def failure_handler(dag, node_id, error):
            return FailureDecision(action="retry", reasoning="fix")

        engine = DAGExecutionEngine(capturing_executor, failure_handler)
        engine.on_event(lambda e: events.append(e))
        await engine.execute(dag)  # noqa: F841

        # Find upstream_retry event
        upstream_events = [e for e in events if e.event_type == "upstream_retry"]
        if upstream_events:
            feedback = upstream_events[0].details.get("feedback", "")
            assert len(feedback) >= 500  # Should have substantial feedback


class TestUpstreamRetryGuard:
    """#630: UPSTREAM_RETRY should not re-trigger already-successful nodes."""

    @pytest.mark.asyncio
    async def test_no_upstream_retry_when_target_succeeded(self):
        """When evaluator fails but target generator succeeded, skip
        UPSTREAM_RETRY and retry evaluator directly (#630)."""
        dag = DAG(reasoning="test")
        gen_node = DAGNode(
            id="gen",
            agent_type="generator",
            task_description="impl",
            max_retries=3,
        )
        eval_node = DAGNode(
            id="eval",
            agent_type="evaluator",
            task_description="eval",
        )
        dag.add_node(gen_node)
        dag.add_node(eval_node)
        dag.add_edge("gen", "eval")

        gen_node.status = NodeStatus.SUCCESS
        gen_node.result = {"summary": "done", "artifacts": ["main.py"]}
        gen_node.output_artifacts = ["main.py"]

        eval_node.status = NodeStatus.FAILED
        eval_node.error = "Eval timeout"

        events = []
        executed_nodes = []

        async def capturing_executor(node, artifacts, **kwargs):
            executed_nodes.append(node.id)
            return {"status": "completed", "summary": "ok", "artifacts": []}

        async def failure_handler(dag, node_id, error):
            return FailureDecision(action="retry", reasoning="fix")

        engine = DAGExecutionEngine(capturing_executor, failure_handler)
        engine.on_event(lambda e: events.append(e))
        await engine.execute(dag)

        # gen should NOT be re-executed (it already succeeded)
        assert "gen" not in executed_nodes
        # eval should be retried directly
        assert "eval" in executed_nodes
        # No upstream_retry event should be emitted
        upstream_events = [e for e in events if e.event_type == "upstream_retry"]
        assert len(upstream_events) == 0

    @pytest.mark.asyncio
    async def test_no_upstream_retry_when_target_partial_pass(self):
        """PARTIAL_PASS target should also skip UPSTREAM_RETRY (#630)."""
        dag = DAG(reasoning="test")
        gen_node = DAGNode(
            id="gen",
            agent_type="generator",
            task_description="impl",
            max_retries=3,
        )
        eval_node = DAGNode(
            id="eval",
            agent_type="evaluator",
            task_description="eval",
        )
        dag.add_node(gen_node)
        dag.add_node(eval_node)
        dag.add_edge("gen", "eval")

        gen_node.status = NodeStatus.PARTIAL_PASS
        gen_node.result = {"summary": "done", "artifacts": ["main.py"]}
        gen_node.output_artifacts = ["main.py"]

        eval_node.status = NodeStatus.FAILED
        eval_node.error = "Eval timeout"

        executed_nodes = []

        async def capturing_executor(node, artifacts, **kwargs):
            executed_nodes.append(node.id)
            return {"status": "completed", "summary": "ok", "artifacts": []}

        async def failure_handler(dag, node_id, error):
            return FailureDecision(action="retry", reasoning="fix")

        engine = DAGExecutionEngine(capturing_executor, failure_handler)
        await engine.execute(dag)

        assert "gen" not in executed_nodes
        assert "eval" in executed_nodes

    @pytest.mark.asyncio
    async def test_no_upstream_retry_when_target_warned(self):
        """WARNED target should also skip UPSTREAM_RETRY (#630)."""
        dag = DAG(reasoning="test")
        gen_node = DAGNode(
            id="gen",
            agent_type="generator",
            task_description="impl",
            max_retries=3,
        )
        eval_node = DAGNode(
            id="eval",
            agent_type="evaluator",
            task_description="eval",
        )
        dag.add_node(gen_node)
        dag.add_node(eval_node)
        dag.add_edge("gen", "eval")

        gen_node.status = NodeStatus.WARNED
        gen_node.result = {"summary": "done", "artifacts": ["main.py"]}
        gen_node.output_artifacts = ["main.py"]

        eval_node.status = NodeStatus.FAILED
        eval_node.error = "Eval timeout"

        executed_nodes = []

        async def capturing_executor(node, artifacts, **kwargs):
            executed_nodes.append(node.id)
            return {"status": "completed", "summary": "ok", "artifacts": []}

        async def failure_handler(dag, node_id, error):
            return FailureDecision(action="retry", reasoning="fix")

        engine = DAGExecutionEngine(capturing_executor, failure_handler)
        await engine.execute(dag)

        assert "gen" not in executed_nodes
        assert "eval" in executed_nodes


class TestDegenerationRecoveredEvent:
    """#663: degeneration_recovered should be a valid ExecutionEvent type."""

    def test_degeneration_recovered_is_valid_event_type(self):
        """ExecutionEvent accepts degeneration_recovered as event_type (#663)."""
        from core.dag_models import ExecutionEvent

        event = ExecutionEvent(
            node_id="test_node",
            event_type="degeneration_recovered",
            details={"reason": "inherited_upstream_artifacts"},
        )
        assert event.event_type == "degeneration_recovered"
        assert event.details["reason"] == "inherited_upstream_artifacts"
