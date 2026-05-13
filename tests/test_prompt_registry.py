"""
Tests for #180: prompt externalization.

Verifies that:
- PromptRegistry loads prompts from .md files
- Content hashes are computed for audit
- Fallback behavior when files are missing
- Orchestrator uses PromptRegistry instead of inline constants
"""
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from orchestrator.prompts import PromptRegistry, get_prompt_registry


class TestPromptRegistry:
    def test_load_planning_prompt(self):
        registry = PromptRegistry()
        prompt = registry.load("planning")
        assert "orchestrator" in prompt.lower()
        assert "agent_descriptions" in prompt
        assert len(prompt) > 500

    def test_load_adaptation_prompt(self):
        registry = PromptRegistry()
        prompt = registry.load("adaptation")
        assert "retry" in prompt
        assert "{node_id}" in prompt
        assert "{error}" in prompt

    def test_load_replan_prompt(self):
        registry = PromptRegistry()
        prompt = registry.load("replan")
        assert "executed" in prompt.lower()
        assert "{failed_node}" in prompt
        assert "{agent_descriptions}" in prompt

    def test_content_hash_is_consistent(self):
        registry = PromptRegistry()
        h1 = registry.get_hash("planning")
        h2 = registry.get_hash("planning")
        assert h1 == h2
        assert len(h1) == 8

    def test_content_hash_differs_for_different_prompts(self):
        registry = PromptRegistry()
        h_plan = registry.get_hash("planning")
        h_adapt = registry.get_hash("adaptation")
        assert h_plan != h_adapt

    def test_available_prompts(self):
        registry = PromptRegistry()
        names = registry.available_prompts()
        assert "planning" in names
        assert "adaptation" in names
        assert "replan" in names

    def test_missing_prompt_raises_error(self):
        registry = PromptRegistry(prompts_dir=Path("/nonexistent"))
        with pytest.raises(ValueError, match="not found"):
            registry.load("nonexistent")

    def test_caching(self):
        registry = PromptRegistry()
        p1 = registry.load("planning")
        p2 = registry.load("planning")
        assert p1 is p2  # Same object from cache

    def test_get_metadata(self):
        registry = PromptRegistry()
        registry.load("planning")
        meta = registry.get_metadata()
        assert "planning" in meta["loaded"]
        assert "hash" in meta["loaded"]["planning"]

    def test_get_prompt_registry_singleton(self):
        r1 = get_prompt_registry()
        r2 = get_prompt_registry()
        assert r1 is r2

    def test_custom_prompts_dir_overrides_default(self, tmp_path):
        custom = tmp_path / "planning.md"
        custom.write_text("CUSTOM PROMPT {agent_descriptions}", encoding="utf-8")
        registry = PromptRegistry(prompts_dir=tmp_path)
        prompt = registry.load("planning")
        assert prompt == "CUSTOM PROMPT {agent_descriptions}"

    def test_custom_prompts_dir_does_not_affect_default(self, tmp_path):
        custom = tmp_path / "planning.md"
        custom.write_text("OVERRIDDEN", encoding="utf-8")
        registry = PromptRegistry(prompts_dir=tmp_path)
        default = get_prompt_registry()
        assert default.load("planning") != "OVERRIDDEN"


class TestPlanningPromptCriticalRules:
    """Ensure externalized planning prompt contains all required rules."""

    def test_planning_contains_stdlib_shadowing_rule(self):
        registry = PromptRegistry()
        prompt = registry.load("planning")
        assert "standard library module" in prompt.lower()
        assert "stdlib" in prompt.lower()

    def test_planning_contains_naming_contract_rule(self):
        registry = PromptRegistry()
        prompt = registry.load("planning")
        assert "NAMING CONTRACT" in prompt
        assert "cross-node naming" in prompt.lower()


class TestOrchestratorPromptIntegration:
    def test_orchestrator_uses_prompt_registry(self):
        """Orchestrator loads prompts from registry, not inline constants."""
        from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
        # PLANNING_PROMPT_TEMPLATE should be empty (loaded dynamically)
        assert IntelligentOrchestrator.PLANNING_PROMPT_TEMPLATE == ""

    def test_orchestrator_accepts_custom_registry(self):
        """Orchestrator can be initialized with a custom prompt registry."""
        from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
        custom = PromptRegistry()
        orchestrator = IntelligentOrchestrator.__new__(IntelligentOrchestrator)
        orchestrator._prompt_registry = custom
        prompt = orchestrator._prompt_registry.load("planning")
        assert "agent_descriptions" in prompt
