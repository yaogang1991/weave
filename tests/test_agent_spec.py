"""Tests for AgentSpec unified model and its 5-core sub-models."""
import pytest

from core.agent_spec import (
    AgentSpec,
    AgentDependency,
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
    TerminationConditions,
)
from core.eval_models import SuccessCriterion


class TestContractSpec:
    def test_defaults(self):
        spec = ContractSpec()
        assert spec.input_schema == {}
        assert spec.output_schema == {}
        assert spec.success_criteria == []

    def test_with_criteria(self):
        spec = ContractSpec(
            input_schema={"type": "string", "description": "User requirement"},
            output_schema={"type": "object", "properties": {"plan": {"type": "object"}}},
            success_criteria=[SuccessCriterion(type="file_exists", path="src/main.py")],
        )
        assert spec.success_criteria[0].type.value == "file_exists"

    def test_serialization_roundtrip(self):
        spec = ContractSpec(
            input_schema={"type": "string"},
            success_criteria=[SuccessCriterion(description="test")],
        )
        data = spec.model_dump()
        restored = ContractSpec.model_validate(data)
        assert restored == spec


class TestBrainSpec:
    def test_defaults(self):
        spec = BrainSpec()
        assert spec.quality_tier == QualityTier.BALANCED
        assert spec.model_id is None
        assert spec.temperature == 0.7
        assert spec.max_tokens == 4096
        assert spec.system_prompt == ""

    def test_quality_tier_values(self):
        assert QualityTier.FAST.value == "fast"
        assert QualityTier.BALANCED.value == "balanced"
        assert QualityTier.HIGH.value == "high"
        assert QualityTier.PREMIUM.value == "premium"

    def test_model_id_override(self):
        spec = BrainSpec(model_id="claude-opus-4-8")
        assert spec.model_id == "claude-opus-4-8"

    def test_serialization_roundtrip(self):
        spec = BrainSpec(quality_tier=QualityTier.PREMIUM, temperature=0.5)
        data = spec.model_dump()
        restored = BrainSpec.model_validate(data)
        assert restored == spec


class TestCapabilitySpec:
    def test_defaults(self):
        spec = CapabilitySpec()
        assert spec.skills == []
        assert spec.tools == []
        assert spec.dependencies == []
        assert spec.constraints == []

    def test_with_dependencies(self):
        dep = AgentDependency(agent_name="planner", purpose="Gets task plan")
        spec = CapabilitySpec(
            skills=["code_writing"],
            tools=["read", "write", "bash"],
            dependencies=[dep],
            constraints=["Must follow coding standards"],
        )
        assert spec.dependencies[0].agent_name == "planner"
        assert spec.dependencies[0].fallback is None

    def test_dependency_with_fallback(self):
        dep = AgentDependency(agent_name="evaluator", purpose="Quality check", fallback="self_eval")
        assert dep.fallback == "self_eval"


class TestBoundarySpec:
    def test_defaults(self):
        spec = BoundarySpec()
        assert spec.timeout is None
        assert spec.stall_timeout is None
        assert spec.resource_budget is None
        assert spec.termination is None
        assert spec.max_retries == 3

    def test_with_budget(self):
        budget = ResourceBudget(max_tokens=100000, max_cost_usd=1.0, max_iterations=10)
        spec = BoundarySpec(timeout=600, resource_budget=budget)
        assert spec.resource_budget.max_tokens == 100000
        assert spec.resource_budget.max_cost_usd == 1.0

    def test_termination_conditions(self):
        tc = TerminationConditions(convergence_threshold=0.95, convergence_rounds=3)
        spec = BoundarySpec(termination=tc)
        assert spec.termination.convergence_threshold == 0.95

    def test_serialization_roundtrip(self):
        spec = BoundarySpec(
            timeout=300,
            stall_timeout=120,
            resource_budget=ResourceBudget(max_tokens=50000),
            max_retries=5,
        )
        data = spec.model_dump()
        restored = BoundarySpec.model_validate(data)
        assert restored == spec


class TestLifecycleSpec:
    def test_defaults(self):
        spec = LifecycleSpec()
        assert spec.memory_enabled is True
        assert spec.memory_scope is None
        assert spec.error_policy is None

    def test_with_error_policy(self):
        policy = ErrorPolicy(
            strategy=ErrorStrategy.RETRY_WITH_BACKOFF,
            max_retries=3,
            backoff_base=2.0,
            backoff_cap=60.0,
        )
        spec = LifecycleSpec(error_policy=policy)
        assert spec.error_policy.strategy == ErrorStrategy.RETRY_WITH_BACKOFF

    def test_error_strategy_values(self):
        assert ErrorStrategy.FAIL_FAST.value == "fail_fast"
        assert ErrorStrategy.REPLAN.value == "replan"
        assert ErrorStrategy.CIRCUIT_BREAKER.value == "circuit_breaker"
        assert ErrorStrategy.FALLBACK_AGENT.value == "fallback_agent"

    def test_error_policy_with_fallback_agent(self):
        policy = ErrorPolicy(strategy=ErrorStrategy.FALLBACK_AGENT, fallback_agent="simple_generator")
        assert policy.fallback_agent == "simple_generator"


