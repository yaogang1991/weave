"""
Agent Registry: Capability-based agent discovery.

The orchestrator does NOT hardcode agent types.
It discovers available agents through this registry.

Default: Anthropic's 3 foundational agents (planner/generator/evaluator)
Extension: Projects register custom agents via config or code.

M7.4: Internal storage uses AgentSpec. Backward-compatible AgentCapability
      API is preserved via to_capability() / from_capability() conversion.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import yaml

from core.agent_spec import (
    AgentSpec,
    BoundarySpec,
    BrainSpec,
    CapabilitySpec,
    ContractSpec,
    ErrorPolicy,
    ErrorStrategy,
    LifecycleSpec,
    QualityTier,
)
from core.models import AgentCapability


class AgentRegistry:
    """
    Central registry for Worker Agent capabilities.

    Design principles:
    1. Orchestrator is AGNOSTIC - it queries the registry, doesn't hardcode types
    2. Default 3 agents (planner/generator/evaluator) are registered at init
    3. Projects extend by registering custom agents
    4. Registration can be via YAML config or programmatic API
    """

    def __init__(self):
        self._specs: dict[str, AgentSpec] = {}
        self._factories: dict[str, Callable] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        """Register the 3 foundational agents as full AgentSpecs."""
        self.register_spec(AgentSpec(
            name="planner",
            description=(
                "Responsible for requirement analysis, task decomposition, "
                "architecture design, and technical decision-making. "
                "Produces structured plans, specifications, and architecture documents."
            ),
            contract=ContractSpec(
                input_schema={
                    "user_requirements": {"description": "User requirement text"},
                    "project_context": {"description": "Project context and constraints"},
                },
                output_schema={
                    "plan": {"description": "DAG execution plan"},
                    "specification": {"description": "Task specification"},
                    "architecture_doc": {"description": "Architecture document"},
                    "sprint_contract": {"description": "Sprint contract"},
                },
            ),
            brain=BrainSpec(quality_tier=QualityTier.HIGH, system_prompt=""),
            capability=CapabilitySpec(
                skills=[
                    "requirement_analysis",
                    "task_decomposition",
                    "architecture_design",
                    "technical_decision",
                    "interface_definition",
                    "planning",
                ],
                constraints=[
                    "Only produces design documents, does NOT write implementation code",
                    "Must define clear success criteria for each task",
                    "Must identify dependencies between tasks",
                ],
            ),
            boundary=BoundarySpec(timeout=300, max_retries=2),
            lifecycle=LifecycleSpec(
                memory_enabled=True,
                error_policy=ErrorPolicy(strategy=ErrorStrategy.REPLAN, max_retries=2),
            ),
        ))

        self.register_spec(AgentSpec(
            name="generator",
            description=(
                "Responsible for code implementation, file editing, feature development, "
                "and test writing. Executes the plan produced by the planner."
            ),
            contract=ContractSpec(
                input_schema={
                    "plan": {"description": "Execution plan"},
                    "existing_code": {"description": "Existing codebase"},
                    "feedback": {"description": "Evaluator feedback"},
                },
                output_schema={
                    "code": {"description": "Generated code"},
                    "tests": {"description": "Test files"},
                    "git_commit": {"description": "Git commit"},
                    "implementation_artifact": {"description": "Handoff artifact"},
                },
            ),
            brain=BrainSpec(quality_tier=QualityTier.BALANCED, system_prompt=""),
            capability=CapabilitySpec(
                skills=[
                    "code_writing",
                    "file_editing",
                    "test_writing",
                    "debugging",
                    "git_operations",
                    "implementation",
                ],
                tools=["read", "write", "edit", "bash", "glob", "grep"],
                constraints=[
                    "Must follow project coding standards",
                    "Must read related existing code before modifying",
                    "Must verify work by running tests",
                    "Must produce handoff artifacts for evaluator",
                ],
            ),
            boundary=BoundarySpec(timeout=600, max_retries=3),
            lifecycle=LifecycleSpec(
                memory_enabled=True,
                error_policy=ErrorPolicy(strategy=ErrorStrategy.RETRY_WITH_BACKOFF),
            ),
        ))

        self.register_spec(AgentSpec(
            name="evaluator",
            description=(
                "Responsible for quality assessment, test verification, code review, "
                "and pass/fail judgment. Provides structured feedback to generator."
            ),
            contract=ContractSpec(
                input_schema={
                    "code": {"description": "Code to evaluate"},
                    "tests": {"description": "Test files"},
                    "sprint_contract": {"description": "Sprint contract"},
                    "implementation_artifact": {"description": "Handoff artifact"},
                },
                output_schema={
                    "evaluation_report": {"description": "Evaluation report"},
                    "pass_fail_verdict": {"description": "Pass/fail verdict"},
                    "feedback": {"description": "Structured feedback"},
                    "score": {"description": "Quality score"},
                },
            ),
            brain=BrainSpec(quality_tier=QualityTier.BALANCED, system_prompt=""),
            capability=CapabilitySpec(
                skills=[
                    "test_execution",
                    "quality_assessment",
                    "code_review",
                    "performance_analysis",
                    "security_scan",
                    "evaluation",
                ],
                constraints=[
                    "Does NOT modify code - only evaluates and reports",
                    "Must provide explicit pass/fail verdict",
                    "Feedback must be specific and actionable",
                    "Uses predefined scoring criteria calibrated with examples",
                ],
            ),
            boundary=BoundarySpec(timeout=480, max_retries=2),
            lifecycle=LifecycleSpec(
                memory_enabled=True,
                error_policy=ErrorPolicy(strategy=ErrorStrategy.FAIL_FAST, max_retries=1),
            ),
        ))

    # ------------------------------------------------------------------
    # AgentSpec API (primary)
    # ------------------------------------------------------------------

    def register_spec(self, spec: AgentSpec) -> None:
        """Register an AgentSpec."""
        self._specs[spec.name] = spec

    def get_spec(self, name: str) -> AgentSpec | None:
        """Get an AgentSpec by name."""
        return self._specs.get(name)

    def list_specs(self) -> list[AgentSpec]:
        """List all registered AgentSpecs."""
        return list(self._specs.values())

    # ------------------------------------------------------------------
    # Backward-compatible AgentCapability API
    # ------------------------------------------------------------------

    def register(self, capability: AgentCapability) -> None:
        """Register via legacy AgentCapability (converted to AgentSpec internally)."""
        spec = AgentSpec.from_capability(capability)
        self._specs[spec.name] = spec

    def get(self, agent_id: str) -> AgentCapability | None:
        """Get backward-compatible AgentCapability."""
        spec = self._specs.get(agent_id)
        return spec.to_capability() if spec else None

    def list_agents(self) -> list[AgentCapability]:
        """List all agents as backward-compatible AgentCapabilities."""
        return [spec.to_capability() for spec in self._specs.values()]

    # ------------------------------------------------------------------
    # Shared API (unchanged)
    # ------------------------------------------------------------------

    def has_agent(self, agent_id: str) -> bool:
        """Check if an agent is registered."""
        return agent_id in self._specs

    def register_factory(self, agent_id: str, factory: Callable) -> None:
        """Register a factory function that creates agent instances."""
        self._factories[agent_id] = factory

    def get_factory(self, agent_id: str) -> Callable | None:
        """Get the factory for creating agent instances."""
        return self._factories.get(agent_id)

    def unregister(self, agent_id: str) -> None:
        """Remove an agent. Protected agents cannot be removed."""
        protected = {"planner", "generator", "evaluator"}
        if agent_id in protected:
            raise ValueError(f"Cannot unregister protected agent: {agent_id}")
        self._specs.pop(agent_id, None)
        self._factories.pop(agent_id, None)

    def load_from_yaml(self, path: str | Path) -> None:
        """Load agent definitions from YAML.

        Supports both legacy AgentCapability format and new 5-core AgentSpec format.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Agent config not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        for agent_def in data.get("agents", []):
            # Detect format: new format has 5-core keys
            if any(k in agent_def for k in ("contract", "brain", "capability", "boundary", "lifecycle")):
                spec = AgentSpec.model_validate(agent_def)
                self.register_spec(spec)
            else:
                # Legacy AgentCapability format
                capability = AgentCapability(**agent_def)
                self.register(capability)

    def load_from_directory(self, dir_path: str | Path) -> None:
        """Load all YAML agent definitions from a directory."""
        dir_path = Path(dir_path)
        if not dir_path.exists():
            return

        for yaml_file in dir_path.glob("*.yaml"):
            self.load_from_yaml(yaml_file)

    def to_prompt_description(self) -> str:
        """Generate formatted description for orchestrator system prompt."""
        lines = ["Available Worker Agents (registered in the system):"]
        for spec in self._specs.values():
            cap = spec.capability
            contract = spec.contract
            lines.append(f"\n### {spec.name}: {spec.name}")
            lines.append(f"Description: {spec.description}")
            lines.append(f"Skills: {', '.join(cap.skills)}")
            input_items = list(contract.input_schema.keys()) if contract.input_schema else []
            output_items = list(contract.output_schema.keys()) if contract.output_schema else []
            if input_items:
                lines.append(f"Input: {', '.join(input_items)}")
            if output_items:
                lines.append(f"Output: {', '.join(output_items)}")
            if cap.constraints:
                lines.append("Constraints:")
                for c in cap.constraints:
                    lines.append(f"  - {c}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"AgentRegistry(agents={list(self._specs.keys())})"
