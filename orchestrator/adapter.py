"""Failure Adaptation: adapt_to_failure and replan logic.

Extracted from IntelligentOrchestrator (#919) to separate adaptation
responsibilities from planning logic.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from core.models import DAG, FailureDecision, OrchestratorPlan
from core.agent_registry import AgentRegistry
from core.llm_client import LLMClient
from orchestrator.llm_utils import (
    prune_messages_for_size,
    prune_messages_for_tokens,
    extract_json,
)
from orchestrator.prompts import PromptRegistry
from orchestrator.plan_validator import PlanValidator

logger = logging.getLogger(__name__)

INFRASTRUCTURE_ERROR_PATTERNS: list[str] = [
    "no linter available", "pytest not installed",
    "no python interpreter", "permission denied",
    "connection refused", "connection timed out",
]

_KNOWN_TOOL_COMMANDS: list[str] = [
    "python", "python3", "pytest", "flake8", "ruff",
    "autopep8", "pip", "node", "npm", "git",
]

_PLANNER_MAX_TOKENS = 8192


def _is_infrastructure_error(error: str) -> bool:
    if not error:
        return False
    lower = error.lower()
    if any(p in lower for p in INFRASTRUCTURE_ERROR_PATTERNS):
        return True
    if "command not found" in lower:
        return any(
            f"{t}: command not found" in lower or f"command not found: {t}" in lower
            for t in _KNOWN_TOOL_COMMANDS
        )
    return False


class Adapter:
    """Handles failure adaptation and replanning via LLM."""

    def __init__(
        self,
        llm: LLMClient,
        llm_config: Any,
        agent_registry: AgentRegistry,
        prompt_registry: PromptRegistry,
        plan_to_dag_fn: Any,
    ) -> None:
        self.llm = llm
        self.llm_config = llm_config
        self.agent_registry = agent_registry
        self._prompt_registry = prompt_registry
        self._plan_to_dag = plan_to_dag_fn

    def _prune_messages(self, messages: list[dict]) -> list[dict]:
        pruned = prune_messages_for_size(messages)
        return prune_messages_for_tokens(pruned, self.llm_config.model)

    async def adapt_to_failure(
        self, dag: DAG, failed_node_id: str, error: str = "",
    ) -> FailureDecision:
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
            feature_count = PlanValidator._estimate_feature_count(
                failed_node.task_description,
            )
            if feature_count > 3:
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

        from core.models import DependencyType
        dependents = dag.get_dependents(failed_node_id)
        has_only_soft_dependents = True
        downstream_info: list[str] = []
        for dep_id in dependents:
            for edge in dag.edges:
                if edge.from_node == failed_node_id and edge.to_node == dep_id:
                    downstream_info.append(
                        f"  → {dep_id} ({edge.dependency_type.value} dependency)"
                    )
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

        if failed_node.retry_count >= failed_node.max_retries:
            messages.append({
                "role": "user",
                "content": (
                    "CRITICAL: This node has exhausted ALL retries "
                    f"({failed_node.retry_count}/{failed_node.max_retries}). "
                    "Do NOT recommend 'retry'. Choose from: replan, skip, abort."
                ),
            })

        response = self.llm.call(messages, tools=[], max_tokens_override=_PLANNER_MAX_TOKENS)

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

    async def replan(self, dag: DAG, failed_node_id: str, requirement: str = "") -> DAG:
        """Re-plan the remaining work after a node failure."""
        executed_summary: list[dict[str, Any]] = []
        for nid, node in dag.nodes.items():
            if node.status.value in ("success", "failed"):
                entry: dict[str, Any] = {
                    "id": nid,
                    "agent_type": node.agent_type,
                    "status": node.status.value,
                    "task": node.task_description[:100],
                    "result_summary": (
                        node.result.get("summary", "")[:200] if node.result else ""
                    ),
                }
                if node.output_artifacts:
                    if len(node.output_artifacts) > 30:
                        entry["output_artifacts"] = (
                            node.output_artifacts[:30]
                            + [f"...and {len(node.output_artifacts) - 30} more"]
                        )
                    else:
                        entry["output_artifacts"] = node.output_artifacts
                executed_summary.append(entry)

        failed_error = (
            dag.nodes[failed_node_id].error[:500]
            if failed_node_id in dag.nodes else ""
        )
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
            messages = self._prune_messages(messages)
            response = self.llm.call(
                messages, tools=[], max_tokens_override=_PLANNER_MAX_TOKENS,
            )
            plan_data = extract_json(response.get("content", ""))
            if plan_data is not None:
                break
            if attempt < max_retries:
                replan_resp = response.get("content", "")
                if len(replan_resp) > 2000:
                    replan_resp = replan_resp[:2000] + "\n... (truncated)"
                messages.append({"role": "assistant", "content": replan_resp})
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

        # Soften hub-and-spoke dependencies to prevent cascade skip (#959).
        validator = PlanValidator()
        plan_dump = plan.model_dump()
        plan_dump["edges"] = validator._soften_hub_dependencies(
            plan_dump.get("nodes", []),
            plan_dump.get("edges", []),
        )
        plan.edges = plan_dump["edges"]
        for w in validator.warnings:
            logger.warning("[ReplanValidator] %s", w)

        return self._plan_to_dag(plan)
