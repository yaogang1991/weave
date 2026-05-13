"""Test cross-node naming consistency for parallel generators (#265).

Verifies that planner and generator prompts include rules for preventing
naming mismatches between parallel nodes that share a library namespace.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
from agent.agent_pool import WorkerAgent


class TestPlannerNamingConvention:
    """Planner prompt must include rules for cross-node naming consistency."""

    def test_planner_mentions_naming_contract(self):
        prompt = IntelligentOrchestrator.PLANNING_PROMPT_TEMPLATE
        assert "NAMING CONTRACT" in prompt, (
            "Planner prompt must mention NAMING CONTRACT for parallel nodes"
        )

    def test_planner_mentions_serialize_preference(self):
        prompt = IntelligentOrchestrator.PLANNING_PROMPT_TEMPLATE
        assert "serialize" in prompt.lower(), (
            "Planner prompt must mention serialization as preferred option"
        )
        assert "edge from the source node to the test node" in prompt, (
            "Planner prompt must explain how to serialize source→test nodes"
        )

    def test_planner_mentions_cross_node_rule(self):
        prompt = IntelligentOrchestrator.PLANNING_PROMPT_TEMPLATE
        assert "Cross-node naming" in prompt or "cross-node" in prompt.lower(), (
            "Planner prompt must have a cross-node naming consistency rule"
        )

    def test_planner_gives_naming_example(self):
        prompt = IntelligentOrchestrator.PLANNING_PROMPT_TEMPLATE
        assert "TokenBucket" in prompt, (
            "Planner prompt must include a concrete naming example"
        )


class TestGeneratorNamingConvention:
    """Generator prompt must include rules for respecting naming contracts."""

    def test_generator_mentions_naming_contract(self):
        prompts = WorkerAgent.SYSTEM_PROMPTS
        gen_prompt = prompts.get("generator", "")
        assert "NAMING CONTRACT" in gen_prompt, (
            "Generator prompt must mention NAMING CONTRACT"
        )

    def test_generator_mentions_exact_names(self):
        prompts = WorkerAgent.SYSTEM_PROMPTS
        gen_prompt = prompts.get("generator", "")
        assert "exact" in gen_prompt.lower() and "names" in gen_prompt.lower(), (
            "Generator prompt must emphasize using exact specified names"
        )

    def test_generator_mentions_read_source_first(self):
        prompts = WorkerAgent.SYSTEM_PROMPTS
        gen_prompt = prompts.get("generator", "")
        assert "read the source" in gen_prompt.lower() or "FIRST read" in gen_prompt, (
            "Generator prompt must instruct reading source files before writing tests"
        )

    def test_generator_mentions_no_guessing(self):
        prompts = WorkerAgent.SYSTEM_PROMPTS
        gen_prompt = prompts.get("generator", "")
        assert "NEVER guess" in gen_prompt, (
            "Generator prompt must warn against guessing class names"
        )


class TestNamingConventionExample:
    """Verify the naming example in planner prompt is concrete and correct."""

    def test_example_includes_not_alternative(self):
        prompt = IntelligentOrchestrator.PLANNING_PROMPT_TEMPLATE
        assert "not TokenBucketLimiter" in prompt, (
            "Example should show the WRONG name to avoid, using the exact "
            "name from the #265 bug report"
        )
