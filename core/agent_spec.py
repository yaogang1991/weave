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
        output_items = list(self.contract.output_schema.keys()) if self.contract.output_schema else []

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
