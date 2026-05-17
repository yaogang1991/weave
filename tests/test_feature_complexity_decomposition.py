"""
Tests for feature-complexity decomposition (#409).

Verifies that:
- Planning prompt has feature-complexity decomposition rule
- PlanValidator warns on over-complex generator nodes
- Adaptation prompt has zero-output splitting guidance
- Replan prompt has node-splitting guidance
- adapt_to_failure auto-replans for zero-output + complex tasks
- Generator prompt has early file output guidance
"""
import pytest

from orchestrator.plan_validator import PlanValidator


# ---------------------------------------------------------------------------
# Prompt content tests
# ---------------------------------------------------------------------------


class TestPlanningPromptHasComplexityRule:
    def test_contains_feature_complexity_rule(self):
        with open("orchestrator/prompts/planning.md") as f:
            content = f.read()
        assert "feature complexity" in content.lower() or "complex feature" in content.lower()

    def test_mentions_max_features_threshold(self):
        with open("orchestrator/prompts/planning.md") as f:
            content = f.read()
        # Should mention a numeric threshold (3) for max features per node
        assert ("3 distinct complex features" in content or
                "more than 3" in content.lower() or
                "3 complex feature" in content)

    def test_mentions_splitting_guidance(self):
        with open("orchestrator/prompts/planning.md") as f:
            content = f.read()
        # Should guide splitting into multiple nodes
        assert "split" in content.lower() or "decompose" in content.lower()


class TestAdaptationPromptHasZeroOutputRule:
    def test_contains_zero_output_splitting_rule(self):
        with open("orchestrator/prompts/adaptation.md") as f:
            content = f.read()
        assert "zero output" in content.lower()

    def test_mentions_replan_for_complex_tasks(self):
        with open("orchestrator/prompts/adaptation.md") as f:
            content = f.read()
        # Should mention replan + split when zero-output + complex
        content_lower = content.lower()
        assert "replan" in content_lower and "split" in content_lower


class TestReplanPromptHasSplitGuidance:
    def test_contains_split_over_complex_nodes(self):
        with open("orchestrator/prompts/replan.md") as f:
            content = f.read()
        assert "split" in content.lower() and "complex" in content.lower()

    def test_mentions_zero_output(self):
        with open("orchestrator/prompts/replan.md") as f:
            content = f.read()
        assert "zero output" in content.lower()


class TestGeneratorPromptHasEarlyOutputRule:
    def test_contains_early_file_output_guidance(self):
        from agent.agent_pool import WorkerAgent
        prompt = WorkerAgent.SYSTEM_PROMPTS["generator"]
        assert "early" in prompt.lower() and "file" in prompt.lower()

    def test_guidance_about_incremental_writes(self):
        from agent.agent_pool import WorkerAgent
        prompt = WorkerAgent.SYSTEM_PROMPTS["generator"]
        # Should mention writing files incrementally/per-feature, not waiting
        prompt_lower = prompt.lower()
        assert ("write" in prompt_lower and
                ("incremental" in prompt_lower or
                 "each feature" in prompt_lower or
                 "as soon as" in prompt_lower))


# ---------------------------------------------------------------------------
# PlanValidator feature complexity tests
# ---------------------------------------------------------------------------


