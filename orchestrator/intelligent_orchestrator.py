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
import logging
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
from orchestrator.plan_validator import PlanValidator, PlanValidationError

logger = logging.getLogger(__name__)
from templates.library import TemplateRegistry


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
7. **Scope isolation**: For tasks that create independent libraries or utilities,
   task descriptions must explicitly state "create a standalone module that does NOT
   import from or depend on existing project modules". List specific features required.
8. **Avoid stdlib shadowing**: NEVER name a package/module the same as a Python
   standard library module (e.g., urllib, json, collections, typing, io, os, sys,
   pathlib, http, email, html, xml, asyncio, logging, unittest, ctypes, importlib,
   multiprocessing, sqlite3, xmlrpc, lib2to3, distutils, curses, tkinter). If the
   user's requirement mentions such a name, use a prefixed alternative (e.g.,
   "myurl_lib" instead of "urllib", "json_utils" instead of "json"). Shadowing
   stdlib causes catastrophic import failures in pytest and the entire runtime.
9. **Cross-node naming consistency**: When creating PARALLEL generator nodes that
   share a library namespace (e.g., one node creates source files, another creates
   tests for those sources), you MUST do ONE of the following to prevent naming
   mismatches:
   a. **Preferred — serialize**: Add an edge from the source node to the test node
      so tests are generated AFTER source code exists. The test generator will read
      the source files and use the exact class/function names.
   b. **Alternative — explicit naming contract**: If parallel execution is required,
      include an explicit "NAMING CONTRACT" section in EACH node's task description
      listing all class names, function names, and module paths that both nodes must
      use. Example: "NAMING CONTRACT: class TokenBucket (not TokenBucketLimiter),
      module path ratelib.token_bucket".

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
      "success_criteria": [
        {{"type": "tests_pass", "description": "tests pass"}},
        {{"type": "lint", "description": "lint clean"}}
      ]
    }},
    {{
      "id": "eval",
      "agent_type": "evaluator",
      "task": "Verify implementation against plan and project standards...",
      "success_criteria": [
        {{"type": "tests_pass", "description": "tests pass"}},
        {{"type": "coverage", "target": 80, "description": "coverage 80%"}}
      ]
    }}
  ],
  "edges": [
    {{"from": "plan", "to": "impl"}},
    {{"from": "impl", "to": "eval"}}
  ]
}}

## Success Criteria Types

Each success_criteria entry should be a structured object with a "type" field:
- **tests_pass**: {{"type": "tests_pass", "description": "tests pass"}} — runs pytest
- **lint**: {{"type": "lint", "description": "lint clean"}} — runs flake8/ruff
- **file_exists**: {{"type": "file_exists", "path": "src/foo.py", "description": "file exists"}} — exact path must exist on disk; use ONLY when the exact filename is a hard requirement
- **file_pattern**: {{"type": "file_pattern", "pattern": "reporter/*.py", "description": "report module exists"}} — glob pattern; at least one non-empty file must match; use when the generator can choose the filename
- **coverage**: {{"type": "coverage", "target": 80, "description": "coverage 80%"}}
- **no_critical**: {{"type": "no_critical", "description": "no critical markers"}}

**file_exists vs file_pattern**:
- Use `file_exists` when the exact file path matters (e.g., entry points, config files, imports by other modules).
- Use `file_pattern` when any file matching the pattern is acceptable (e.g., "a module under reporter/", "any test file").
- When using `file_exists`, the task description must tell the generator: "Create this exact file path."

**CRITICAL**: Only assign file-based criteria (file_exists, file_pattern, tests_pass, lint, coverage) to `generator` nodes.
Planner and evaluator nodes produce in-memory output (plans, feedback), NOT files.
For planner nodes, either omit success_criteria or use CUSTOM type.
For evaluator nodes, omit success_criteria entirely.

For simple cases you MAY use plain strings like "tests pass" or "lint clean" — these will be auto-parsed — but structured objects are preferred for reliability.

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
- **retry**: Retry the same node (most common for evaluation failures)
- **skip**: Skip this node and continue (if failure is acceptable)
- **abort**: Stop execution entirely (if failure is critical)
- **replan**: Create a new plan (if current plan is fundamentally flawed)

Return JSON:
{{
  "action": "retry|skip|abort|replan",
  "reasoning": "Why you chose this action..."
}}

CRITICAL RULES:
1. If the failure reason is "evaluation_failed" and the feedback contains specific,
   actionable issues (e.g. "tests failed because table not created", "missing import"),
   you MUST choose "retry". The generator agent will receive the feedback and fix
   the issues on the next attempt.
2. Choose "replan" ONLY if the task decomposition or agent assignment is wrong.
3. Choose "abort" ONLY for critical security issues or data loss risks.
4. Choose "skip" ONLY for non-critical optional nodes.

