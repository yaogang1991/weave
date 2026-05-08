"""
Agent Pool: Manages multiple independent Agent instances.

Each Worker Agent gets:
- Independent LLM context (no shared context window)
- Isolated tool registry (subset of tools based on agent type)
- Guardrails enforcement on every tool call
- Independent session tracking within the global session
"""

from __future__ import annotations

import asyncio
from typing import Any

from core.models import AgentMessage
from core.models_v2 import DAGNode, HandoffArtifact, AgentCapability
from core.config import LLMConfig
from core.agent_registry import AgentRegistry
from session.store import SessionStore
from agent.worker import AgentWorker
from tools.registry import ToolRegistry
from guardrails.policy import Guardrails


class WorkerAgent:
    """
    A single Worker Agent instance with isolated context.

    This wraps AgentWorker with:
    - Agent-type-specific system prompt
    - Context isolation (reset between tasks)
    - Guardrails enforcement on tool calls
    - Artifact collection
    """

    SYSTEM_PROMPTS = {
        "planner": """You are the Planner Agent in a software development team.

Your role: Analyze requirements, decompose tasks, design architecture.

You have access to tools: read, glob, grep.

Rules:
1. Produce structured, actionable plans
2. Define clear success criteria for each task
3. Identify dependencies and risks
4. Consider existing codebase before planning changes
5. Output: plan.md, spec.md, architecture decision records

Always consider the project context and existing conventions.
""",

        "generator": """You are the Generator Agent in a software development team.

Your role: Implement code according to specifications.

You have access to tools: read, write, edit, bash, glob, grep, git.

Rules:
1. Follow the plan precisely
2. Read existing code before modifying
3. Use edit tool for small changes (old_string → new_string)
4. Use write tool for new files
5. Run tests after implementation
6. Follow project coding standards (import order, naming, formatting)

Work systematically: gather context → implement → verify.
""",

        "evaluator": """You are the Evaluator Agent in a software development team.

Your role: Assess quality, run tests, provide structured feedback.

You have access to tools: read, bash, glob, grep.

Rules:
1. Be strict but constructive
2. Run all relevant tests
3. Check code quality (lint, type safety, coverage)
4. Provide explicit PASS/FAIL verdict
5. Feedback must be specific and actionable

Evaluate against:
- Functional correctness (tests pass)
- Code quality (lint clean, typed)
- Architecture alignment (follows project patterns)
- Edge cases handled
""",
    }

    # Agent-type-specific tool allowlists
    TOOL_ALLOWLIST = {
        "planner": {"read", "glob", "grep"},
        "generator": {"read", "write", "edit", "bash", "glob", "grep", "git"},
        "evaluator": {"read", "bash", "glob", "grep"},
    }

    def __init__(
        self,
        capability: AgentCapability,
        llm_config: LLMConfig,
        session_store: SessionStore,
        tool_registry: ToolRegistry,
        guardrails: Guardrails | None = None,
        max_iterations: int = 50,
    ):
        self.capability = capability
        self.llm_config = llm_config
        self.session_store = session_store
        self.tool_registry = tool_registry
        self.guardrails = guardrails
        self.max_iterations = max_iterations

        # Build agent-specific system prompt
        system_prompt = capability.system_prompt or self.SYSTEM_PROMPTS.get(
            capability.id,
            f"You are the {capability.name} agent. {capability.description}"
        )

        self.worker = AgentWorker(llm_config, session_store)
        self.system_prompt = system_prompt

        # Filter tools by agent type
        allowed = self.TOOL_ALLOWLIST.get(capability.id, {"read", "glob", "grep"})
        self.tools = [s for s in tool_registry.schemas if s["name"] in allowed]

    def _execute_tool(self, name: str, arguments: dict):
        """Execute a tool through guardrails if available, otherwise directly."""
        if self.guardrails:
            return self.guardrails.guarded_execute(name, arguments)
        return self.tool_registry.execute(name, arguments)

    async def execute(
        self,
        task: str,
        input_artifacts: list[HandoffArtifact],
        session_id: str,
    ) -> dict[str, Any]:
        """
        Execute this agent's task with isolated context.

        Context isolation: Each execution starts fresh - previous
        executions do not pollute the context window.
        """
        # Build context from input artifacts
        artifact_context = self._format_artifacts(input_artifacts)

        full_prompt = f"""{artifact_context}

Your task: {task}

Execute using your available tools. Produce clear, verifiable output.
"""

        # Run the agent (dumb loop) via AgentWorker
        return await self._run_with_tools(full_prompt, session_id)

    async def _run_with_tools(self, prompt: str, session_id: str) -> dict[str, Any]:
        """Run agent loop and collect results via AgentWorker."""

        def _run_sync() -> list[AgentMessage]:
            return list(
                self.worker.run(
                    session_id=session_id,
                    system_prompt=self.system_prompt,
                    user_message=prompt,
                    tools=self.tools,
                    tool_executor=self,
                    max_iterations=self.max_iterations,
                )
            )

        # AgentWorker.run() is synchronous (blocks on LLM API calls);
        # offload to a thread and cap total wall-clock time per node.
        try:
            messages = await asyncio.wait_for(
                asyncio.to_thread(_run_sync),
                timeout=60,
            )
        except asyncio.TimeoutError:
            return {
                "status": "timeout",
                "summary": "Agent execution timed out after 60s",
                "artifacts": [],
                "output": "",
            }

        final = messages[-1] if messages else None
        return {
            "status": "completed",
            "summary": final.content if final else "",
            "artifacts": [],
            "output": final.content if final else "",
        }

    def _format_artifacts(self, artifacts: list[HandoffArtifact]) -> str:
        """Format input artifacts as context for the agent."""
        if not artifacts:
            return ""

        parts = ["## Input from previous agents:"]
        for artifact in artifacts:
            parts.append(f"\n### From {artifact.from_agent}:")
            parts.append(f"Summary: {artifact.content}")
            if artifact.file_paths:
                parts.append(f"Files: {', '.join(artifact.file_paths)}")
        return "\n".join(parts)