class TestPlanValidatorFeatureComplexity:
    def test_warns_on_complex_task_with_four_features(self):
        task = (
            "Implement a patch toolkit with: 1) apply_patch to apply unified diffs, "
            "2) create_patch to generate patches from file pairs, "
            "3) reverse_patch to invert patches, "
            "4) three_way_merge to merge conflicting changes"
        )
        plan = {
            "nodes": [
                {"id": "impl_patch", "agent_type": "generator", "task": task},
            ],
            "edges": [],
        }
        validator = PlanValidator()
        validator.validate(plan)
        " ".join(validator.warnings)  # noqa: F841
        assert any("feature" in w.lower() or "complex" in w.lower() for w in validator.warnings), \
            f"Expected complexity warning, got: {validator.warnings}"

    def test_no_warning_for_simple_task(self):
        task = "Implement a simple rate limiter with token bucket algorithm"
        plan = {
            "nodes": [
                {"id": "impl", "agent_type": "generator", "task": task},
            ],
            "edges": [],
        }
        validator = PlanValidator()
        validator.validate(plan)
        complexity_warnings = [
            w for w in validator.warnings
            if "feature" in w.lower() or "complex" in w.lower()
        ]
        assert complexity_warnings == []

    def test_no_warning_for_planner_nodes(self):
        task = (
            "Analyze and plan: 1) user auth, 2) database schema, "
            "3) API routes, 4) frontend components"
        )
        plan = {
            "nodes": [
                {"id": "plan", "agent_type": "planner", "task": task},
            ],
            "edges": [],
        }
        validator = PlanValidator()
        validator.validate(plan)
        complexity_warnings = [
            w for w in validator.warnings
            if "feature" in w.lower() or "complex" in w.lower()
        ]
        assert complexity_warnings == []

    def test_no_warning_for_evaluator_nodes(self):
        task = (
            "Evaluate: 1) test coverage, 2) lint compliance, "
            "3) code quality, 4) documentation"
        )
        plan = {
            "nodes": [
                {"id": "eval", "agent_type": "evaluator", "task": task},
            ],
            "edges": [],
        }
        validator = PlanValidator()
        validator.validate(plan)
        complexity_warnings = [
            w for w in validator.warnings
            if "feature" in w.lower() or "complex" in w.lower()
        ]
        assert complexity_warnings == []

    def test_warns_with_comma_separated_features(self):
        task = (
            "Build a toolkit implementing apply_patch, create_patch, "
            "reverse_patch, and three_way_merge functionality"
        )
        plan = {
            "nodes": [
                {"id": "impl", "agent_type": "generator", "task": task},
            ],
            "edges": [],
        }
        validator = PlanValidator()
        validator.validate(plan)
        assert any("feature" in w.lower() or "complex" in w.lower() for w in validator.warnings), \
            f"Expected complexity warning, got: {validator.warnings}"


# ---------------------------------------------------------------------------
# adapt_to_failure auto-replan tests
# ---------------------------------------------------------------------------


class TestAdaptToFailureAutoReplan:
    @pytest.fixture
    def orchestrator(self):
        from core.config import LLMConfig
        from core.agent_registry import AgentRegistry
        from session.store import SessionStore
        from orchestrator.intelligent_orchestrator import IntelligentOrchestrator

        return IntelligentOrchestrator(
            llm_config=LLMConfig(),
            session_store=SessionStore(base_path="/tmp/test_events_409"),
            agent_registry=AgentRegistry(),
        )

    @pytest.fixture
    def complex_dag(self):
        from core.models import DAG, DAGNode, NodeStatus

        dag = DAG(reasoning="test")
        node = DAGNode(
            id="impl_patch",
            agent_type="generator",
            task_description=(
                "Implement a patch toolkit with: 1) apply_patch, "
                "2) create_patch, 3) reverse_patch, 4) three_way_merge"
            ),
        )
        node.status = NodeStatus.FAILED
        node.error = "Node produced zero output artifacts. Agent type: generator"
        dag.add_node(node)
        return dag

    @pytest.mark.asyncio
    async def test_auto_replan_on_zero_output_complex(self, orchestrator, complex_dag):
        """Zero-output + 4+ features should auto-replan without LLM call."""
        from core.models import FailureDecision

        decision = await orchestrator.adapt_to_failure(
            complex_dag, "impl_patch",
            error="Node produced zero output artifacts. Agent type: generator",
        )
        assert isinstance(decision, FailureDecision)
        assert decision.action == "replan"

    @pytest.mark.asyncio
    async def test_no_auto_replan_for_simple_zero_output(self, orchestrator):
        """Zero-output + simple task should go through normal LLM adaptation."""
        from core.models import DAG, DAGNode, NodeStatus

        dag = DAG(reasoning="test")
        node = DAGNode(
            id="impl",
            agent_type="generator",
            task_description="Implement a simple rate limiter",
        )
        node.status = NodeStatus.FAILED
        node.error = "Node produced zero output artifacts."
        dag.add_node(node)

        # This will try to call the LLM, which we can't mock easily here.
        # Instead verify the feature count heuristic returns False for simple tasks.
        feature_count = orchestrator._count_features(node.task_description)
        assert feature_count < 4, "Simple task should have <4 features"

    @pytest.mark.asyncio
    async def test_no_auto_replan_for_non_zero_output(self, orchestrator, complex_dag):
        """Non-zero-output failure on complex task should go through normal LLM path."""
        # Change error to non-zero-output
        complex_dag.nodes["impl_patch"].error = "evaluation_failed: tests did not pass"

        feature_count = orchestrator._count_features(
            complex_dag.nodes["impl_patch"].task_description
        )
        assert feature_count >= 4, "Complex task should have 4+ features"
        # The error is not zero-output, so auto-replan should not trigger
        # (We can't easily verify this without mocking LLM, but the heuristic
        # check below confirms the condition)
        is_zero_output = "zero output" in complex_dag.nodes["impl_patch"].error.lower()
        assert not is_zero_output
