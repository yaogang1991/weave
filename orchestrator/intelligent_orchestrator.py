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

from core.models import (
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
from core.llm_router import LLMRouter
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
      "task": "Implement the planned feature following project conventions...",
      "success_criteria": ["tests pass", "lint clean"]
    }},
    {{
      "id": "eval",
      "agent_type": "evaluator",
      "task": "Verify implementation against plan and project standards...",
      "success_criteria": ["tests pass", "coverage 80%"]
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

    REPLAN_PROMPT_TEMPLATE = """You are the Orchestrator Agent for a multi-agent software development harness.

A previous execution plan has partially failed. You need to create a new plan for the REMAINING work, taking into account what has already been successfully completed.

## Already Executed Nodes

{executed_nodes}

## Failed Node

- ID: {failed_node}
- Error: {failed_error}

## Available Agents

{agent_descriptions}

## Replanning Rules

1. **Preserve completed work**: Do NOT re-plan nodes that already succeeded. Only plan for failed, skipped, or pending nodes.
2. **Address the root cause**: The new plan should specifically address why the failed node errored (e.g., different agent type, simpler task decomposition, alternative approach).
3. **Reuse successful outputs**: Dependent nodes can reference artifacts from already-completed successful nodes.
4. **Valid agent types ONLY**: Use ONLY the agent types listed above.
5. **Keep it minimal**: Only include nodes that still need to be executed.

## Output Format

Return a JSON object with this exact structure:

{{
  "reasoning": "Explanation of why the original plan failed and how the new plan addresses it...",
  "nodes": [
    {{
      "id": "plan_fix",
      "agent_type": "planner",
      "task": "Re-analyze the failure and produce a corrected implementation plan..."
    }},
    {{
      "id": "impl_fix",
      "agent_type": "generator",
      "task": "Implement the corrected plan...",
      "success_criteria": ["tests pass", "lint clean"]
    }},
    {{
      "id": "eval_fix",
      "agent_type": "evaluator",
      "task": "Verify the corrected implementation...",
      "success_criteria": ["tests pass", "coverage 80%"]
    }}
  ],
  "edges": [
    {{"from": "plan_fix", "to": "impl_fix"}},
    {{"from": "impl_fix", "to": "eval_fix"}}
  ]
}}

## Important
- Node IDs must be unique and not conflict with already-executed nodes
- Every edge references valid node IDs
- The DAG must be acyclic
- Include ALL nodes that still need execution (failed node + any pending downstream nodes)
"""

    def __init__(
        self,
        llm_config: LLMConfig,
        session_store: SessionStore,
        agent_registry: AgentRegistry,
        llm_router: LLMRouter | None = None,
    ):
        self.llm_config = llm_config
        self.session_store = session_store
        self.agent_registry = agent_registry
        if llm_router:
            self.llm = llm_router.get_client("orchestrator")
        else:
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

        # Get tools schema for the orchestrator (no tools needed for planning)
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
                success_criteria=node_def.get("success_criteria", []),
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

    async def replan(self, dag: DAG, failed_node_id: str, requirement: str = "") -> DAG:
        """
        Re-plan the remaining work after a node failure.

        This method:
        1. Collects a summary of already-executed nodes (both successful and failed)
        2. Builds a replanning prompt with execution context
        3. Calls the LLM to generate a new plan for the remaining work
        4. Validates and converts the plan to a DAG
        5. Returns the new DAG (to be merged with the old one by the engine)

        Args:
            dag: The current DAG with execution status populated.
            failed_node_id: The ID of the node that triggered the replan.
            requirement: The original user requirement for context.

        Returns:
            A new DAG containing only the nodes that still need execution.
        """
        # 1. Collect executed node summaries
        executed_summary: list[dict[str, Any]] = []
        for nid, node in dag.nodes.items():
            if node.status.value in ("success", "failed"):
                executed_summary.append({
                    "id": nid,
                    "agent_type": node.agent_type,
                    "status": node.status.value,
                    "task": node.task_description[:100],
                    "result_summary": (
                        node.result.get("summary", "")[:200]
                        if node.result else ""
                    ),
                })

        # 2. Build replan prompt
        failed_error = dag.nodes[failed_node_id].error[:500] if failed_node_id in dag.nodes else ""
        system_prompt = self.REPLAN_PROMPT_TEMPLATE.format(
            executed_nodes=json.dumps(executed_summary, indent=2),
            failed_node=failed_node_id,
            failed_error=failed_error,
            agent_descriptions=self.agent_registry.to_prompt_description(),
        )

        user_prompt = (
            f"Original requirement: {requirement}\n\n"
            f"Failed node: {failed_node_id}\n\n"
            f"Please generate a new plan for the remaining work."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # 3. Call LLM
        response = self.llm.call(messages, tools=[])

        # 4. Parse and validate
        plan_data = self._extract_json(response.get("content", ""))
        plan = OrchestratorPlan(**plan_data)

        for node_def in plan.nodes:
            agent_type = node_def.get("agent_type", "")
            if not self.agent_registry.has_agent(agent_type):
                raise ValueError(
                    f"Replan references unregistered agent: {agent_type}. "
                    f"Available: {[a.id for a in self.agent_registry.list_agents()]}"
                )

        # 5. Convert to DAG and return
        new_dag = self._plan_to_dag(plan)
        return new_dag
