"""
Intelligent Orchestrator: An LLM-driven agent that plans and manages multi-agent execution.

Key design principles:
1. AGNOSTIC: Does NOT hardcode agent types - discovers via AgentRegistry
2. DYNAMIC: Generates DAG at runtime based on user requirements
3. ADAPTIVE: Handles failures by replanning, not hardcoded rules
4. MINIMAL DEFAULT: Only assumes planner/generator/evaluator exist

The orchestrator itself IS an agent - it uses LLM to reason about task decomposition
and agent assignment, but it does NOT execute tasks itself.
"""

from __future__ import annotations

import json
import re
from typing import Any

from core.models_v2 import (
    DAG,
    DAGNode,
    DAGEdge,
    FailureDecision,
    AgentCapability,
    OrchestratorPlan,
)
from core.agent_registry import AgentRegistry
from core.config import LLMConfig
from core.llm_client import LLMClient
from session.store import SessionStore


class IntelligentOrchestrator:
    """
    Orchestrator Agent: Plans DAG, monitors execution, adapts to failures.

    This is itself an LLM-driven agent, not a hardcoded state machine.
    It queries the AgentRegistry to discover available workers,
    then uses LLM reasoning to decompose tasks and build execution DAGs.
    """

    PLANNING_PROMPT_TEMPLATE = """You are the Orchestrator Agent for a multi-agent software development harness.

Your job: Analyze the user's requirement and produce an execution plan (DAG).

{agent_descriptions}

## Planning Rules

1. **Default pattern for simple tasks**: planner → generator → evaluator (linear)
2. **Decompose complex tasks**: If the requirement spans multiple domains (e.g., frontend + backend + database), create separate generator nodes for each domain
3. **Parallelize when possible**: Nodes without data dependencies should execute in parallel
4. **Always include evaluator**: Every code generation path must end with an evaluator node
5. **Specific task descriptions**: Each node's task must be concrete and verifiable
6. **Valid agent types ONLY**: Use ONLY the agent types listed above. Do not invent new ones.

## Output Format

Return a JSON object with this exact structure:

{{
  "reasoning": "Brief explanation of your planning decisions...",
  "nodes": [
    {{
      "id": "plan",
      "agent_type": "planner",
      "task": "Analyze requirement and produce implementation plan..."
    }},
    {{
      "id": "impl",
      "agent_type": "generator",
      "task": "Implement the planned feature following project conventions..."
    }},
    {{
      "id": "eval",
      "agent_type": "evaluator",
      "task": "Verify implementation against plan and project standards..."
    }}
  ],
  "edges": [
    {{"from": "plan", "to": "impl"}},
    {{"from": "impl", "to": "eval"}}
  ]
}}

## Important
- Node IDs must be unique and descriptive (e.g., "plan", "impl_api", "eval")
- Every edge references valid node IDs
- The DAG must be acyclic
- Keep it minimal: don't add unnecessary nodes
"""

    ADAPTATION_PROMPT_TEMPLATE = """You are the Orchestrator Agent handling an execution failure.

A Worker Agent has failed during execution. Decide how to proceed.

Failed Node:
- ID: {node_id}
- Agent Type: {agent_type}
- Task: {task}
- Error: {error}
- Retry count: {retry_count}

DAG Status:
{dag_status}

Available Actions:
- **retry**: Retry the same node (if error seems transient)
- **skip**: Skip this node and continue (if failure is acceptable)
- **abort**: Stop execution entirely (if failure is critical)
- **replan**: Create a new plan (if current plan is fundamentally flawed)

Return JSON:
{{
  "action": "retry|skip|abort|replan",
  "reasoning": "Why you chose this action..."
}}

Choose "replan" if:
- The task was incorrectly decomposed
- The wrong agent type was assigned
- The dependency structure is wrong

Choose "abort" if:
- Critical security issue detected
- Data loss risk
- The failure is unrecoverable

Choose "skip" if:
- The node is non-critical (e.g., optional optimization)
- The failure is expected and acceptable

Choose "retry" if:
- Transient error (network, timeout)
- The agent can succeed with another attempt
"""

    def __init__(
        self,
        llm_config: LLMConfig,
        session_store: SessionStore,
        agent_registry: AgentRegistry,
    ):
        self.llm_config = llm_config
        self.session_store = session_store
        self.agent_registry = agent_registry
        self.llm = LLMClient(llm_config)

    async def plan(self, requirement: str, project_context: dict | None = None) -> DAG:
        """
        Generate an execution DAG from user requirements.

        This is the core planning method. It:
        1. Discovers available agents from registry
        2. Builds a dynamic planning prompt
        3. Calls LLM to generate the DAG
        4. Validates and returns the DAG
        """
        # Step 1: Discover available agents
        agent_descriptions = self.agent_registry.to_prompt_description()

        # Step 2: Build planning prompt
        system_prompt = self.PLANNING_PROMPT_TEMPLATE.format(
            agent_descriptions=agent_descriptions
        )

        user_prompt = f"User requirement: {requirement}"
        if project_context:
            user_prompt += f"\n\nProject context: {json.dumps(project_context, indent=2, default=str)}"

        # Step 3: Call LLM for planning
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        response = self.llm.call(messages, tools=[])

        # Step 4: Parse and validate the plan
        plan_data = self._extract_json(response.get("content", ""))
        plan = OrchestratorPlan(**plan_data)

        # Step 5: Validate all agent types exist in registry
        for node_def in plan.nodes:
            agent_type = node_def.get("agent_type", "")
            if not self.agent_registry.has_agent(agent_type):
                raise ValueError(
                    f"Plan references unregistered agent: {agent_type}. "
                    f"Available: {[a.id for a in self.agent_registry.list_agents()]}"
                )

        # Step 6: Convert to DAG
        dag = self._plan_to_dag(plan)

        return dag

    async def adapt_to_failure(self, dag: DAG, failed_node_id: str, error: str = "") -> FailureDecision:
        """
        Handle a failed node by asking the orchestrator LLM to decide.

        This is the adaptive part - the orchestrator reasons about the failure
        and decides the best course of action, rather than using hardcoded rules.
        """
        failed_node = dag.nodes[failed_node_id]

        # Build DAG status summary
        dag_status = []
        for nid, node in dag.nodes.items():
            dag_status.append(
                f"- {nid}: {node.agent_type} = {node.status.value}"
                f"{' (FAILED: ' + node.error[:100] + ')' if node.status.value == 'failed' else ''}"
            )

        # Build adaptation prompt
        system_prompt = self.ADAPTATION_PROMPT_TEMPLATE.format(
            node_id=failed_node_id,
            agent_type=failed_node.agent_type,
            task=failed_node.task_description,
            error=failed_node.error[:500],
            retry_count=failed_node.retry_count,
            dag_status="\n".join(dag_status),
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Handle failure of node {failed_node_id}"},
        ]

        response = self.llm.call(messages, tools=[])

        try:
            decision_data = self._extract_json(response.get("content", ""))
            return FailureDecision(**decision_data)
        except Exception:
            # Default to retry, then abort
            if failed_node.retry_count < failed_node.max_retries:
                return FailureDecision(action="retry", reasoning="Parse error, defaulting to retry")
            return FailureDecision(action="abort", reasoning="Parse error, max retries reached")

    def _plan_to_dag(self, plan: OrchestratorPlan) -> DAG:
        """Convert an OrchestratorPlan to an executable DAG."""
        dag = DAG(reasoning=plan.reasoning)

        for node_def in plan.nodes:
            node = DAGNode(
                id=node_def["id"],
                agent_type=node_def["agent_type"],
                task_description=node_def["task"],
            )
            dag.add_node(node)

        for edge_def in plan.edges:
            dag.add_edge(edge_def["from"], edge_def["to"])

        return dag

    def _extract_json(self, text: str) -> dict:
        """
        Extract JSON from LLM response (handles markdown code blocks).

        Uses regex-based extraction with brace matching to handle edge cases
        like nested code blocks or multiple code blocks in the response.
        """
        text = text.strip()

        # Strategy 1: Try to find JSON inside ```json ... ``` blocks
        json_block_match = re.search(r"```json\s*\n(.*?)```", text, re.DOTALL)
        if json_block_match:
            return json.loads(json_block_match.group(1).strip())

        # Strategy 2: Try to find JSON inside generic ``` ... ``` blocks
        generic_block_match = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
        if generic_block_match:
            candidate = generic_block_match.group(1).strip()
            if candidate.startswith("{") or candidate.startswith("["):
                return json.loads(candidate)

        # Strategy 3: Find the first top-level JSON object using brace matching
        # This handles cases where the LLM outputs raw JSON without code blocks
        brace_depth = 0
        start = None
        for i, ch in enumerate(text):
            if ch == '{':
                if brace_depth == 0:
                    start = i
                brace_depth += 1
            elif ch == '}':
                brace_depth -= 1
                if brace_depth == 0 and start is not None:
                    candidate = text[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        start = None
                        continue

        raise json.JSONDecodeError("No valid JSON object found in LLM response", text, 0)
