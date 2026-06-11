# M7.4 — AgentSpec Unified Model Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Introduce a unified 5-core AgentSpec model that replaces the scattered agent_type-based configuration across 7+ files with a single "one object describes one agent" model.

**Architecture:** Create `core/agent_spec.py` with 5 orthogonal sub-models (Contract/Brain/Capability/Boundary/Lifecycle), migrate `AgentCapability` to `AgentSpec`, then wire it into `AgentRegistry`, `NodeExecutor`, `LLMRouter`, `EvaluationPipeline`, and `plan_validator`.

**Tech Stack:** Python 3.11+, Pydantic BaseModel, PyYAML, pytest

---

## Phase 1: Create AgentSpec Model

### Task 1: Create `core/agent_spec.py` with all sub-models

**Files:**
- Create: `core/agent_spec.py`
- Test: `tests/test_agent_spec.py`

**Step 1: Write failing tests for all sub-models**

Create `tests/test_agent_spec.py`:

```python
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
        import json
        spec = AgentSpec(name="test", brain=BrainSpec(quality_tier=QualityTier.FAST))
        json_str = spec.model_dump_json()
        restored = AgentSpec.model_validate_json(json_str)
        assert restored == spec

    def test_to_capability_backward_compat(self):
        """AgentSpec can produce backward-compatible AgentCapability."""
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
```

**Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_agent_spec.py -v --tb=short
```

Expected: FAIL — `ModuleNotFoundError: No module named 'core.agent_spec'`

**Step 3: Create `core/agent_spec.py`**

```python
"""
AgentSpec — Unified 5-core agent description model.

Replaces the scattered agent_type-based configuration with a single
"one object describes one agent" model. Each core is orthogonal:
  - Contract:  what goes in, what comes out, how success is measured
  - Brain:     which model, what parameters, what system prompt
  - Capability: what tools, what skills, what dependencies
  - Boundary:  how much budget, how long, how many retries
  - Lifecycle: what to remember, how to handle errors

Design principles:
  - Each core exposes Configuration (knobs), hides Mechanism (gears)
  - Caller interface: 1 required (task) + 3 optional tighten (success/budget/timeout)
  - Harness is orchestration-layer, NOT part of AgentSpec
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from core.eval_models import SuccessCriterion


# ---------------------------------------------------------------------------
# Contract Spec — what goes in, what comes out
# ---------------------------------------------------------------------------

class ContractSpec(BaseModel):
    """Agent input/output contract and success criteria."""
    input_schema: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema describing expected input",
    )
    output_schema: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema describing expected output",
    )
    success_criteria: list[SuccessCriterion] = Field(
        default_factory=list,
        description="Criteria for evaluating agent output",
    )


# ---------------------------------------------------------------------------
# Brain Spec — which model, what configuration
# ---------------------------------------------------------------------------

class QualityTier(str, Enum):
    """Model quality tier — caller selects tier, not specific model."""
    FAST = "fast"           # haiku-class — lightweight, fast
    BALANCED = "balanced"   # sonnet-class — good balance
    HIGH = "high"           # sonnet-class with higher parameters
    PREMIUM = "premium"     # opus-class — highest quality


class BrainSpec(BaseModel):
    """Agent reasoning configuration."""
    quality_tier: QualityTier = QualityTier.BALANCED
    model_id: str | None = Field(
        default=None,
        description="Direct model override (None = route by quality_tier)",
    )
    temperature: float = 0.7
    max_tokens: int = 4096
    system_prompt: str = ""


# ---------------------------------------------------------------------------
# Capability Spec — tools, skills, dependencies
# ---------------------------------------------------------------------------

class AgentDependency(BaseModel):
    """Declaration of a dependency on another agent."""
    agent_name: str
    purpose: str
    fallback: str | None = Field(
        default=None,
        description="Fallback agent name when primary is unavailable",
    )


class CapabilitySpec(BaseModel):
    """Agent tools, skills, and inter-agent dependencies."""
    skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(
        default_factory=list,
        description="Explicit tool names this agent requires",
    )
    dependencies: list[AgentDependency] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Boundary Spec — resource limits and temporal constraints
# ---------------------------------------------------------------------------

class ResourceBudget(BaseModel):
    """Per-agent resource budget."""
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    max_iterations: int | None = None


class TerminationConditions(BaseModel):
    """Convergence-based termination conditions."""
    convergence_threshold: float | None = None
    convergence_rounds: int | None = None


class BoundarySpec(BaseModel):
    """Agent resource boundaries and temporal constraints."""
    timeout: int | None = Field(default=None, description="Execution timeout in seconds")
    stall_timeout: int | None = Field(default=None, description="Stall timeout in seconds")
    resource_budget: ResourceBudget | None = None
    termination: TerminationConditions | None = None
    max_retries: int = 3


# ---------------------------------------------------------------------------
# Lifecycle Spec — memory and error recovery
# ---------------------------------------------------------------------------

class ErrorStrategy(str, Enum):
    """Error recovery strategy."""
    FAIL_FAST = "fail_fast"
    RETRY_WITH_BACKOFF = "retry_with_backoff"
    FALLBACK_AGENT = "fallback_agent"
    CIRCUIT_BREAKER = "circuit_breaker"
    REPLAN = "replan"


class ErrorPolicy(BaseModel):
    """Error recovery policy chain."""
    strategy: ErrorStrategy = ErrorStrategy.RETRY_WITH_BACKOFF
    max_retries: int = 3
    backoff_base: float = 2.0
    backoff_cap: float = 60.0
    fallback_agent: str | None = None


class LifecycleSpec(BaseModel):
    """Agent memory and error recovery configuration."""
    memory_enabled: bool = True
    memory_scope: str | None = Field(
        default=None,
        description="Memory scope: 'session' or 'global'",
    )
    error_policy: ErrorPolicy | None = None


# ---------------------------------------------------------------------------
# Caller Override Interface
# ---------------------------------------------------------------------------

class InvocationOverrides(BaseModel):
    """Caller override interface — can only tighten, never loosen.

    The agent designer's defaults are ceilings.
    Callers may supply stricter criteria/budget/timeout.
    """
    success_criteria: list[SuccessCriterion] | None = None
    budget: ResourceBudget | None = None
    timeout: int | None = None


# ---------------------------------------------------------------------------
# AgentSpec — unified 5-core model
# ---------------------------------------------------------------------------

class AgentSpec(BaseModel):
    """Unified agent description model — 5 orthogonal cores.

    Replaces the scattered agent_type-based configuration with a single
    "one object describes one agent" model.
    """
    name: str
    version: str = "1.0.0"
    description: str = ""

    contract: ContractSpec = Field(default_factory=ContractSpec)
    brain: BrainSpec = Field(default_factory=BrainSpec)
    capability: CapabilitySpec = Field(default_factory=CapabilitySpec)
    boundary: BoundarySpec = Field(default_factory=BoundarySpec)
    lifecycle: LifecycleSpec = Field(default_factory=LifecycleSpec)

    # -------------------------------------------------------------------
    # Backward compatibility: AgentCapability interop
    # -------------------------------------------------------------------

    def to_capability(self) -> Any:
        """Convert to legacy AgentCapability for backward compatibility."""
        from core.dag_models import AgentCapability

        input_items = list(self.contract.input_schema.keys()) if self.contract.input_schema else []
        output_items = list(self.contract.output_schema.keys()) if self.contract.output_schema else {}

        return AgentCapability(
            id=self.name,
            name=self.name,
            description=self.description,
            skills=self.capability.skills,
            input_schema=input_items,
            output_schema=output_items,
            constraints=self.capability.constraints,
            system_prompt=self.brain.system_prompt,
        )

    @classmethod
    def from_capability(cls, capability: Any) -> AgentSpec:
        """Create AgentSpec from legacy AgentCapability."""
        from core.dag_models import AgentCapability

        if not isinstance(capability, AgentCapability):
            raise TypeError(f"Expected AgentCapability, got {type(capability).__name__}")

        input_schema = {k: {} for k in capability.input_schema} if capability.input_schema else {}
        output_schema = {k: {} for k in capability.output_schema} if capability.output_schema else {}

        return cls(
            name=capability.id,
            description=capability.description,
            contract=ContractSpec(
                input_schema=input_schema,
                output_schema=output_schema,
            ),
            brain=BrainSpec(system_prompt=capability.system_prompt),
            capability=CapabilitySpec(
                skills=capability.skills,
                constraints=capability.constraints,
            ),
        )
```

**Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_agent_spec.py -v --tb=short
```

Expected: All 25+ tests PASS.

**Step 5: Commit**

```bash
git add core/agent_spec.py tests/test_agent_spec.py
git commit -m "feat(M7.4): add AgentSpec unified 5-core model (#1109 T7.4.1, T7.4.2)

- ContractSpec: input/output schema + success criteria
- BrainSpec: quality_tier + model_id + system_prompt
- CapabilitySpec: skills + tools + AgentDependency
- BoundarySpec: timeout + ResourceBudget + TerminationConditions
- LifecycleSpec: memory + ErrorPolicy
- InvocationOverrides: caller tighten-only interface
- Backward compat: to_capability() / from_capability()"
```


### Task 2: Add AgentSpec re-exports to `core/models.py`

**Files:**
- Modify: `core/models.py`
- Test: `tests/test_agent_spec.py` (add import test)

**Step 1: Add import test**

Append to `tests/test_agent_spec.py`:

```python
class TestAgentSpecReExports:
    """Verify AgentSpec and sub-models are re-exported from core.models."""

    def test_import_from_core_models(self):
        from core.models import (
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
        from core.agent_spec import AgentSpec as Direct
        assert AgentSpec is Direct
```

**Step 2: Run to verify failure**

```bash
python -m pytest tests/test_agent_spec.py::TestAgentSpecReExports -v --tb=short
```

Expected: FAIL — `ImportError: cannot import name 'AgentSpec' from 'core.models'`

**Step 3: Add re-exports to `core/models.py`**

Add after the existing `from core.dag_models import ...` block (around line 47):

```python
from core.agent_spec import (  # noqa: F401
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
    TerminationConditions,
)
```

Add all 13 names to `__all__`.

**Step 4: Run tests to verify**

```bash
python -m pytest tests/test_agent_spec.py -v --tb=short
```

Expected: All tests PASS.

**Step 5: Commit**

```bash
git add core/models.py tests/test_agent_spec.py
git commit -m "feat(M7.4): re-export AgentSpec models from core.models (#1109)"
```


## Phase 2: Wire AgentSpec into Existing Code

### Task 3: Migrate AgentRegistry to support AgentSpec

**Files:**
- Modify: `core/agent_registry.py`
- Test: `tests/test_agent_spec.py` (add registry integration tests)

**Step 1: Write failing tests for registry integration**

Append to `tests/test_agent_spec.py`:

```python
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

    def test_list_agents_returns_specs(self):
        from core.agent_registry import AgentRegistry
        registry = AgentRegistry()
        specs = registry.list_specs()
        assert len(specs) >= 3
        names = [s.name for s in specs]
        assert "planner" in names
        assert "generator" in names
        assert "evaluator" in names

    def test_default_agents_are_full_specs(self):
        from core.agent_registry import AgentRegistry
        registry = AgentRegistry()
        for name in ("planner", "generator", "evaluator"):
            spec = registry.get_spec(name)
            assert spec is not None
            assert spec.contract.input_schema, f"{name} missing input_schema"
            assert spec.capability.skills, f"{name} missing skills"
            assert spec.boundary.timeout is not None or spec.boundary.max_retries > 0

    def test_to_prompt_description_unchanged(self):
        from core.agent_registry import AgentRegistry
        registry = AgentRegistry()
        desc = registry.to_prompt_description()
        assert "planner" in desc
        assert "generator" in desc
        assert "evaluator" in desc

    def test_register_spec_overwrite(self):
        from core.agent_registry import AgentRegistry
        registry = AgentRegistry()
        spec = AgentSpec(name="planner", description="Custom planner")
        registry.register_spec(spec)
        result = registry.get_spec("planner")
        assert result.description == "Custom planner"

    def test_load_from_yaml_new_format(self, tmp_path):
        from core.agent_registry import AgentRegistry
        yaml_content = """
