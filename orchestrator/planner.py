"""DAG Planning: LLM-driven plan generation and DAG construction.

Extracted from IntelligentOrchestrator (#919) to separate planning
responsibilities from adaptation/replan logic.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from core.models import (
    DAG,
    DAGNode,
    DependencyType,
    OrchestratorPlan,
    SuccessCriterion,
)
from core.dag_models import DAGOutputModel
from core.agent_registry import AgentRegistry
from core.llm_client import LLMClient
from core.provider_health import ProviderHealthTracker, FailureCategory
from orchestrator.llm_utils import (
    truncate_requirement_if_needed,
    prune_messages_for_size,
    prune_messages_for_tokens,
    extract_json,
)
from orchestrator.plan_validator import PlanValidator, PlanValidationError
from orchestrator.prompts import PromptRegistry

logger = logging.getLogger(__name__)


class Planner:
    """Generates execution DAGs from user requirements via LLM."""

    _PLANNER_MAX_TOKENS = 8192
    _PLAN_TIMEOUT_RETRIES = 2

    def __init__(
        self,
        llm: LLMClient,
        llm_config: Any,
        agent_registry: AgentRegistry,
        prompt_registry: PromptRegistry,
        learning_optimizer: Any | None = None,
        skill_registry: Any | None = None,
        token_estimator: Any | None = None,
    ) -> None:
        self.llm = llm
        self.llm_config = llm_config
        self.agent_registry = agent_registry
        self._prompt_registry = prompt_registry
        self.learning_optimizer = learning_optimizer
        self.skill_registry = skill_registry
        self._token_estimator = token_estimator
        self._plan_health = ProviderHealthTracker()

    def _prune_messages(self, messages: list[dict]) -> list[dict]:
        pruned = prune_messages_for_size(messages)
        return prune_messages_for_tokens(pruned, self.llm_config.model)

    async def plan(self, requirement: str, project_context: dict | None = None) -> DAG:
        """Generate an execution DAG from user requirements."""
        agent_descriptions = self.agent_registry.to_prompt_description()
        planning_template = self._prompt_registry.load("planning")
        system_prompt = planning_template.format(
            agent_descriptions=agent_descriptions,
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

        plan_data = None
        last_timeout_exc = None
        provider = getattr(self.llm_config, "provider", "anthropic")
        model = getattr(self.llm_config, "model", "")
        for plan_attempt in range(self._PLAN_TIMEOUT_RETRIES + 1):
            if plan_attempt > 0 and not self._plan_health.is_healthy(
                provider, model,
            ):
                logger.warning(
                    "Provider %s/%s unhealthy after timeout, "
                    "skipping plan retry %d/%d (#934)",
                    provider, model,
                    plan_attempt + 1, self._PLAN_TIMEOUT_RETRIES + 1,
                )
                break
            try:
                plan_data = self._plan_structured_output(messages)
                if plan_data is None:
                    plan_data = self._plan_free_text(messages)
                self._plan_health.record_success(provider, model)
                break
            except TimeoutError as exc:
                last_timeout_exc = exc
                self._plan_health.record_failure(
                    provider, model, FailureCategory.UNKNOWN,
                )
                if plan_attempt < self._PLAN_TIMEOUT_RETRIES:
                    logger.warning(
                        "Plan LLM timeout (attempt %d/%d), retrying (#735): %s",
                        plan_attempt + 1, self._PLAN_TIMEOUT_RETRIES + 1,
                        str(exc)[:200],
                    )
                    continue
                raise
        if plan_data is None and last_timeout_exc is not None:
            raise last_timeout_exc

        plan = OrchestratorPlan(**plan_data)
        self._validate_agents(plan)

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
                node_resp = json.dumps(plan_data, default=str)[:2000]
                if len(node_resp) > 2000:
                    node_resp = node_resp[:2000] + "\n... (truncated)"
                messages.append({"role": "assistant", "content": node_resp})
                messages.append({
                    "role": "user",
                    "content": (
                        f"Validation error: {err_msg}. "
                        "Reduce the plan to at most 25 nodes by combining "
                        "related sub-tasks, and return a valid JSON plan."
                    ),
                })
                messages = self._prune_messages(messages)
                response = self.llm.call(
                    messages, tools=[],
                    max_tokens_override=self._PLANNER_MAX_TOKENS,
                )
                plan_data = extract_json(response.get("content", ""))
                if plan_data is None:
                    raise ValueError(
                        "Failed to parse plan after node-limit error. "
                        f"Original error: {err_msg}"
                    )
                plan = OrchestratorPlan(**plan_data)
                self._validate_agents(plan)
                fixed_plan_data = validator.validate(plan.model_dump())
            else:
                raise

        if validator.warnings:
            for w in validator.warnings:
                logger.warning("[PlanValidator] %s", w)
        plan = OrchestratorPlan(**fixed_plan_data)

        dag = self._plan_to_dag(plan)
        if validator.rename_map:
            _apply_rename_map(dag, validator.rename_map)
        dag = await self._estimate_dag_tokens(dag)
        return dag

    def _validate_agents(self, plan: OrchestratorPlan) -> None:
        for node_def in plan.nodes:
            agent_type = node_def.get("agent_type", "")
            if not self.agent_registry.has_agent(agent_type):
                raise ValueError(
                    f"Plan references unregistered agent: {agent_type}. "
                    f"Available: {[a.id for a in self.agent_registry.list_agents()]}"
                )

    def _plan_structured_output(self, messages: list[dict]) -> dict | None:
        """Attempt DAG generation via structured output (tool_use mode)."""
        dag_schema = DAGOutputModel.model_json_schema()
        structured_tool = {
            "name": "generate_dag",
            "description": (
                "Generate a DAG execution plan from the requirement. "
                "Return nodes with agent assignments, task descriptions, "
                "and dependencies."
            ),
            "input_schema": dag_schema,
        }

        try:
            messages_copy = self._prune_messages(list(messages))
            response = self.llm.call(
                messages_copy,
                tools=[structured_tool],
                tool_choice={"type": "tool", "name": "generate_dag"},
                max_tokens_override=self._PLANNER_MAX_TOKENS,
            )

            tool_calls = response.get("tool_calls", [])
            if not tool_calls:
                return None

            dag_call = tool_calls[0]
            if dag_call.get("name") != "generate_dag":
                return None

            input_data = dag_call.get("arguments", {})
            if not input_data:
                return None

            dag_model = DAGOutputModel.model_validate(input_data)
            plan_data = {
                "nodes": [
                    {
                        "id": node.id,
                        "agent_type": node.agent_type,
                        "task_description": node.task_description,
                        "dependencies": node.dependencies,
                        "backend": node.backend,
                    }
                    for node in dag_model.nodes
                ],
                "reasoning": dag_model.reasoning,
            }
            logger.info(
                "Structured output generated DAG with %d nodes (#505)",
                len(dag_model.nodes),
            )
            return plan_data

        except Exception as exc:
            logger.debug(
                "Structured output failed, falling back to free-text: %s",
                exc,
            )
            return None

    def _plan_free_text(self, messages: list[dict]) -> dict:
        """Fallback: free-text JSON parsing with retry."""
        max_retries = 2
        plan_data = None
        response = {}

        for attempt in range(max_retries + 1):
            messages = self._prune_messages(messages)
            response = self.llm.call(
                messages, tools=[],
                max_tokens_override=self._PLANNER_MAX_TOKENS,
            )
            plan_data = extract_json(response.get("content", ""))
            if plan_data is not None:
                break
            if attempt < max_retries:
                failed_content = response.get("content", "")
                is_truncated = _is_response_truncated(failed_content)
                if len(failed_content) > 2000:
                    failed_content = failed_content[:2000] + "\n... (truncated)"
                messages.append({"role": "assistant", "content": failed_content})
                if is_truncated:
                    messages.append({
                        "role": "user",
                        "content": (
                            "Your previous response was truncated. "
                            "CRITICAL: produce a SIMPLER plan with at most "
                            "6 nodes. Use very short task descriptions "
                            "(under 20 words each). Keep reasoning to one "
                            "sentence. The total response MUST be valid JSON "
                            "that fits within the output token limit."
                        ),
                    })
                    logger.info(
                        "Planner response truncated on attempt %d, "
                        "retrying with simplified prompt (#621)",
                        attempt + 1,
                    )
                else:
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
            raise ValueError(
                f"Failed to parse planning response after {max_retries + 1} retries. "
                f"LLM did not return valid JSON.\n"
                f"Last response preview: {preview}"
            )

        return plan_data

    async def plan_from_template(
        self, template_name: str, variables: dict[str, str] | None = None,
    ) -> DAG:
        """Generate a DAG from a named template with variable substitution."""
        from templates.library import TemplateRegistry
        registry = TemplateRegistry()
        dag = registry.instantiate(template_name, variables)
        for nid, node in dag.nodes.items():
            if not self.agent_registry.has_agent(node.agent_type):
                raise ValueError(
                    f"Template references unregistered agent: {node.agent_type}. "
                    f"Available: {[a.id for a in self.agent_registry.list_agents()]}"
                )
        return dag

    @staticmethod
    def _plan_to_dag(plan: OrchestratorPlan) -> DAG:
        """Convert an OrchestratorPlan to an executable DAG."""
        dag = DAG(reasoning=plan.reasoning)
        for node_def in plan.nodes:
            task_desc = (
                node_def.get("task")
                or node_def.get("task_description")
                or node_def.get("description", "")
            )
            node = DAGNode(
                id=node_def["id"],
                agent_type=node_def["agent_type"],
                task_description=task_desc,
                success_criteria=node_def.get("success_criteria", []),
                owned_files=node_def.get("owned_files", []),
                backend=node_def.get("backend"),
            )
            dag.add_node(node)

        for edge_def in plan.edges:
            dep_type_str = edge_def.get("dependency_type", "hard")
            dep_type = (
                DependencyType.SOFT if dep_type_str == "soft"
                else DependencyType.HARD
            )
            dag.add_edge(edge_def["from"], edge_def["to"], dependency_type=dep_type)

        if not plan.edges:
            dag = _infer_fallback_edges(dag)
        return dag

    async def _estimate_dag_tokens(self, dag: DAG) -> DAG:
        """Estimate token counts for all planned nodes (M4.6)."""
        if not self._token_estimator:
            return dag
        from core.token_estimator import build_node_context
        from agent.prompts import SYSTEM_PROMPTS
        nodes = [(nid, build_node_context(node, SYSTEM_PROMPTS))
                 for nid, node in dag.nodes.items()]
        if not nodes:
            return dag
        estimates = await self._token_estimator.estimate_nodes_batch(nodes)
        for est in estimates:
            if est.node_id in dag.nodes:
                dag.update_node(est.node_id, estimated_tokens=est.estimated_tokens)
        for nid, node in dag.nodes.items():
            if node.estimated_tokens > 0 and node.estimated_tokens > node.token_budget:
                logger.warning(
                    "Node '%s' estimated at %d tokens exceeds budget of %d",
                    nid, node.estimated_tokens, node.token_budget,
                )
        return dag


# -- Module-level helpers (no circular imports) --


def _is_response_truncated(content: str) -> bool:
    if not content:
        return False
    stripped = content.strip()
    if not stripped.startswith("{"):
        return False
    if stripped.endswith("}"):
        return False
    return stripped.count("{") > stripped.count("}")


def _infer_fallback_edges(dag: DAG) -> DAG:
    """Infer dependency edges when LLM produces an empty edge list (#689)."""
    planner_ids = [nid for nid, n in dag.nodes.items() if n.agent_type == "planner"]
    generator_ids = [nid for nid, n in dag.nodes.items() if n.agent_type == "generator"]
    evaluator_ids = [nid for nid, n in dag.nodes.items() if n.agent_type == "evaluator"]
    existing = {(e.from_node, e.to_node) for e in dag.edges}

    for pid in planner_ids:
        for nid, node in dag.nodes.items():
            if nid != pid and node.agent_type != "planner" and (pid, nid) not in existing:
                dag.add_edge(pid, nid)
                existing.add((pid, nid))

    non_gen_non_plan = [nid for nid, n in dag.nodes.items()
                        if n.agent_type not in ("generator", "planner")]
    for gid in generator_ids:
        for tid in non_gen_non_plan:
            if (gid, tid) not in existing:
                dag.add_edge(gid, tid)
                existing.add((gid, tid))

    for gid in generator_ids:
        for eid in evaluator_ids:
            if (gid, eid) not in existing:
                dag.add_edge(gid, eid)
                existing.add((gid, eid))

    if existing:
        logger.warning(
            "LLM produced empty edges — inferred %d fallback edges (#689): %s",
            len(existing),
            ", ".join(f"{f}→{t}" for f, t in existing),
        )
    return dag


def _apply_rename_map(dag: DAG, rename_map: dict[str, str]) -> None:
    """Update criterion paths and task descriptions on stdlib rename (#422)."""
    for node in dag.nodes.values():
        updated: list[str | SuccessCriterion] = []
        for crit in node.success_criteria:
            if isinstance(crit, SuccessCriterion):
                new_path, new_pattern = crit.path, crit.pattern
                for old, new in rename_map.items():
                    new_path = new_path.replace(f"/{old}/", f"/{new}/").replace(f"/{old}.", f"/{new}.")
                    new_pattern = new_pattern.replace(f"/{old}/", f"/{new}/").replace(f"/{old}.", f"/{new}.")
                if new_path != crit.path or new_pattern != crit.pattern:
                    crit = crit.model_copy(update={"path": new_path, "pattern": new_pattern})
                updated.append(crit)
            elif isinstance(crit, str):
                s = crit
                for old, new in rename_map.items():
                    s = s.replace(f"/{old}/", f"/{new}/").replace(f"/{old}.", f"/{new}.")
                updated.append(s)
            else:
                updated.append(crit)
        node.success_criteria = updated

        for old, new in rename_map.items():
            task = node.task_description
            result, i, target = [], 0, f"{old}.py"
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
