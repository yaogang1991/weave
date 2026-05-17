"""
Agent Registry: Capability-based agent discovery.

The orchestrator does NOT hardcode agent types.
It discovers available agents through this registry.

Default: Anthropic's 3 foundational agents (planner/generator/evaluator)
Extension: Projects register custom agents via config or code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import yaml

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
        self._agents: dict[str, AgentCapability] = {}
        self._factories: dict[str, Callable] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        """
        Register Anthropic Managed Agents' 3 foundational agents.
        These are the Weave's出厂设置.
        """
        self.register(AgentCapability(
            id="planner",
            name="Planner",
            description=(
                "Responsible for requirement analysis, task decomposition, "
                "architecture design, and technical decision-making. "
                "Produces structured plans, specifications, and architecture documents."
            ),
            skills=[
                "requirement_analysis",
                "task_decomposition",
                "architecture_design",
                "technical_decision",
                "interface_definition",
                "planning",
            ],
            input_schema=["user_requirements", "project_context"],
            output_schema=["plan", "specification", "architecture_doc", "sprint_contract"],
            constraints=[
                "Only produces design documents, does NOT write implementation code",
                "Must define clear success criteria for each task",
                "Must identify dependencies between tasks",
            ],
        ))

        self.register(AgentCapability(
            id="generator",
            name="Generator",
            description=(
                "Responsible for code implementation, file editing, feature development, "
                "and test writing. Executes the plan produced by the planner."
            ),
            skills=[
                "code_writing",
                "file_editing",
                "test_writing",
                "debugging",
                "git_operations",
                "implementation",
            ],
            input_schema=["plan", "existing_code", "feedback"],
            output_schema=["code", "tests", "git_commit", "implementation_artifact"],
            constraints=[
                "Must follow project coding standards",
                "Must read related existing code before modifying",
                "Must verify work by running tests",
                "Must produce handoff artifacts for evaluator",
            ],
        ))

        self.register(AgentCapability(
            id="evaluator",
            name="Evaluator",
            description=(
                "Responsible for quality assessment, test verification, code review, "
                "and pass/fail judgment. Provides structured feedback to generator."
            ),
            skills=[
                "test_execution",
                "quality_assessment",
                "code_review",
                "performance_analysis",
                "security_scan",
                "evaluation",
            ],
            input_schema=["code", "tests", "sprint_contract", "implementation_artifact"],
            output_schema=["evaluation_report", "pass_fail_verdict", "feedback", "score"],
            constraints=[
                "Does NOT modify code - only evaluates and reports",
                "Must provide explicit pass/fail verdict",
                "Feedback must be specific and actionable",
                "Uses predefined scoring criteria calibrated with examples",
            ],
        ))

    def register(self, capability: AgentCapability) -> None:
        """Register a new agent capability."""
        self._agents[capability.id] = capability

    def register_factory(self, agent_id: str, factory: Callable) -> None:
        """
        Register a factory function that creates agent instances.
        The factory signature: factory(task_description, artifacts) -> AgentInstance
        """
        self._factories[agent_id] = factory

    def get(self, agent_id: str) -> AgentCapability | None:
        """Get an agent's capability description."""
        return self._agents.get(agent_id)

    def list_agents(self) -> list[AgentCapability]:
        """List all registered agents."""
        return list(self._agents.values())

    def has_agent(self, agent_id: str) -> bool:
        """Check if an agent is registered."""
        return agent_id in self._agents

    def get_factory(self, agent_id: str) -> Callable | None:
        """Get the factory for creating agent instances."""
        return self._factories.get(agent_id)

    def unregister(self, agent_id: str) -> None:
        """Remove an agent from registry. Protected agents cannot be removed."""
        protected = {"planner", "generator", "evaluator"}
        if agent_id in protected:
            raise ValueError(f"Cannot unregister protected agent: {agent_id}")
        self._agents.pop(agent_id, None)
        self._factories.pop(agent_id, None)

    def load_from_yaml(self, path: str | Path) -> None:
        """Load agent definitions from a YAML file."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Agent config not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        for agent_def in data.get("agents", []):
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
        """
        Generate a formatted description of all registered agents
        for injection into the orchestrator's system prompt.
        """
        lines = ["Available Worker Agents (registered in the system):"]
        for agent in self._agents.values():
            lines.append(f"\n### {agent.id}: {agent.name}")
            lines.append(f"Description: {agent.description}")
            lines.append(f"Skills: {', '.join(agent.skills)}")
            lines.append(f"Input: {', '.join(agent.input_schema)}")
            lines.append(f"Output: {', '.join(agent.output_schema)}")
            if agent.constraints:
                lines.append("Constraints:")
                for c in agent.constraints:
                    lines.append(f"  - {c}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"AgentRegistry(agents={list(self._agents.keys())})"