class TestAgentSpec:
    def test_minimal_creation(self):
        spec = AgentSpec(name="test_agent")
        assert spec.name == "test_agent"
        assert spec.version == "1.0.0"
        assert isinstance(spec.contract, ContractSpec)
        assert isinstance(spec.brain, BrainSpec)
        assert isinstance(spec.capability, CapabilitySpec)
        assert isinstance(spec.boundary, BoundarySpec)
        assert isinstance(spec.lifecycle, LifecycleSpec)

    def test_full_creation(self):
        spec = AgentSpec(
            name="generator",
            version="2.0.0",
            description="Generates code from specifications",
            contract=ContractSpec(
                input_schema={"type": "object", "description": "Task spec"},
                output_schema={"type": "object", "description": "Code artifacts"},
            ),
            brain=BrainSpec(quality_tier=QualityTier.BALANCED, system_prompt="You are a code generator."),
            capability=CapabilitySpec(
                skills=["code_writing", "test_writing"],
                tools=["read", "write", "edit", "bash"],
                dependencies=[AgentDependency(agent_name="planner", purpose="Gets plan")],
            ),
            boundary=BoundarySpec(timeout=600, max_retries=3),
            lifecycle=LifecycleSpec(
                memory_enabled=True,
                error_policy=ErrorPolicy(strategy=ErrorStrategy.RETRY_WITH_BACKOFF),
            ),
        )
        assert spec.name == "generator"
        assert spec.capability.skills == ["code_writing", "test_writing"]
        assert spec.boundary.timeout == 600
        assert spec.lifecycle.error_policy.strategy == ErrorStrategy.RETRY_WITH_BACKOFF

    def test_serialization_roundtrip(self):
        spec = AgentSpec(
            name="planner",
            brain=BrainSpec(quality_tier=QualityTier.HIGH),
            boundary=BoundarySpec(timeout=300),
        )
        data = spec.model_dump()
        restored = AgentSpec.model_validate(data)
        assert restored == spec

    def test_json_serialization(self):
        spec = AgentSpec(name="test", brain=BrainSpec(quality_tier=QualityTier.FAST))
        json_str = spec.model_dump_json()
        restored = AgentSpec.model_validate_json(json_str)
        assert restored == spec

    def test_to_capability_backward_compat(self):
        """AgentSpec can produce backward-compatible AgentCapability."""
        from core.dag_models import AgentCapability

        spec = AgentSpec(
            name="planner",
            description="Plans tasks",
            contract=ContractSpec(
                input_schema={"description": "user_requirements"},
                output_schema={"description": "plan"},
            ),
            brain=BrainSpec(system_prompt="You plan things."),
            capability=CapabilitySpec(
                skills=["planning"],
                constraints=["No code writing"],
            ),
        )
        cap = spec.to_capability()
        assert isinstance(cap, AgentCapability)
        assert cap.id == "planner"
        assert cap.name == "planner"
        assert "planning" in cap.skills
        assert "No code writing" in cap.constraints
        assert cap.system_prompt == "You plan things."

    def test_from_capability_backward_compat(self):
        """AgentSpec can be created from legacy AgentCapability."""
        from core.dag_models import AgentCapability

        cap = AgentCapability(
            id="reviewer",
            name="Reviewer",
            description="Reviews code",
            skills=["code_review"],
            input_schema=["code"],
            output_schema=["review_report"],
            constraints=["Read-only"],
            system_prompt="You review code.",
        )
        spec = AgentSpec.from_capability(cap)
        assert spec.name == "reviewer"
        assert "code_review" in spec.capability.skills
        assert spec.capability.constraints == ["Read-only"]
        assert spec.brain.system_prompt == "You review code."

    def test_from_capability_type_error(self):
        """from_capability raises TypeError for non-AgentCapability."""
        with pytest.raises(TypeError, match="Expected AgentCapability"):
            AgentSpec.from_capability("not a capability")