Default behavior for evaluation failures: retry.
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
      "success_criteria": [
        {{"type": "tests_pass", "description": "tests pass"}},
        {{"type": "lint", "description": "lint clean"}}
      ]
    }},
    {{
      "id": "eval_fix",
      "agent_type": "evaluator",
      "task": "Verify the corrected implementation...",
      "success_criteria": [
        {{"type": "tests_pass", "description": "tests pass"}},
        {{"type": "coverage", "target": 80, "description": "coverage 80%"}}
      ]
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
        learning_optimizer: Any | None = None,
    ):
        self.llm_config = llm_config
        self.session_store = session_store
        self.agent_registry = agent_registry
        self.learning_optimizer = learning_optimizer
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

        # M3.3: Inject learning hints if available
        if self.learning_optimizer:
            try:
                hints = self.learning_optimizer.get_planning_hints(requirement)
                if hints:
                    user_prompt += f"\n\n{hints}"
            except Exception:
                pass  # Learning hints must not break planning

        # Step 3: Call LLM for planning (with retry on JSON parse failure)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        max_retries = 2
        plan_data = None
        for attempt in range(max_retries + 1):
            response = self.llm.call(messages, tools=[])
            plan_data = self._extract_json(response.get("content", ""))
            if plan_data is not None:
                break
            if attempt < max_retries:
                messages.append({
                    "role": "assistant",
                    "content": response.get("content", ""),
                })
                messages.append({
                    "role": "user",
                    "content": (
                        "Your previous response could not be parsed as valid JSON. "
                        "Please provide the plan as a valid JSON object."
                    ),
                })

        if plan_data is None:
            last_response = response.get("content", "")
            preview = last_response[:500] if last_response else "(empty response)"
            logger.error(
                "Planning response parse failed after %d attempts. "
                "Last LLM output (first 500 chars):\n%s",
                max_retries + 1, preview,
            )
            raise ValueError(
                f"Failed to parse planning response after {max_retries + 1} retries. "
                f"LLM did not return valid JSON.\n"
                f"Last response preview: {preview}"
            )

        # Step 4: Parse and validate the plan
        plan = OrchestratorPlan(**plan_data)

        # Step 5: Validate all agent types exist in registry
        for node_def in plan.nodes:
            agent_type = node_def.get("agent_type", "")
            if not self.agent_registry.has_agent(agent_type):
                raise ValueError(
                    f"Plan references unregistered agent: {agent_type}. "
                    f"Available: {[a.id for a in self.agent_registry.list_agents()]}"
                )

        # Step 6: Structural validation & auto-fix (NEW)
        validator = PlanValidator(auto_fix=True)
        fixed_plan_data = validator.validate(plan.model_dump())
        if validator.warnings:
            for w in validator.warnings:
                print(f"[PlanValidator] {w}")
        plan = OrchestratorPlan(**fixed_plan_data)

        # Step 7: Convert to DAG
        dag = self._plan_to_dag(plan)

        return dag

    async def plan_from_template(
        self,
        template_name: str,
        variables: dict[str, str] | None = None,
    ) -> DAG:
        """
        Generate a DAG from a named template with variable substitution.

        This bypasses LLM planning for known task patterns.
        """
        registry = TemplateRegistry()
        dag = registry.instantiate(template_name, variables)

        # Validate agent types
        for nid, node in dag.nodes.items():
            if not self.agent_registry.has_agent(node.agent_type):
                raise ValueError(
                    f"Template references unregistered agent: {node.agent_type}. "
                    f"Available: {[a.id for a in self.agent_registry.list_agents()]}"
                )
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
                f"{' (FAILED: ' + node.error[:300] + ')' if node.status.value == 'failed' else ''}"
            )

        # Build adaptation prompt
        system_prompt = self.ADAPTATION_PROMPT_TEMPLATE.format(
            node_id=failed_node_id,
            agent_type=failed_node.agent_type,
            task=failed_node.task_description,
            error=failed_node.error[:2000],
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

    def _extract_json(self, text: str) -> dict | None:
        """
        Extract JSON from LLM response (handles markdown code blocks).

        Collects candidate substrings from multiple strategies, tries
        json.loads on each, and only attempts repair on truly truncated
        candidates (unclosed braces at end-of-text).

        Returns None when no valid JSON can be extracted.
        """
        text = text.strip()
        candidates: list[str] = []

        # Strategy 1: JSON inside ```json ... ``` blocks
        json_block_match = re.search(r"```json\s*\n(.*?)```", text, re.DOTALL)
        if json_block_match:
            candidates.append(json_block_match.group(1).strip())

        # Strategy 2: JSON inside generic ``` ... ``` blocks
        generic_block_match = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
        if generic_block_match:
            candidate = generic_block_match.group(1).strip()
            if candidate.startswith("{") or candidate.startswith("["):
                candidates.append(candidate)

        # Strategy 3: First top-level JSON object via brace matching
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
                    candidates.append(text[start:i + 1])
                    start = None

        # Strategy 4: Truncated JSON (unclosed braces at end-of-text)
        if start is not None and brace_depth > 0:
            candidates.append(self._repair_truncated_json(text[start:], brace_depth))

        # Try each candidate in order — first valid wins
        for candidate in candidates:
            try:
                result = json.loads(candidate)
                if isinstance(result, dict):
                    return result
            except (json.JSONDecodeError, ValueError):
                continue

        return None

    @staticmethod
    def _repair_truncated_json(text: str, brace_depth: int) -> str:
        """Attempt to close a truncated JSON object by appending missing
        closing quotes and braces.  Only handles genuine truncation
        (unclosed braces), not complete-but-malformed JSON."""
        # Close unclosed quotes (odd number of unescaped ")
        quote_count = text.count('"') - text.count('\\"')
        if quote_count % 2:
            text += '"'
        # Close unclosed braces
        text += '}' * brace_depth
        return text

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

        # 3. Call LLM (with retry on JSON parse failure)
        max_retries = 2
        plan_data = None
        for attempt in range(max_retries + 1):
            response = self.llm.call(messages, tools=[])
            plan_data = self._extract_json(response.get("content", ""))
            if plan_data is not None:
                break
            if attempt < max_retries:
                messages.append({
                    "role": "assistant",
                    "content": response.get("content", ""),
                })
                messages.append({
                    "role": "user",
                    "content": (
                        "Your previous response could not be parsed as valid JSON. "
                        "Please provide the plan as a valid JSON object."
                    ),
                })

        if plan_data is None:
            raise ValueError(
                "Failed to parse replanning response after retries. "
                "The LLM did not return valid JSON."
            )

        # 4. Parse and validate
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
