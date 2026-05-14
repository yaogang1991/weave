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
    DependencyType,
    FailureDecision,
    AgentCapability,
    OrchestratorPlan,
    SuccessCriterion,
)
from core.agent_registry import AgentRegistry
from core.config import LLMConfig
from core.llm_client import LLMClient
from core.llm_router import LLMRouter
from session.store import SessionStore
from orchestrator.plan_validator import PlanValidator, PlanValidationError
from orchestrator.prompts import PromptRegistry, get_prompt_registry

logger = logging.getLogger(__name__)
from templates.library import TemplateRegistry


# Infrastructure errors cannot be fixed by retrying with the same environment.
# Detect early and abort instead of wasting retry budget (#187).
INFRASTRUCTURE_ERROR_PATTERNS: list[str] = [
    # Explicit tool/command missing — always infra
    "no linter available",
    "pytest not installed",
    "no python interpreter",
    # Network / permission — always infra
    "permission denied",
    "connection refused",
    "connection timed out",
]

# Known infrastructure tool commands — only "command not found" for these
# counts as an infrastructure error.  Other missing commands (project CLIs,
# make targets, etc.) may be fixable by the agent via retry.
_KNOWN_TOOL_COMMANDS: list[str] = [
    "python",
    "python3",
    "pytest",
    "flake8",
    "ruff",
    "autopep8",
    "pip",
    "node",
    "npm",
    "git",
]


def _is_infrastructure_error(error: str) -> bool:
    """Check whether an error is an infrastructure/environment issue
    that cannot be resolved by retrying."""
    if not error:
        return False
    lower = error.lower()
    if any(pattern in lower for pattern in INFRASTRUCTURE_ERROR_PATTERNS):
        return True
    # Check for "command not found" only when it refers to a known tool.
    if "command not found" in lower:
        # Match both "bash: python: command not found" and "command not found: python"
        return any(
            f"{tool}: command not found" in lower
            or f"command not found: {tool}" in lower
            for tool in _KNOWN_TOOL_COMMANDS
        )
    return False


class IntelligentOrchestrator:
    """
    Orchestrator Agent: Plans DAG, monitors execution, adapts to failures.
    
    This is itself an LLM-driven agent, not a hardcoded state machine.
    It queries the AgentRegistry to discover available workers,
    then uses LLM reasoning to decompose tasks and build execution DAGs.
    """

    PLANNING_PROMPT_TEMPLATE: str = ""  # Loaded from file via PromptRegistry
    ADAPTATION_PROMPT_TEMPLATE: str = ""  # Loaded from file via PromptRegistry
    REPLAN_PROMPT_TEMPLATE: str = ""  # Loaded from file via PromptRegistry

    def __init__(
        self,
        llm_config: LLMConfig,
        session_store: SessionStore,
        agent_registry: AgentRegistry,
        llm_router: LLMRouter | None = None,
        learning_optimizer: Any | None = None,
        prompt_registry: PromptRegistry | None = None,
    ):
        self.llm_config = llm_config
        self.session_store = session_store
        self.agent_registry = agent_registry
        self.learning_optimizer = learning_optimizer
        self._prompt_registry = prompt_registry or get_prompt_registry()
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
        planning_template = self._prompt_registry.load("planning")
        system_prompt = planning_template.format(
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

        # Step 8: Apply stdlib rename map to criterion paths (#285)
        if validator.rename_map:
            self._apply_rename_map(dag, validator.rename_map)

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

        # Infrastructure errors (missing tools, broken env) are never fixable
        # by retrying — abort immediately instead of wasting retry budget (#187).
        node_error = failed_node.error or error
        if _is_infrastructure_error(node_error):
            logger.warning(
                "Node %s failed with infrastructure error, aborting: %s",
                failed_node_id, node_error[:200],
            )
            return FailureDecision(
                action="abort",
                reasoning=f"Infrastructure error (not retryable): {node_error[:200]}",
            )

        # Build DAG status summary
        dag_status = []
        for nid, node in dag.nodes.items():
            dag_status.append(
                f"- {nid}: {node.agent_type} = {node.status.value}"
                f"{' (FAILED: ' + node.error[:300] + ')' if node.status.value == 'failed' else ''}"
            )

        # Topology awareness: describe downstream dependency types (#271)
        dependents = dag.get_dependents(failed_node_id)
        downstream_info: list[str] = []
        has_only_soft_dependents = True
        for dep_id in dependents:
            dep_edges = [e for e in dag.edges
                         if e.from_node == failed_node_id and e.to_node == dep_id]
            for edge in dep_edges:
                dtype = edge.dependency_type.value
                downstream_info.append(f"  → {dep_id} ({dtype} dependency)")
                if edge.dependency_type == DependencyType.HARD:
                    has_only_soft_dependents = False
        if downstream_info:
            dag_status.append(f"Downstream from {failed_node_id}:")
            dag_status.extend(downstream_info)

        # Build adaptation prompt
        system_prompt = self._prompt_registry.load("adaptation").format(
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
            # Topology-aware fallback: soft-only dependents → skip, not abort (#271)
            if failed_node.retry_count < failed_node.max_retries:
                return FailureDecision(action="retry", reasoning="Parse error, defaulting to retry")
            if has_only_soft_dependents and dependents:
                return FailureDecision(
                    action="skip",
                    reasoning="Parse error; all downstream deps are soft, skipping failed node",
                )
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
            dep_type_str = edge_def.get("dependency_type", "hard")
            dep_type = (
                DependencyType.SOFT if dep_type_str == "soft"
                else DependencyType.HARD
            )
            dag.add_edge(edge_def["from"], edge_def["to"], dependency_type=dep_type)

        return dag

    @staticmethod
    def _apply_rename_map(dag: DAG, rename_map: dict[str, str]) -> None:
        """Update criterion paths in-place when stdlib shadowing triggered a rename.

        For each node, scan success_criteria for file_exists / file_pattern
        paths that contain a stdlib-conflicting name and replace it with the
        prefixed alternative.  Also rewrites plain-string criteria that
        contain the conflicting name as a path segment.
        """
        for node in dag.nodes.values():
            updated: list[str | SuccessCriterion] = []
            for crit in node.success_criteria:
                if isinstance(crit, SuccessCriterion):
                    new_path = crit.path
                    new_pattern = crit.pattern
                    for old, new in rename_map.items():
                        new_path = new_path.replace(f"/{old}/", f"/{new}/")
                        new_path = new_path.replace(f"/{old}.", f"/{new}.")
                        new_pattern = new_pattern.replace(f"/{old}/", f"/{new}/")
                        new_pattern = new_pattern.replace(f"/{old}.", f"/{new}.")
                    if new_path != crit.path or new_pattern != crit.pattern:
                        crit = crit.model_copy(update={
                            "path": new_path,
                            "pattern": new_pattern,
                        })
                    updated.append(crit)
                elif isinstance(crit, str):
                    s = crit
                    for old, new in rename_map.items():
                        s = s.replace(f"/{old}/", f"/{new}/")
                        s = s.replace(f"/{old}.", f"/{new}.")
                    updated.append(s)
                else:
                    updated.append(crit)
            node.success_criteria = updated

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
        system_prompt = self._prompt_registry.load("replan").format(
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