class TestInvocationOverrides:
    def test_defaults(self):
        overrides = InvocationOverrides()
        assert overrides.success_criteria is None
        assert overrides.budget is None
        assert overrides.timeout is None

    def test_with_values(self):
        overrides = InvocationOverrides(
            budget=ResourceBudget(max_tokens=50000),
            timeout=120,
        )
        assert overrides.budget.max_tokens == 50000
        assert overrides.timeout == 120

    def test_tighten_validation(self):
        """Overrides with timeout=0 should be valid (tighten to immediate)."""
        overrides = InvocationOverrides(timeout=0)
        assert overrides.timeout == 0


class TestAgentRegistrySpecIntegration:
    """Verify AgentRegistry supports AgentSpec registration and lookup."""

    def test_register_spec(self):
        from core.agent_registry import AgentRegistry
        registry = AgentRegistry()
        spec = AgentSpec(
            name="reviewer",
            description="Reviews code quality",
            brain=BrainSpec(quality_tier=QualityTier.BALANCED),
            capability=CapabilitySpec(skills=["code_review"]),
        )
        registry.register_spec(spec)
        assert registry.has_agent("reviewer")

    def test_get_spec(self):
        from core.agent_registry import AgentRegistry
        registry = AgentRegistry()
        spec = registry.get_spec("planner")
        assert spec is not None
        assert isinstance(spec, AgentSpec)
        assert spec.name == "planner"
        assert "planning" in spec.capability.skills

    def test_get_returns_backward_compat_capability(self):
        from core.agent_registry import AgentRegistry
        from core.dag_models import AgentCapability
        registry = AgentRegistry()
        result = registry.get("planner")
        assert isinstance(result, AgentCapability)
        assert result.id == "planner"

    def test_list_agents_returns_capabilities(self):
        from core.agent_registry import AgentRegistry
        registry = AgentRegistry()
        caps = registry.list_agents()
        assert len(caps) >= 3
        ids = [c.id for c in caps]
        assert "planner" in ids
        assert "generator" in ids
        assert "evaluator" in ids

    def test_list_specs(self):
        from core.agent_registry import AgentRegistry
        registry = AgentRegistry()
        specs = registry.list_specs()
        assert len(specs) >= 3
        names = [s.name for s in specs]
        assert "planner" in names

    def test_default_agents_are_full_specs(self):
        from core.agent_registry import AgentRegistry
        registry = AgentRegistry()
        for name in ("planner", "generator", "evaluator"):
            spec = registry.get_spec(name)
            assert spec is not None
            assert spec.contract.input_schema, f"{name} missing input_schema"
            assert spec.capability.skills, f"{name} missing skills"
            assert spec.boundary.timeout is not None, f"{name} missing timeout"

    def test_to_prompt_description_unchanged(self):
        from core.agent_registry import AgentRegistry
        registry = AgentRegistry()
        desc = registry.to_prompt_description()
        assert "planner" in desc
        assert "generator" in desc
        assert "evaluator" in desc
        assert "requirement_analysis" in desc

    def test_register_spec_overwrite(self):
        from core.agent_registry import AgentRegistry
        registry = AgentRegistry()
        spec = AgentSpec(name="planner", description="Custom planner")
        registry.register_spec(spec)
        result = registry.get_spec("planner")
        assert result.description == "Custom planner"

    def test_load_from_yaml_new_format(self, tmp_path):
        from core.agent_registry import AgentRegistry
        yaml_content = (
            "agents:\n"
            "  - name: custom_agent\n"
            "    description: 'A custom agent'\n"
            "    brain:\n"
            "      quality_tier: fast\n"
            "      system_prompt: 'Custom prompt'\n"
            "    capability:\n"
            "      skills: ['custom_skill']\n"
            "      tools: ['read']\n"
            "    boundary:\n"
            "      timeout: 120\n"
            "      max_retries: 2\n"
            "    lifecycle:\n"
            "      memory_enabled: false\n"
        )
        yaml_file = tmp_path / "agents.yaml"
        yaml_file.write_text(yaml_content)
        registry = AgentRegistry()
        registry.load_from_yaml(yaml_file)
        spec = registry.get_spec("custom_agent")
        assert spec is not None
        assert spec.brain.quality_tier == QualityTier.FAST
        assert spec.boundary.timeout == 120
        assert spec.lifecycle.memory_enabled is False

    def test_load_from_yaml_legacy_format(self, tmp_path):
        from core.agent_registry import AgentRegistry
        yaml_content = (
            "agents:\n"
            "  - id: legacy_agent\n"
            "    name: Legacy Agent\n"
            "    description: 'Old format'\n"
            "    skills: ['old_skill']\n"
        )
        yaml_file = tmp_path / "legacy.yaml"
        yaml_file.write_text(yaml_content)
        registry = AgentRegistry()
        registry.load_from_yaml(yaml_file)
        spec = registry.get_spec("legacy_agent")
        assert spec is not None
        assert "old_skill" in spec.capability.skills