class AgentPool:
    """
    Pool of Worker Agent instances.

    Creates agent instances on demand based on AgentRegistry capabilities.
    Each instance is independent with isolated context.
    """

    def __init__(
        self,
        llm_config: LLMConfig,
        session_store: SessionStore,
        agent_registry: AgentRegistry,
        tool_registry: ToolRegistry | None = None,
        guardrails: Guardrails | None = None,
        max_iterations: int = 50,
    ):
        self.llm_config = llm_config
        self.session_store = session_store
        self.agent_registry = agent_registry
        self.tool_registry = tool_registry or ToolRegistry()
        self.guardrails = guardrails
        self.max_iterations = max_iterations
        self._instances: dict[str, WorkerAgent] = {}

    def get_or_create(self, agent_type: str) -> WorkerAgent:
        """Get or create a WorkerAgent instance for the given type."""
        if agent_type not in self._instances:
            capability = self.agent_registry.get(agent_type)
            if not capability:
                raise ValueError(f"Unknown agent type: {agent_type}")

            self._instances[agent_type] = WorkerAgent(
                capability=capability,
                llm_config=self.llm_config,
                session_store=self.session_store,
                tool_registry=self.tool_registry,
                guardrails=self.guardrails,
                max_iterations=self.max_iterations,
            )

        return self._instances[agent_type]

    def get_executor(self, session_id: str):
        """
        Return a callable that the DAG engine can use to execute nodes.

        Signature: async def executor(node, artifacts) -> result_dict
        """
        async def _executor(node: DAGNode, artifacts: list[HandoffArtifact]) -> dict:
            worker = self.get_or_create(node.agent_type)
            return await worker.execute(node.task_description, artifacts, session_id)

        return _executor

    def reset_context(self, agent_type: str) -> None:
        """Reset an agent's context (for context isolation between tasks)."""
        if agent_type in self._instances:
            del self._instances[agent_type]

    def reset_all(self) -> None:
        """Reset all agent contexts."""
        self._instances.clear()