agents:
  - name: custom_agent
    description: "A custom agent"
    contract:
      input_schema:
        description: "task spec"
      output_schema:
        description: "result"
    brain:
      quality_tier: fast
      system_prompt: "Custom prompt"
    capability:
      skills: ["custom_skill"]
      tools: ["read"]
    boundary:
      timeout: 120
      max_retries: 2
    lifecycle:
      memory_enabled: false
"""
        yaml_file = tmp_path / "agents.yaml"
        yaml_file.write_text(yaml_content)
        registry = AgentRegistry()
        registry.load_from_yaml(yaml_file)
        spec = registry.get_spec("custom_agent")
        assert spec is not None
        assert spec.brain.quality_tier == QualityTier.FAST
        assert spec.boundary.timeout == 120
        assert spec.lifecycle.memory_enabled is False
```

**Step 2: Run tests to verify failure**

```bash
python -m pytest tests/test_agent_spec.py::TestAgentRegistrySpecIntegration -v --tb=short
```

Expected: FAIL — `AttributeError: 'AgentRegistry' object has no attribute 'register_spec'`

**Step 3: Rewrite `core/agent_registry.py`**

Internal storage uses `AgentSpec`, exposes backward-compatible `AgentCapability` via `get()` and `list_agents()`. See design doc for full implementation.

Key changes:
- `_agents: dict[str, AgentCapability]` → `_specs: dict[str, AgentSpec]`
- `_register_defaults()` creates full 5-core AgentSpecs
- `register_spec()` / `get_spec()` / `list_specs()` — new API
- `register()` / `get()` / `list_agents()` — backward compat (wraps to_capability)
- `load_from_yaml()` auto-detects legacy vs 5-core format

**Step 4: Run all agent_spec tests**

```bash
python -m pytest tests/test_agent_spec.py -v --tb=short
```

**Step 5: Run existing tests to verify backward compatibility**

```bash
python -m pytest tests/ -v --tb=short -x -k "agent" --timeout=30 2>&1 | head -80
```

**Step 6: Commit**

```bash
git add core/agent_registry.py tests/test_agent_spec.py
git commit -m "feat(M7.4): migrate AgentRegistry to AgentSpec-backed storage (#1109 T7.4.3)"
```


### Task 4: Add `get_client_for_spec()` to LLMRouter

**Files:**
- Modify: `core/llm_router.py`
- Test: `tests/test_agent_spec.py`

Add method `get_client_for_spec(spec: AgentSpec) -> LLMClient` that uses `BrainSpec.model_id` for direct override or falls back to `get_client(spec.name)`.

**Commit:**

```bash
git add core/llm_router.py tests/test_agent_spec.py
git commit -m "feat(M7.4): add LLMRouter.get_client_for_spec() (#1109 T7.4.5)"
```


### Task 5: Wire NodeExecutor to read from AgentSpec

**Files:**
- Modify: `core/node_executor.py`
- Test: `tests/test_agent_spec.py`

Add `agent_registry: Any | None = None` to `NodeExecutorConfig`. Add `_get_agent_spec()` helper. Modify `_get_node_timeout()` and `_get_stall_timeout()` to prefer `BoundarySpec`. Add `_get_max_retries()` from `BoundarySpec`.

**Commit:**

```bash
git add core/node_executor.py tests/test_agent_spec.py
git commit -m "feat(M7.4): wire NodeExecutor to read config from AgentSpec (#1109 T7.4.4)"
```


### Task 6: Wire EvaluationPipeline to read from ContractSpec

**Files:**
- Modify: `core/evaluation_pipeline.py`
- Test: `tests/test_agent_spec.py`

Minimal change: extract `_should_run_evaluation(node)` method from inline condition.

**Commit:**

```bash
git add core/evaluation_pipeline.py tests/test_agent_spec.py
git commit -m "feat(M7.4): wire EvaluationPipeline to use ContractSpec (#1109 T7.4.6)"
```


## Phase 3: Orchestration Integration

### Task 7: Add DependencySpec checking to plan_validator

**Files:**
- Modify: `orchestrator/plan_validator.py`
- Test: `tests/test_agent_spec.py`

Add `_check_agent_dependencies()` method. Extend `validate()` signature with optional `agent_registry` param.

**Commit:**

```bash
git add orchestrator/plan_validator.py tests/test_agent_spec.py
git commit -m "feat(M7.4): add DependencySpec validation to plan_validator (#1109 T7.4.7)"
```


### Task 8: Add Capability-Contract consistency check

**Files:**
- Modify: `orchestrator/plan_validator.py`
- Test: `tests/test_agent_spec.py`

Add `_check_capability_contract_consistency()` method.

**Commit:**

```bash
git add orchestrator/plan_validator.py tests/test_agent_spec.py
git commit -m "feat(M7.4): add Capability-Contract consistency check (#1109 T7.4.8)"
```


### Task 9: Expose caller invocation interface

**Files:**
- Modify: `core/agent_registry.py`
- Test: `tests/test_agent_spec.py`

Add `invoke(agent_name, task, overrides)` async method. Add `_apply_overrides()` static method (tighten-only).

**Commit:**

```bash
git add core/agent_registry.py tests/test_agent_spec.py
git commit -m "feat(M7.4): add invoke() caller interface to AgentRegistry (#1109 T7.4.9)"
```


## Phase 4: YAML Config and Full Test Suite

### Task 10: Create `.weave/agents.yaml.example`

**Files:**
- Create: `.weave/agents.yaml.example`

Full example with reviewer agent showing all 5 cores.

**Commit:**

```bash
git add .weave/agents.yaml.example
git commit -m "docs(M7.4): add .weave/agents.yaml example (#1109 T7.4.10)"
```


### Task 11: Write comprehensive unit tests

**Files:**
- Modify: `tests/test_agent_spec.py`

Target: 80%+ coverage on `core/agent_spec.py` and `core/agent_registry.py`.

**Commit:**

```bash
git add tests/test_agent_spec.py
git commit -m "test(M7.4): comprehensive unit tests for AgentSpec (#1109 T7.4.11)"
```


### Task 12: Integration test — full YAML → AgentSpec → execution chain

**Files:**
- Create: `tests/test_agent_spec_integration.py`

End-to-end tests covering: YAML loading, mixed format, default agents, spec-to-executor config chain, invoke with overrides.

**Commit:**

```bash
git add tests/test_agent_spec_integration.py
git commit -m "test(M7.4): integration tests for YAML → AgentSpec → execution chain (#1109 T7.4.12)"
```


## Final: Full test suite and coverage

```bash
python -m pytest -v --tb=short --timeout=120
python -m pytest tests/test_agent_spec.py tests/test_agent_spec_integration.py \
  --cov=core.agent_spec --cov=core.agent_registry --cov-report=term-missing
```
