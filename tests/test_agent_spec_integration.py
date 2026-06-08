"""Integration tests: YAML → AgentSpec → Registry → execution config chain."""
import asyncio

import pytest

from core.agent_spec import (
    AgentDependency,
    AgentSpec,
    BoundarySpec,
    BrainSpec,
    CapabilitySpec,
    ContractSpec,
    ErrorPolicy,
    ErrorStrategy,
    InvocationOverrides,
    LifecycleSpec,
    QualityTier,
    ResourceBudget,
)
from core.agent_registry import AgentRegistry


class TestFullIntegrationChain:
    """End-to-end: load YAML → register → query → resolve config."""

    def test_yaml_to_spec_to_registry(self, tmp_path):
        """Load a 5-core YAML, register it, verify full config chain."""
        yaml_content = """
agents:
  - name: integration_test_agent
    description: "Integration test agent"
    contract:
      input_schema:
        task: {description: "Task description"}
      output_schema:
        result: {description: "Task result"}
      success_criteria:
        - type: file_exists
          path: "output.txt"
    brain:
      quality_tier: balanced
      temperature: 0.5
      max_tokens: 8000
      system_prompt: "You are a test agent."
    capability:
      skills: ["testing"]
      tools: ["read", "write"]
      constraints: ["Must be fast"]
    boundary:
      timeout: 120
      max_retries: 2
      resource_budget:
        max_tokens: 50000
        max_cost_usd: 0.25
    lifecycle:
      memory_enabled: false
      error_policy:
        strategy: fail_fast
        max_retries: 1
"""
        yaml_file = tmp_path / "test_agents.yaml"
        yaml_file.write_text(yaml_content)

        registry = AgentRegistry()
        registry.load_from_yaml(yaml_file)

        spec = registry.get_spec("integration_test_agent")
        assert spec is not None
        assert spec.description == "Integration test agent"
        assert spec.brain.quality_tier == QualityTier.BALANCED
        assert spec.brain.temperature == 0.5
        assert spec.boundary.timeout == 120
        assert spec.boundary.resource_budget.max_tokens == 50000
        assert spec.lifecycle.error_policy.strategy == ErrorStrategy.FAIL_FAST

        cap = registry.get("integration_test_agent")
        assert cap is not None
        assert cap.id == "integration_test_agent"

        desc = registry.to_prompt_description()
        assert "integration_test_agent" in desc

    def test_default_agents_full_chain(self):
        """Verify default agents have complete 5-core config."""
        registry = AgentRegistry()

        for name in ("planner", "generator", "evaluator"):
            spec = registry.get_spec(name)
            assert spec is not None, f"{name} not found in registry"
            assert spec.description, f"{name} missing description"
            assert spec.capability.skills, f"{name} missing skills"
            assert spec.boundary.timeout is not None, f"{name} missing timeout"
            assert spec.lifecycle.error_policy is not None, f"{name} missing error_policy"

    def test_mixed_format_yaml_loading(self, tmp_path):
        """Verify both legacy and new-format agents can coexist."""
        yaml_content = """
agents:
  - id: legacy_agent
    name: Legacy Agent
    description: "Legacy format"
    skills: ["old_skill"]
  - name: new_agent
    description: "New format"
    brain:
      quality_tier: fast
"""
        yaml_file = tmp_path / "mixed.yaml"
        yaml_file.write_text(yaml_content)

        registry = AgentRegistry()
        registry.load_from_yaml(yaml_file)

        legacy = registry.get_spec("legacy_agent")
        assert legacy is not None
        assert "old_skill" in legacy.capability.skills

        new = registry.get_spec("new_agent")
        assert new is not None
        assert new.brain.quality_tier == QualityTier.FAST

    def test_spec_to_executor_config_chain(self):
        """Verify AgentSpec boundary config can feed into executor logic."""
        registry = AgentRegistry()
        spec = registry.get_spec("generator")

        assert spec.boundary.timeout == 600
        assert spec.boundary.max_retries == 3
        assert spec.lifecycle.error_policy is not None
        assert spec.lifecycle.error_policy.strategy == ErrorStrategy.RETRY_WITH_BACKOFF

    def test_invoke_chain(self):
        """Verify invoke() with overrides resolves correctly."""
        registry = AgentRegistry()
        result = asyncio.run(registry.invoke(
            "generator",
            "Write a function",
            overrides=InvocationOverrides(timeout=300),
        ))
        assert result.boundary.timeout == 300  # tightened from 600

    def test_planner_boundary_config(self):
        """Verify planner has REPLAN error strategy."""
        registry = AgentRegistry()
        spec = registry.get_spec("planner")
        assert spec.boundary.timeout == 300
        assert spec.boundary.max_retries == 2
        assert spec.lifecycle.error_policy.strategy == ErrorStrategy.REPLAN

    def test_evaluator_boundary_config(self):
        """Verify evaluator has FAIL_FAST error strategy."""
        registry = AgentRegistry()
        spec = registry.get_spec("evaluator")
        assert spec.boundary.timeout == 480
        assert spec.lifecycle.error_policy.strategy == ErrorStrategy.FAIL_FAST

    def test_dependency_validation_in_plan(self, tmp_path):
        """Verify plan_validator checks dependencies via AgentSpec."""
        from orchestrator.plan_validator import PlanValidator

        registry = AgentRegistry()
        registry.register_spec(AgentSpec(
            name="custom",
            capability=CapabilitySpec(
                dependencies=[
                    AgentDependency(agent_name="nonexistent", purpose="Missing dep"),
                ],
            ),
        ))

        plan = {
            "nodes": [
                {"id": "n1", "agent_type": "custom", "task_description": "test"},
            ],
            "edges": [],
        }

        validator = PlanValidator()
        validator.validate(plan, agent_registry=registry)
        warnings = validator.warnings
        assert any("nonexistent" in w for w in warnings), \
            f"Expected dependency warning, got: {warnings}"

    def test_capability_contract_consistency_warning(self):
        """Verify consistency check warns when node has criteria but agent doesn't."""
        from orchestrator.plan_validator import PlanValidator

        registry = AgentRegistry()
        registry.register_spec(AgentSpec(name="bare_agent"))

        plan = {
            "nodes": [
                {
                    "id": "n1",
                    "agent_type": "bare_agent",
                    "task_description": "test",
                    "success_criteria": [{"type": "file_exists", "path": "x.py"}],
                },
            ],
            "edges": [],
        }

        validator = PlanValidator()
        validator.validate(plan, agent_registry=registry)
        warnings = validator.warnings
        assert any("ContractSpec defines no default criteria" in w for w in warnings), \
            f"Expected consistency warning, got: {warnings}"
