"""
ExecutionFactory — extracted from RunService (#177 PR3).

Builds the object graph for DAG execution: IntelligentOrchestrator,
DAGExecutionEngine, AgentPool, Guardrails, ToolRegistry, EvaluatorEngine.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from core.config import WeaveConfig, LLMConfig, WatchdogConfig
from core.dag_engine import DAGExecutionEngine
from core.agent_registry import AgentRegistry
from core.models import EventType, PersonalGuardrailPolicy
from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
from agent.agent_pool import AgentPool
from agent.backends.registry import BackendRegistry
from session.store import SessionStore
from tools.registry import ToolRegistry
from guardrails.policy import (
    Guardrails,
    GuardrailPolicy,
    PermissionMode,
    PersonalGuardrails,
)
from evaluator.engine import EvaluatorEngine

logger = logging.getLogger(__name__)


class ExecutionFactory:
    """Creates and wires the execution object graph.

    Extracted from RunService so that the factory logic is independently
    testable and RunService focuses on orchestration lifecycle.
    """

    def __init__(
        self,
        llm_config: LLMConfig,
        max_parallel: int,
        agent_timeout: int,
        max_context_tokens: int,
        max_iterations: int,
        artifact_path: str,
        non_interactive: bool,
        watchdog_config: WatchdogConfig,
        hooks: list[Any],
        approval_repo: Any | None = None,
        policy: GuardrailPolicy | None = None,
        budget_manager: Any | None = None,
    ) -> None:
        self._llm_config = llm_config
        self._max_parallel = max_parallel
        self._agent_timeout = agent_timeout
        self._max_context_tokens = max_context_tokens
        self._max_iterations = max_iterations
        self._artifact_path = artifact_path
        self._non_interactive = non_interactive
        self._watchdog_config = watchdog_config
        self._hooks = hooks
        self._budget_manager = budget_manager
        self._approval_repo = approval_repo
        self._policy = policy

    def create_orchestrator(self, store: SessionStore) -> IntelligentOrchestrator:
        """Build an IntelligentOrchestrator with default registries."""
        registry = AgentRegistry()
        # Get learning optimizer from LearningHook (if registered)
        learning_optimizer = None
        for hook in self._hooks:
            if hasattr(hook, "optimizer"):
                learning_optimizer = hook.optimizer
                break
        return IntelligentOrchestrator(
            llm_config=self._llm_config,
            session_store=store,
            agent_registry=registry,
            llm_router=getattr(self, "_llm_router", None),
            learning_optimizer=learning_optimizer,
        )

    def create_execution_engine(
        self,
        session_id: str,
        store: SessionStore,
        replan_handler: Any | None = None,
        work_dir: Path | None = None,
        memory_manager: Any | None = None,
        job_id: str = "",
        approval_repo: Any | None = None,
        run_id: str | None = None,
        backend_manager: Any | None = None,
        project_dir: str | None = None,
    ) -> DAGExecutionEngine:
        """Build a DAGExecutionEngine with agent pool, failure handler,
        and optional replan handler."""
        registry = AgentRegistry()

        # Wire sandbox through SyncSandboxAdapter if backend_manager
        # is available (#179 PR3)
        sandbox_runner = None
        if backend_manager is not None and getattr(backend_manager, "sandbox", None) is not None:
            from tools.command_runner import SyncSandboxAdapter
            sandbox_runner = SyncSandboxAdapter(backend_manager.sandbox)

        tool_registry = ToolRegistry(
            base_cwd=str(work_dir) if work_dir is not None else None,
            sandbox_runner=sandbox_runner,
        )

        # Default guardrails: non-interactive → DONT_ASK + built-in tool whitelist
        if self._policy is not None:
            policy = self._policy
        else:
            project_guardrails = self.load_project_guardrails(work_dir)
            if self._non_interactive:
                default_mode = PermissionMode.DONT_ASK
                default_allowed = ["read", "write", "edit", "bash", "glob", "grep", "git"]
            else:
                default_mode = PermissionMode.ACCEPT_EDITS
                default_allowed = []
            policy = GuardrailPolicy(
                mode=project_guardrails.get("permission_mode", default_mode),
                auto_approve_read=project_guardrails.get("auto_approve_read", True),
                allowed_tools=project_guardrails.get("allowed_tools", default_allowed),
                denied_commands=project_guardrails.get("denied_commands", []),
                max_iterations=self._max_iterations,
            )

        # If policy is PersonalGuardrailPolicy, use PersonalGuardrails
        if isinstance(policy, PersonalGuardrailPolicy):
            guardrails = PersonalGuardrails(
                policy,
                tool_registry,
                non_interactive=self._non_interactive,
                approval_repo=self._approval_repo,
                project_dir=project_dir,
            )
        else:
            guardrails = Guardrails(policy, tool_registry, project_dir=project_dir)

        pool = AgentPool(
            llm_config=self._llm_config,
            session_store=store,
            agent_registry=registry,
            tool_registry=tool_registry,
            guardrails=guardrails,
            max_iterations=self._max_iterations,
            timeout=self._agent_timeout,
            max_context_tokens=self._max_context_tokens,
            llm_router=getattr(self, "_llm_router", None),
            memory_manager=memory_manager,
            job_id=job_id,
            approval_repo=approval_repo,
            run_id=run_id,
        )

        # Orchestrator for failure handling
        orchestrator = self.create_orchestrator(store)

        # Evaluator for quality gates
        _cfg = WeaveConfig.from_env()
        evaluator = EvaluatorEngine(
            session_store=store,
            pass_threshold=_cfg.pass_threshold,
            auto_format_before_eval=_cfg.auto_format_before_eval,
        )

        # M4.0: Create BackendRegistry wrapping the AgentPool
        backend_registry = BackendRegistry(pool=pool, session_id=session_id)

        # M4.1: Register ClaudeCodeBackend if enabled
        if _cfg.claude_code.enabled:
            from agent.backends.claude_code import (
                ClaudeCodeBackend,
                ClaudeCodeConfig as RuntimeConfig,
            )
            cc_config = RuntimeConfig.from_core_config(_cfg.claude_code)
            backend_registry.register(
                "claude_code", ClaudeCodeBackend(config=cc_config),
            )

        engine = DAGExecutionEngine(
            agent_executor=pool.get_executor(session_id),
            failure_handler=orchestrator.adapt_to_failure,
            replan_handler=replan_handler,
            max_parallel=self._max_parallel,
            evaluator=evaluator,
            artifact_path=self._artifact_path,
            work_dir=str(work_dir) if work_dir else None,
            memory_manager=memory_manager,
            session_id=session_id,
            heartbeat_interval_sec=self._watchdog_config.heartbeat_interval_sec,
            heartbeat_miss_threshold=self._watchdog_config.heartbeat_miss_threshold,
            enable_watchdog=self._watchdog_config.enabled,
            watchdog_overrides={
                agent_type: (ov.heartbeat_interval_sec, ov.heartbeat_miss_threshold)
                for agent_type, ov in self._watchdog_config.agent_overrides.items()
                if ov.heartbeat_interval_sec is not None
                and ov.heartbeat_miss_threshold is not None
            },
            alert_thresholds={
                agent_type: self._watchdog_config.alert_threshold_for(agent_type)
                for agent_type in self._watchdog_config.agent_overrides
            },
            node_timeout_config=_cfg.node_timeout,
            backend_manager=backend_manager,
            job_id=job_id,
            run_id=run_id or "",
            backend_registry=backend_registry,
            budget_manager=self._budget_manager,
        )

        async def _session_event_handler(event):
            event_type_map = {
                "started": EventType.WORKFLOW_STAGE_START,
                "completed": EventType.WORKFLOW_STAGE_END,
                "failed": EventType.WORKFLOW_STAGE_ERROR,
                "retrying": EventType.WORKFLOW_STAGE_START,
                "failure_decision": EventType.WORKFLOW_STAGE_ERROR,
            }
            mapped_type = event_type_map.get(event.event_type)
            if mapped_type:
                store.emit_event(
                    session_id, mapped_type,
                    {"node_id": event.node_id, **event.details},
                )

        engine.on_event(_session_event_handler)
        return engine

    @staticmethod
    def load_project_guardrails(work_dir: Path | None) -> dict[str, Any]:
        """Load guardrail overrides from .weave/config.yaml if present."""
        result: dict[str, Any] = {}
        if work_dir is None:
            return result
        try:
            config_path = Path(work_dir) / ".weave" / "config.yaml"
            if config_path.exists():
                import yaml
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                gr = cfg.get("guardrails", {})
                if "permission_mode" in gr:
                    result["permission_mode"] = PermissionMode(gr["permission_mode"])
                if "auto_approve_read" in gr:
                    result["auto_approve_read"] = gr["auto_approve_read"]
                if "denied_commands" in gr:
                    result["denied_commands"] = gr["denied_commands"]
                if "allowed_tools" in gr:
                    result["allowed_tools"] = gr["allowed_tools"]
        except Exception:
            pass
        return result
