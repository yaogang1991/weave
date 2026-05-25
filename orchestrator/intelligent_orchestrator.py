"""
Intelligent Orchestrator: Thin facade delegating to Planner and Adapter.

Refactored (#919) from a monolithic 943-line class into three modules:
- orchestrator/planner.py  — DAG generation, structured output, plan→DAG conversion
- orchestrator/adapter.py  — failure adaptation and replan logic
- orchestrator/intelligent_orchestrator.py  — this file, backward-compat facade

All public APIs remain unchanged. Internal methods delegate to the
appropriate submodule while preserving the original call signatures.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from core.models import (
    DAG,
    FailureDecision,
    OrchestratorPlan,
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
from orchestrator.plan_validator import PlanValidator
from orchestrator.prompts import PromptRegistry, get_prompt_registry
from orchestrator.planner import (
    Planner as _Planner,
    _infer_fallback_edges,
    _apply_rename_map,
)
from orchestrator.adapter import (
    Adapter as _Adapter,
    _is_infrastructure_error,
    INFRASTRUCTURE_ERROR_PATTERNS,
    _KNOWN_TOOL_COMMANDS,
)

logger = logging.getLogger(__name__)

from learning.optimizer import LearningOptimizer  # noqa: E402
from skills.registry import SkillRegistry  # noqa: E402


def _is_response_truncated(content: str) -> bool:
    """Detect if planner JSON response was truncated (#621)."""
    if not content:
        return False
    stripped = content.strip()
    if not stripped.startswith("{"):
        return False
    if stripped.endswith("}"):
        return False
    return stripped.count("{") > stripped.count("}")


class IntelligentOrchestrator:
    """Orchestrator Agent: Plans DAG, monitors execution, adapts to failures.

    Delegates planning to Planner and failure handling to Adapter.
    This class is the public API — all backward-compatible methods remain.
    """

    PLANNING_PROMPT_TEMPLATE: str = ""
    ADAPTATION_PROMPT_TEMPLATE: str = ""
    REPLAN_PROMPT_TEMPLATE: str = ""

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
    _PLANNER_MAX_TOKENS = 8192
    _PLAN_TIMEOUT_RETRIES = 2

    def __init__(
        self,
        llm_config: LLMConfig,
        session_store: SessionStore,
        agent_registry: AgentRegistry,
        llm_router: LLMRouter | None = None,
        learning_optimizer: LearningOptimizer | None = None,
        prompt_registry: PromptRegistry | None = None,
        skill_registry: SkillRegistry | None = None,
    ):
        self.llm_config = llm_config
        self.session_store = session_store
        self.agent_registry = agent_registry
        self.learning_optimizer = learning_optimizer
        self.skill_registry = skill_registry
        self._token_estimator = None
        self._prompt_registry = prompt_registry or get_prompt_registry()

        if llm_router:
            self.llm = llm_router.get_client("orchestrator")
        else:
            self.llm = LLMClient(llm_config)

        self._planner = _Planner(
            llm=self.llm,
            llm_config=llm_config,
            agent_registry=agent_registry,
            prompt_registry=self._prompt_registry,
            learning_optimizer=learning_optimizer,
            skill_registry=skill_registry,
        )
        self._adapter = _Adapter(
            llm=self.llm,
            llm_config=llm_config,
            agent_registry=agent_registry,
            prompt_registry=self._prompt_registry,
            plan_to_dag_fn=self._planner._plan_to_dag,
        )

    # ------------------------------------------------------------------
    # Public API — delegates to submodules
    # ------------------------------------------------------------------

    async def plan(self, requirement: str, project_context: dict | None = None) -> DAG:
        return await self._planner.plan(requirement, project_context)

    async def plan_from_template(
        self, template_name: str, variables: dict[str, str] | None = None,
    ) -> DAG:
        return await self._planner.plan_from_template(template_name, variables)

    async def adapt_to_failure(
        self, dag: DAG, failed_node_id: str, error: str = "",
    ) -> FailureDecision:
        return await self._adapter.adapt_to_failure(dag, failed_node_id, error)

    async def replan(self, dag: DAG, failed_node_id: str, requirement: str = "") -> DAG:
        return await self._adapter.replan(dag, failed_node_id, requirement)

    # ------------------------------------------------------------------
    # Backward-compat: internal methods referenced by tests
    # ------------------------------------------------------------------

    def _plan_to_dag(self, plan: OrchestratorPlan) -> DAG:
        return self._planner._plan_to_dag(plan)

    @staticmethod
    def _infer_fallback_edges(dag: DAG) -> DAG:
        return _infer_fallback_edges(dag)

    @staticmethod
    def _apply_rename_map(dag: DAG, rename_map: dict[str, str]) -> None:
        _apply_rename_map(dag, rename_map)

    @staticmethod
    def _count_features(task_description: str) -> int:
        return PlanValidator._estimate_feature_count(task_description)

    @staticmethod
    def _is_response_truncated(content: str) -> bool:
        return _is_response_truncated(content)

    def _plan_structured_output(self, messages: list[dict]) -> dict | None:
        return self._planner._plan_structured_output(messages)

    def _plan_free_text(self, messages: list[dict]) -> dict:
        return self._planner._plan_free_text(messages)

    async def _estimate_dag_tokens(self, dag: DAG) -> DAG:
        return await self._planner._estimate_dag_tokens(dag)

    def _check_post_estimation_budget(self, dag: DAG) -> None:
        for nid, node in dag.nodes.items():
            if node.estimated_tokens > 0 and node.estimated_tokens > node.token_budget:
                logger.warning(
                    "Node '%s' estimated at %d tokens exceeds budget of %d",
                    nid, node.estimated_tokens, node.token_budget,
                )

    # -- Backward-compat: llm_utils delegation --

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
        pruned = prune_messages_for_size(messages)
        return prune_messages_for_tokens(pruned, self.llm_config.model)

    @staticmethod
    def _prune_messages_for_size_static(messages):
        pruned = prune_messages_for_size(messages)
        return prune_messages_for_tokens(pruned, "")

    def _extract_json(self, text):
        return extract_json(text)

    @staticmethod
    def _repair_truncated_json(text, brace_depth):
        return repair_truncated_json(text, brace_depth)
