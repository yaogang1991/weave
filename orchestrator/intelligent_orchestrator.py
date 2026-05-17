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
from typing import Any

from core.models import (
    DAG,
    DAGNode,
    DependencyType,
    FailureDecision,
    OrchestratorPlan,
    SuccessCriterion,
)
from core.agent_registry import AgentRegistry
from core.config import LLMConfig
from core.llm_client import LLMClient
from core.llm_router import LLMRouter
from session.store import SessionStore
from orchestrator.llm_utils import (
    estimate_tokens,
    get_context_window,
    truncate_requirement_if_needed,
    estimate_messages_bytes,
    prune_messages_for_size,
    prune_messages_for_tokens,
    extract_json,
    repair_truncated_json,
)
from orchestrator.plan_validator import PlanValidator, PlanValidationError
from orchestrator.prompts import PromptRegistry, get_prompt_registry

logger = logging.getLogger(__name__)
from templates.library import TemplateRegistry


# Infrastructure errors cannot be fixed by retrying with the same environment.
# Detect early and abort instead of wasting retry budget (#187).
INFRASTRUCTURE_ERROR_PATTERNS: list[str] = [
    "no linter available",
    "pytest not installed",
    "no python interpreter",
    "permission denied",
    "connection refused",
    "connection timed out",
]

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
    """Check whether an error is an infrastructure/environment issue."""
    if not error:
        return False
    lower = error.lower()
    if any(pattern in lower for pattern in INFRASTRUCTURE_ERROR_PATTERNS):
        return True
    if "command not found" in lower:
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

    PLANNING_PROMPT_TEMPLATE: str = ""
    ADAPTATION_PROMPT_TEMPLATE: str = ""
    REPLAN_PROMPT_TEMPLATE: str = ""

    # Re-export constants for backward compat (tests reference these)
    _MODEL_CONTEXT_WINDOWS = {
        k: v for k, v in (
            ("claude-sonnet-4-6", 200_000),
            ("claude-opus-4-6", 200_000),
            ("claude-haiku-4-5", 200_000),
            ("gpt-4o", 128_000),
            ("gpt-4-turbo", 128_000),
            ("o1", 200_000),
            ("o3", 200_000),
            ("o4-mini", 200_000),
            ("kimi", 262_144),
            ("moonshot", 262_144),
        )
    }
    _DEFAULT_CONTEXT_WINDOW = 200_000
    _CHARS_PER_TOKEN = 3.5
    _MAX_MESSAGE_BYTES = 2_097_152
    _PRUNE_THRESHOLD = 0.60

    def __init__(
        self,
        llm_config: LLMConfig,
        session_store: SessionStore,
        agent_registry: AgentRegistry,
        llm_router: LLMRouter | None = None,
        learning_optimizer: Any | None = None,
        prompt_registry: PromptRegistry | None = None,
        skill_registry: Any | None = None,
    ):
        self.llm_config = llm_config
        self.session_store = session_store
        self.agent_registry = agent_registry
        self.learning_optimizer = learning_optimizer
        self.skill_registry = skill_registry
        self._prompt_registry = prompt_registry or get_prompt_registry()
        if llm_router:
            self.llm = llm_router.get_client("orchestrator")
        else:
            self.llm = LLMClient(llm_config)

    async def plan(self, requirement: str, project_context: dict | None = None) -> DAG:
        """Generate an execution DAG from user requirements."""
        agent_descriptions = self.agent_registry.to_prompt_description()

        planning_template = self._prompt_registry.load("planning")
        system_prompt = planning_template.format(
            agent_descriptions=agent_descriptions
        )

        requirement = truncate_requirement_if_needed(
            requirement, system_prompt,
            json.dumps(project_context, default=str) if project_context else None,
            self.llm_config.model,
        )

        user_prompt = f"User requirement: {requirement}"
        if project_context:
            existing = project_context.pop("existing_files", None)
            user_prompt += (
                f"\n\nProject context: "
                f"{json.dumps(project_context, indent=2, default=str)}"
            )
            if existing:
                user_prompt += (
                    "\n\n## Existing Workspace Files\n"
                    "The following files already exist in the workspace. "
                    "You MUST reconcile with them (reuse, edit, or replace) "
                    "instead of creating duplicates:\n"
                )
                for f in existing:
                    user_prompt += f"- [{f['type']}] {f['path']}\n"
                project_context["existing_files"] = existing

            retry_attempt = project_context.get("retry_attempt", 0)
            if retry_attempt > 0:
                user_prompt += (
                    f"\n\n## Retry Context\n"
                    f"This is **attempt {retry_attempt + 1}** of "
                    f"{project_context.get('max_attempts', '?')}.\n"
                    f"A previous attempt failed/timed out. "
                    f"{project_context.get('existing_file_count', 0)} "
                    f"files already exist in the workspace from prior work.\n"
                    f"**DO NOT start from scratch.** Build upon existing files. "
                    f"Only create files that don't exist yet. "
                    f"Only plan work that hasn't been completed.\n"
                )

        if self.learning_optimizer:
            try:
                hints = self.learning_optimizer.get_planning_hints(requirement)
                if hints:
                    user_prompt += f"\n\n{hints}"
            except Exception:
                pass

        if self.skill_registry:
            try:
                skills_desc = self.skill_registry.to_prompt_description()
                if skills_desc:
                    user_prompt += f"\n\n{skills_desc}"
            except Exception:
                pass

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        max_retries = 2
        plan_data = None
        for attempt in range(max_retries + 1):
            messages = self._prune_messages_for_size(messages)
            response = self.llm.call(messages, tools=[])
            plan_data = extract_json(response.get("content", ""))
            if plan_data is not None:
                break
            if attempt < max_retries:
                failed_content = response.get("content", "")
                if len(failed_content) > 2000:
                    failed_content = (
                        failed_content[:2000]
                        + "\n... (truncated for token limit)"
                    )
                messages.append({
                    "role": "assistant",
                    "content": failed_content,
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

        plan = OrchestratorPlan(**plan_data)

        for node_def in plan.nodes:
            agent_type = node_def.get("agent_type", "")
            if not self.agent_registry.has_agent(agent_type):
                raise ValueError(
                    f"Plan references unregistered agent: {agent_type}. "
                    f"Available: {[a.id for a in self.agent_registry.list_agents()]}"
                )

        validator = PlanValidator(auto_fix=True)
        try:
            fixed_plan_data = validator.validate(plan.model_dump())
        except PlanValidationError as e:
            err_msg = str(e)
            if "nodes" in err_msg.lower() and "maximum" in err_msg.lower():
                logger.warning(
                    "Plan has too many nodes, retrying with constraint: %s",
                    err_msg,
                )
                node_resp = response.get("content", "")
                if len(node_resp) > 2000:
                    node_resp = (
                        node_resp[:2000]
                        + "\n... (truncated for message size limit)"
                    )
                messages.append({
                    "role": "assistant",
                    "content": node_resp,
                })
                messages.append({
                    "role": "user",
                    "content": (
                        f"Validation error: {err_msg}. "
                        "Reduce the plan to at most 10 nodes by combining "
                        "related sub-tasks, and return a valid JSON plan."
                    ),
                })
                messages = self._prune_messages_for_size(messages)
                response = self.llm.call(messages, tools=[])
                plan_data = extract_json(response.get("content", ""))
                if plan_data is None:
                    raise ValueError(
                        "Failed to parse replan response after node-limit error. "
                        f"Original error: {err_msg}"
                    )
                plan = OrchestratorPlan(**plan_data)
                for node_def in plan.nodes:
                    agent_type = node_def.get("agent_type", "")
                    if not self.agent_registry.has_agent(agent_type):
                        raise ValueError(
                            f"Plan references unregistered agent: {agent_type}. "
                            f"Available: {[a.id for a in self.agent_registry.list_agents()]}"
                        )
                fixed_plan_data = validator.validate(plan.model_dump())
            else:
                raise

        if validator.warnings:
            for w in validator.warnings:
                print(f"[PlanValidator] {w}")
        plan = OrchestratorPlan(**fixed_plan_data)

        dag = self._plan_to_dag(plan)

        if validator.rename_map:
            self._apply_rename_map(dag, validator.rename_map)

        return dag

    async def plan_from_template(
        self,
        template_name: str,
        variables: dict[str, str] | None = None,
    ) -> DAG:
        """Generate a DAG from a named template with variable substitution."""
        registry = TemplateRegistry()
        dag = registry.instantiate(template_name, variables)

        for nid, node in dag.nodes.items():
            if not self.agent_registry.has_agent(node.agent_type):
                raise ValueError(
                    f"Template references unregistered agent: {node.agent_type}. "
                    f"Available: {[a.id for a in self.agent_registry.list_agents()]}"
                )
        return dag

    async def adapt_to_failure(self, dag: DAG, failed_node_id: str, error: str = "") -> FailureDecision:
        """Handle a failed node by asking the orchestrator LLM to decide."""
        failed_node = dag.nodes[failed_node_id]

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

        is_zero_output = "zero output" in node_error.lower()
        if is_zero_output and failed_node.agent_type == "generator":
            feature_count = self._count_features(failed_node.task_description)
            if feature_count > 3:
                logger.info(
                    "Node %s: zero output with %d features, auto-replanning",
                    failed_node_id, feature_count,
                )
                return FailureDecision(
                    action="replan",
                    reasoning=(
                        f"Node produced zero output artifacts with {feature_count} "
                        f"distinct features. Task is too complex for a single node. "
                        f"Split into 2-3 smaller nodes with shared foundation."
                    ),
                )

        dag_status = []
        for nid, node in dag.nodes.items():
            dag_status.append(
                f"- {nid}: {node.agent_type} = {node.status.value}"
                f"{' (FAILED: ' + node.error[:300] + ')' if node.status.value == 'failed' else ''}"
            )

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
            decision_data = extract_json(response.get("content", ""))
            return FailureDecision(**decision_data)
        except Exception:
            if failed_node.retry_count < failed_node.max_retries:
                return FailureDecision(action="retry", reasoning="Parse error, defaulting to retry")
            if has_only_soft_dependents and dependents:
                return FailureDecision(
                    action="skip",
                    reasoning="Parse error; all downstream deps are soft, skipping failed node",
                )
            return FailureDecision(action="abort", reasoning="Parse error, max retries reached")

    @staticmethod
    def _count_features(task_description: str) -> int:
        """Count distinct complex features in a task description (#409)."""
        return PlanValidator._estimate_feature_count(task_description)

    def _plan_to_dag(self, plan: OrchestratorPlan) -> DAG:
        """Convert an OrchestratorPlan to an executable DAG."""
        dag = DAG(reasoning=plan.reasoning)

        for node_def in plan.nodes:
            node = DAGNode(
                id=node_def["id"],
                agent_type=node_def["agent_type"],
                task_description=node_def["task"],
                success_criteria=node_def.get("success_criteria", []),
                owned_files=node_def.get("owned_files", []),
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
        """Update criterion paths and task descriptions when stdlib shadowing
        triggered a rename (#422)."""
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

            for old, new in rename_map.items():
                task = node.task_description
                result = []
                i = 0
                target = f"{old}.py"
                while i < len(task):
                    pos = task.find(target, i)
                    if pos == -1:
                        result.append(task[i:])
                        break
                    if pos == 0 or not (task[pos - 1].isalnum() or task[pos - 1] == '_'):
                        result.append(task[i:pos])
                        result.append(f"{new}.py")
                        i = pos + len(target)
                    else:
                        result.append(task[i:pos + len(target)])
                        i = pos + len(target)
                node.task_description = "".join(result)

    # -- Delegation to llm_utils for backward compat --

    def _estimate_tokens(self, text: str) -> int:
        return estimate_tokens(text)

    def _get_context_window(self) -> int:
        return get_context_window(self.llm_config.model)

    def _truncate_requirement_if_needed(self, requirement, system_prompt, project_context):
        return truncate_requirement_if_needed(
            requirement, system_prompt,
            json.dumps(project_context, default=str) if project_context else None,
            self.llm_config.model,
        )

    @staticmethod
    def _estimate_messages_bytes(messages):
        return estimate_messages_bytes(messages)

    def _prune_messages_for_size(self, messages):
        # Byte-based pruning
        pruned = prune_messages_for_size(messages)
        # Then token-based pruning
        return prune_messages_for_tokens(pruned, self.llm_config.model)

    def _extract_json(self, text):
        return extract_json(text)

    @staticmethod
    def _repair_truncated_json(text, brace_depth):
        return repair_truncated_json(text, brace_depth)

    async def replan(self, dag: DAG, failed_node_id: str, requirement: str = "") -> DAG:
        """Re-plan the remaining work after a node failure."""
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

        max_retries = 2
        plan_data = None
        for attempt in range(max_retries + 1):
            messages = self._prune_messages_for_size(messages)
            response = self.llm.call(messages, tools=[])
            plan_data = extract_json(response.get("content", ""))
            if plan_data is not None:
                break
            if attempt < max_retries:
                replan_resp = response.get("content", "")
                if len(replan_resp) > 2000:
                    replan_resp = (
                        replan_resp[:2000]
                        + "\n... (truncated for message size limit)"
                    )
                messages.append({
                    "role": "assistant",
                    "content": replan_resp,
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

        plan = OrchestratorPlan(**plan_data)

        for node_def in plan.nodes:
            agent_type = node_def.get("agent_type", "")
            if not self.agent_registry.has_agent(agent_type):
                raise ValueError(
                    f"Replan references unregistered agent: {agent_type}. "
                    f"Available: {[a.id for a in self.agent_registry.list_agents()]}"
                )

        new_dag = self._plan_to_dag(plan)
        return new_dag
