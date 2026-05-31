"""
NodeExecutor — single-node DAG execution with retry, evaluation, and timeouts.

Extracted from DAGExecutionEngine as part of #177 PR5.
Refactored to 3-stage pipeline (ADR-0015):
  prepare -> execute -> evaluate

NodeExecutor handles the full lifecycle of a single node:
- Dependency-aware skip (hard/soft)
- Workspace isolation
- Agent execution with timeout
- Evaluation gate (quality, test files, zero-output)
- Retry logic with exponential backoff (while loop, not recursion)
- Error classification (rate limit, timeout, approval, generic)
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

from core.models import (
    DAG,
    DAGNode,
    NodeStatus,
    NodeHealth,
    ExecutionEvent,
    HandoffArtifact,
)
from core.exceptions import PendingApprovalError
from core.exceptions import RateLimitError
from core.exceptions import NodeTimeoutError
from core.exceptions import BudgetExhaustedError
from core.exceptions import GuardrailBlockedException
from core.backend_models import BackendContext
from core.artifact_handoff import ArtifactHandoffService
from core.quality_gate import QualityGate
from core.retry_policy import RetryPolicyEngine
from core.watchdog import WatchdogService
from core.budget_manager import BudgetManager
from core.evaluation_pipeline import EvaluationPipeline, EvalOutcome
from core.activity_detector import ActivityDetector

EventHandler = Callable[[ExecutionEvent], Coroutine[Any, Any, None]]

logger = logging.getLogger(__name__)


@dataclass
class NodeExecutorConfig:
    """Configuration and optional service dependencies for NodeExecutor.

    Groups the 19 optional parameters that were previously passed individually
    to ``NodeExecutor.__init__``.  Required behavioral args (agent_executor,
    emit_func, watchdog) remain as direct constructor parameters.
    """

    evaluator: Any | None = None
    artifact_path: str = "./data/artifacts"
    work_dir: str | None = None
    quality_gate: QualityGate | None = None
    artifact_handoff: ArtifactHandoffService | None = None
    node_timeout_config: Any | None = None
    backend_manager: Any | None = None
    job_id: str = ""
    run_id: str = ""
    backoff_base: float = 2.0
    backoff_cap: float = 60.0
    backend_registry: Any | None = None
    session_id: str = ""
    budget_manager: BudgetManager | None = None
    memory_manager: Any | None = None
    project_config: Any | None = None
    default_agent_backend: str = "claude_code"
    session_store: Any | None = None
    node_guardrails: Any | None = None


@dataclass
class _PrepareResult:
    """Output of the prepare stage."""

    input_artifacts: list[HandoffArtifact]
    workspace_path: str | None
    node_workspace: Any


class NodeExecutor:
    """Executes a single DAG node with retry, evaluation, and timeout logic.

    Refactored to 3-stage pipeline (ADR-0015):
      prepare -> execute -> evaluate

    Retry is a while loop (not recursion).
    """

    def __init__(
        self,
        agent_executor: Callable[
            [DAGNode, list[HandoffArtifact]], Coroutine[Any, Any, dict]
        ],
        emit_func: Callable[[ExecutionEvent], Coroutine[Any, Any, None]],
        watchdog: WatchdogService,
        config: NodeExecutorConfig | None = None,
    ) -> None:
        cfg = config or NodeExecutorConfig()
        self.agent_executor = agent_executor
        self._emit = emit_func
        self._watchdog = watchdog
        self._evaluator = cfg.evaluator
        self.artifact_path = cfg.artifact_path
        self.work_dir = cfg.work_dir
        self._quality_gate = cfg.quality_gate or QualityGate()
        self._artifact_handoff = cfg.artifact_handoff or ArtifactHandoffService()
        self._node_timeout_config = cfg.node_timeout_config
        self.backend_manager = cfg.backend_manager
        self._job_id = cfg.job_id
        self._run_id = cfg.run_id
        self.backoff_base = cfg.backoff_base
        self.backoff_cap = cfg.backoff_cap
        self._backend_registry = cfg.backend_registry
        self._session_id = cfg.session_id
        self._budget_manager = cfg.budget_manager
        self._memory_manager = cfg.memory_manager
        self._project_config = cfg.project_config
        self._default_agent_backend = cfg.default_agent_backend
        self._session_store = cfg.session_store
        self._node_guardrails = cfg.node_guardrails
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._eval_pipeline = EvaluationPipeline(
            evaluator=cfg.evaluator,
            quality_gate=self._quality_gate,
            budget_manager=cfg.budget_manager,
            artifact_path=cfg.artifact_path,
            work_dir=cfg.work_dir,
            node_timeout_config=cfg.node_timeout_config,
            emit_func=emit_func,
        )

    @property
    def evaluator(self):
        return self._evaluator

    @evaluator.setter
    def evaluator(self, value):
        self._evaluator = value
        self._eval_pipeline.evaluator = value

    # ------------------------------------------------------------------
    # Main entry point: 3-stage pipeline with retry loop
    # ------------------------------------------------------------------

    async def execute_node(self, dag: DAG, node_id: str) -> None:
        """Execute a single DAG node with retry loop (ADR-0015)."""
        # -- Gate check (outside loop — run once) --
        node = dag.nodes[node_id]
        if QualityGate.is_terminal_success(node.status):
            return
        if node.status not in (NodeStatus.PENDING, NodeStatus.RETRYING):
            return

        node_workspace = None

        try:
            while True:
                # === Stage 1: Prepare ===
                prep = await self._prepare_stage(dag, node_id)
                if prep is None:
                    return
                node_workspace = prep.node_workspace

                # === Stage 2: Execute ===
                try:
                    result = await self._execute_with_timeout(
                        dag.nodes[node_id],
                        prep.input_artifacts,
                        workspace_path=prep.workspace_path,
                    )
                    # M6.2: Post-check guardrail for external backends
                    if (
                        result
                        and self._node_guardrails
                        and result.get("artifacts")
                        and dag.nodes[node_id].backend not in ("builtin", "")
                    ):
                        post_result = self._node_guardrails.post_check(
                            result.get("artifacts", []),
                            workspace_path=prep.workspace_path,
                        )
                        if post_result.is_blocked:
                            raise GuardrailBlockedException(
                                post_result.reason, phase="post",
                            )
                    # M5.1: Trace LLM turn and tool calls (isolated from
                    # execution retry logic — trace failures must not trigger
                    # node retry).
                    if result:
                        try:
                            token_usage = result.get("token_usage", {})
                            await self._emit(ExecutionEvent(
                                node_id=node_id,
                                event_type="trace",
                                details={
                                    "trace_type": "llm_turn",
                                    "node_id": node_id,
                                    "input_tokens": token_usage.get(
                                        "input_tokens", 0,
                                    ),
                                    "output_tokens": token_usage.get(
                                        "output_tokens", 0,
                                    ),
                                    "model": result.get("model", "unknown"),
                                    "backend": result.get("backend", "unknown"),
                                },
                            ))
                            tool_calls = result.get("tool_calls", [])
                            for tc in tool_calls:
                                await self._emit(ExecutionEvent(
                                    node_id=node_id,
                                    event_type="trace",
                                    details={
                                        "trace_type": "tool_call",
                                        "node_id": node_id,
                                        "tool_name": tc.get("name", "unknown"),
                                    },
                                ))
                        except Exception:
                            logger.debug(
                                "Trace emission failed for node %s",
                                node_id, exc_info=True,
                            )
                except (
                    asyncio.CancelledError,
                    PendingApprovalError,
                    RateLimitError,
                    NodeTimeoutError,
                    BudgetExhaustedError,
                    GuardrailBlockedException,
                ):
                    # _HardTimeoutError (TimeoutError subclass) is NOT caught
                    # here — it propagates as a hard node failure.
                    raise  # System errors -> outer catch
                except Exception as exc:
                    should_retry = await self._handle_exec_error(
                        dag, node_id, node_workspace, exc,
                    )
                    if should_retry:
                        node_workspace = self._cleanup_for_retry(
                            dag, node_id, node_workspace,
                        )
                        backoff = RetryPolicyEngine.compute_backoff(
                            dag.nodes[node_id].retry_count,
                            base=self.backoff_base, cap=self.backoff_cap,
                        )
                        await asyncio.sleep(backoff)
                        continue
                    return

                # === Stage 3: Evaluate ===
                eval_out = await self._eval_pipeline.evaluate(
                    dag, node_id, result or {},
                    workspace_path=prep.workspace_path,
                    executor=self._executor,
                    emit_func=self._emit,
                )

                if eval_out.passed:
                    await self._finalize_success(
                        dag, node_id, eval_out, prep.workspace_path,
                    )
                    return

                # Evaluation failure → return to dag_engine for retry.
                # Unlike execution errors (which retry in this while loop),
                # evaluation failures are handled by dag_engine's
                # retry_failure_handler, which may choose to re-plan,
                # re-execute a different node, or retry this one.
                return

        except asyncio.CancelledError:
            node = dag.nodes[node_id]
            if node.health_status == NodeHealth.DEAD:
                return
            raise

        except PendingApprovalError:
            dag.update_node(
                node_id,
                status=NodeStatus.PENDING_APPROVAL,
                completed_at=datetime.now(timezone.utc),
            )
            logger.info("Node %s paused for approval", node_id)
            await self._emit(ExecutionEvent(
                node_id=node_id,
                event_type="approval_required",
                details={"ticket": "pending"},
            ))
            raise

        except (RateLimitError, NodeTimeoutError) as e:
            reason = (
                "rate_limit" if isinstance(e, RateLimitError) else "timeout"
            )
            # Do NOT increment retry_count — timeout/rate-limit are
            # infrastructure errors, not code quality failures (#432).
            # Infinite retry protection comes from max_replans and the
            # failure_handler (which may return skip/abort).
            dag.update_node(
                node_id,
                status=NodeStatus.FAILED,
                error=str(e),
                completed_at=datetime.now(timezone.utc),
                auto_eval_result=None,
            )
            await self._emit(ExecutionEvent(
                node_id=node_id,
                event_type="failed",
                details={
                    "error": str(e),
                    "reason": reason,
                    "retry_budget_preserved": True,
                    "retry_count": dag.nodes[node_id].retry_count,
                },
            ))

        except BudgetExhaustedError as e:
            dag.update_node(
                node_id,
                status=NodeStatus.SKIPPED,
                error=f"Budget exhausted: {e}",
                completed_at=datetime.now(timezone.utc),
            )
            await self._emit(ExecutionEvent(
                node_id=node_id,
                event_type="failed",
                details={
                    "reason": "budget_exhausted",
                    "used": e.used_tokens,
                    "budget": e.budget_tokens,
                },
            ))
            raise

        except GuardrailBlockedException as e:
            dag.update_node(
                node_id,
                status=NodeStatus.FAILED,
                error=str(e),
                completed_at=datetime.now(timezone.utc),
                auto_eval_result=None,
            )
            await self._emit(ExecutionEvent(
                node_id=node_id,
                event_type="guardrail_blocked",
                details={
                    "error": str(e),
                    "reason": e.reason,
                    "phase": e.phase,
                    "retry_budget_preserved": True,
                    "retry_count": dag.nodes[node_id].retry_count,
                },
            ))

        finally:
            node = dag.nodes[node_id]
            if node.health_status != NodeHealth.DEAD:
                self._watchdog.unregister(node_id)
                self._running_tasks.pop(node_id, None)
            if node_workspace and self.backend_manager:
                try:
                    self.backend_manager.cleanup_node(
                        self._job_id, self._run_id, node_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "Node %s: cleanup_node failed: %s", node_id, exc,
                    )

    # ------------------------------------------------------------------
    # Stage 1: Prepare
    # ------------------------------------------------------------------

    async def _prepare_stage(
        self, dag: DAG, node_id: str,
    ) -> _PrepareResult | None:
        """Check deps, budget, set RUNNING, register watchdog, setup workspace."""
        node = dag.nodes[node_id]

        hard_deps = dag.get_hard_dependencies(node_id)
        failed_hard = [
            d for d in hard_deps
            if dag.nodes[d].status in (
                NodeStatus.FAILED, NodeStatus.SKIPPED,
                NodeStatus.PENDING_APPROVAL,
            )
        ]
        if failed_hard:
            dag.update_node(
                node_id,
                status=NodeStatus.SKIPPED,
                error=(
                    f"Skipped: hard dependencies {failed_hard} "
                    f"failed/were skipped"
                ),
            )
            logger.info(
                "Node %s skipped due to failed hard dependencies: %s",
                node_id, failed_hard,
            )
            return None

        soft_deps = dag.get_soft_dependencies(node_id)
        failed_soft = [
            d for d in soft_deps
            if dag.nodes[d].status in (
                NodeStatus.FAILED, NodeStatus.SKIPPED,
                NodeStatus.PENDING_APPROVAL,
            )
        ]
        if failed_soft:
            logger.info(
                "Node %s: soft dependencies %s failed, continuing anyway",
                node_id, failed_soft,
            )

        input_artifacts = self._collect_input_artifacts(
            dag, node_id, failed_soft=failed_soft,
        )

        if self._budget_manager and not self._budget_manager.check():
            raise BudgetExhaustedError(
                used_tokens=self._budget_manager.used_total_tokens,
                budget_tokens=self._budget_manager.config.total_tokens,
                node_id=node_id,
            )

        node = dag.update_node(
            node_id,
            status=NodeStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
            health_status=NodeHealth.HEALTHY,
        )
        node.record_heartbeat()

        logger.info(
            "Node %s (%s) starting — attempt %d/%d",
            node_id, node.agent_type, node.retry_count + 1, node.max_retries,
        )

        self._watchdog.register(node_id, node)
        current_task = asyncio.current_task()
        if current_task:
            self._running_tasks[node_id] = current_task

        await self._emit(ExecutionEvent(
            node_id=node_id,
            event_type="started",
            details={
                "agent_type": node.agent_type,
                "task": node.task_description[:100],
            },
        ))

        from core.models import NodeWorkspaceStrategy
        node_workspace = None
        workspace_path: str | None = None
        if (
            self.backend_manager
            and node.workspace_strategy != NodeWorkspaceStrategy.SHARED
        ):
            try:
                node_workspace = self.backend_manager.setup_node(
                    job_id=self._job_id,
                    run_id=self._run_id,
                    node_id=node_id,
                    strategy=node.workspace_strategy.value,
                )
                workspace_path = node_workspace.workspace_path
            except Exception as exc:
                logger.warning(
                    "Node %s: setup_node failed (%s), using shared workspace",
                    node_id, exc,
                )
                await self._emit(ExecutionEvent(
                    node_id=node_id,
                    event_type="workspace_isolation_failed",
                    details={"error": str(exc)},
                ))

        return _PrepareResult(
            input_artifacts=input_artifacts,
            workspace_path=workspace_path,
            node_workspace=node_workspace,
        )

    # ------------------------------------------------------------------
    # Error handling helpers
    # ------------------------------------------------------------------

    async def _handle_exec_error(
        self,
        dag: DAG,
        node_id: str,
        node_workspace: Any,
        exc: Exception,
    ) -> bool:
        """Classify execution error and decide retry.

        Returns True if should retry, False if exhausted.
        """
        node = dag.nodes[node_id]

        if node.health_status == NodeHealth.DEAD:
            return False

        error_str = (
            f"{type(exc).__name__}: {exc}\n"
            f"{''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))}"
        )
        dag.update_node(node_id, error=error_str)

        # #831: NodeTimeoutError/RateLimitError should not consume retry budget.
        no_budget = isinstance(exc, (NodeTimeoutError, RateLimitError))
        new_count = node.retry_count if no_budget else node.retry_count + 1
        node = dag.update_node(
            node_id, retry_count=new_count,
        )

        if node.retry_count < node.max_retries:
            dag.update_node(node_id, status=NodeStatus.RETRYING)
            await self._emit(ExecutionEvent(
                node_id=node_id,
                event_type="retrying",
                details={
                    "attempt": node.retry_count,
                    "error": str(exc),
                },
            ))
            return True
        else:
            dag.update_node(
                node_id,
                status=NodeStatus.FAILED,
                completed_at=datetime.now(timezone.utc),
                auto_eval_result=None,
            )
            await self._emit(ExecutionEvent(
                node_id=node_id,
                event_type="failed",
                details={
                    "error": str(exc),
                    "attempts": node.retry_count,
                },
            ))
            return False

    def _cleanup_for_retry(
        self,
        dag: DAG,
        node_id: str,
        node_workspace: Any,
    ) -> Any:
        """Clean up workspace before retry. Returns None."""
        if node_workspace and self.backend_manager:
            try:
                self.backend_manager.cleanup_node(
                    self._job_id, self._run_id, node_id,
                )
            except Exception as cleanup_exc:
                logger.warning(
                    "Node %s: pre-retry cleanup failed: %s",
                    node_id, cleanup_exc,
                )
        return None

    async def _finalize_success(
        self,
        dag: DAG,
        node_id: str,
        eval_out: EvalOutcome,
        workspace_path: str | None,
    ) -> None:
        """Apply final success status and cleanup artifacts."""
        node = dag.nodes[node_id]

        dag.update_node(
            node_id,
            status=eval_out.node_status,
            completed_at=datetime.now(timezone.utc),
            result=eval_out.result,
            output_artifacts=eval_out.output_artifacts,
        )

        if self.backend_manager and node.owned_files and node.started_at:
            try:
                cleaned = self.backend_manager.cleanup_node_artifacts(
                    job_id="",
                    run_id="",
                    node_id=node_id,
                    expected_artifacts=node.owned_files,
                    started_at=node.started_at.timestamp(),
                )
                if cleaned:
                    logger.info(
                        "Cleaned up %d leftover files from node %s",
                        len(cleaned), node_id,
                    )
            except Exception as e:
                logger.debug(
                    "Artifact cleanup failed for node %s: %s", node_id, e,
                )

        await self._emit(ExecutionEvent(
            node_id=node_id,
            event_type=eval_out.event_type or "completed",
            details=eval_out.event_details,
        ))

    # ------------------------------------------------------------------
    # Stage 2: Execute with timeout (unchanged from M4.5)
    # ------------------------------------------------------------------

    async def _execute_with_timeout(
        self,
        node: DAGNode,
        input_artifacts: list[HandoffArtifact],
        workspace_path: str | None = None,
    ) -> dict[str, Any]:
        """Execute a node with progress-driven timeout (M4.5).

        Poll loop checks ProgressTracker.should_kill().  Work units (LLM calls,
        subprocesses, tool execution) report progress via the shared tracker.
        Stall detection is the sole kill mechanism (no max_total).
        """
        from core.progress import ProgressTracker

        stall_timeout = self._get_stall_timeout(
            node.agent_type, node=node,
        )

        tracker = ProgressTracker(stall_timeout=stall_timeout)
        cancel_event = threading.Event()

        activity_detector = ActivityDetector(
            timeout_seconds=self._get_activity_timeout(node.agent_type),
        )

        loop = asyncio.get_running_loop()

        def _on_progress() -> None:
            try:
                loop.call_soon_threadsafe(node.record_heartbeat)
                activity_detector.record_activity()
                tracker.report("heartbeat")
            except RuntimeError:
                pass

        if self._backend_registry is not None:
            # Resolve backend name first to avoid double injection.
            # BuiltinBackend has its own memory injection via agent_pool/worker;
            # only inject into BackendContext for external backends.
            backend_name = node.backend or self._default_agent_backend

            # M6.2: Pre-check guardrail for external backends
            if self._node_guardrails and backend_name not in ("builtin", ""):
                pre_result = self._node_guardrails.pre_check(
                    node, workspace_path,
                )
                if pre_result.is_blocked:
                    raise GuardrailBlockedException(
                        pre_result.reason, phase="pre",
                    )
            use_external = backend_name != "builtin"

            memory_prompt = ""
            if use_external and self._memory_manager and self._memory_manager.config.enabled:
                try:
                    entries = self._memory_manager.get_context_for_agent(
                        agent_type=node.agent_type,
                        task_description=node.task_description,
                        session_id=self._session_id,
                    )
                    memory_prompt = self._memory_manager.format_memory_prompt(entries)
                except Exception:
                    logger.warning(
                        "Memory retrieval failed for node %s",
                        node.id, exc_info=True,
                    )

            project_context = ""
            if use_external and self._project_config:
                project_context = self._project_config.to_summary()

            # M6.5: event_callback bridges stream events to SessionStore.
            event_callback = None
            if self._session_store is not None and self._session_id:
                def _make_event_cb(sid: str, store: Any):
                    _type_map = {
                        "assistant": "agent.message",
                        "user": "user.message",
                        "result": "workflow.stage_end",
                        "system": "session.status_running",
                        "tool_use": "agent.tool_use",
                        "tool_result": "agent.tool_result",
                    }

                    def _cb(event_type_str: str, payload: dict) -> None:
                        try:
                            from core.event_models import EventType
                            mapped = _type_map.get(event_type_str, event_type_str)
                            store.emit_event(sid, EventType(mapped), payload)
                        except Exception:
                            logger.debug("event_callback failed", exc_info=True)
                    return _cb
                event_callback = _make_event_cb(self._session_id, self._session_store)

            context = BackendContext(
                node=node,
                artifacts=input_artifacts,
                session_id=self._session_id,
                workspace_path=workspace_path,
                job_id=self._job_id,
                run_id=self._run_id,
                cancel_event=cancel_event,
                progress_callback=_on_progress,
                progress_tracker=tracker,
                activity_detector=activity_detector,
                memory_prompt=memory_prompt,
                project_context=project_context,
                event_callback=event_callback,
            )

            async def _run_via_registry() -> dict:
                result = await self._backend_registry.execute_for_node(
                    backend_name, context,
                )
                return result.to_dict()

            task = asyncio.create_task(_run_via_registry())
        else:
            async def _run_with_cancel(
                n: DAGNode, arts: list[HandoffArtifact],
            ) -> dict:
                return await self.agent_executor(
                    n, arts,
                    cancel_event=cancel_event,
                    progress_callback=_on_progress,
                    workspace_path=workspace_path,
                    progress_tracker=tracker,
                )

            task = asyncio.create_task(
                _run_with_cancel(node, input_artifacts),
            )

        try:
            while not task.done():
                # Check if watchdog flagged this node (UNHEALTHY or DEAD)
                if node.health_status in (NodeHealth.UNHEALTHY, NodeHealth.DEAD):
                    cancel_event.set()
                    if not task.done():
                        task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                    raise NodeTimeoutError(
                        node_id=node.id,
                        agent_type=node.agent_type,
                        timeout=stall_timeout,
                    )
                should_kill, reason = tracker.should_kill()
                if should_kill:
                    cancel_event.set()
                    if not task.done():
                        task.cancel()
                    logger.warning(
                        "Node %s (%s) killed: %s (elapsed %.0fs)",
                        node.id, node.agent_type, reason,
                        tracker.elapsed,
                    )
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                    raise NodeTimeoutError(
                        node_id=node.id,
                        agent_type=node.agent_type,
                        timeout=stall_timeout,
                    )
                # M6.6: Semantic inactivity timeout — kill if no meaningful
                # stream events arrived for activity_timeout seconds.
                semantic_timeout, semantic_reason = activity_detector.check_timeout()
                if semantic_timeout:
                    cancel_event.set()
                    if not task.done():
                        task.cancel()
                    logger.warning(
                        "Node %s (%s) killed: semantic inactivity "
                        "(%s, elapsed %.0fs)",
                        node.id, node.agent_type, semantic_reason,
                        tracker.elapsed,
                    )
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                    raise NodeTimeoutError(
                        node_id=node.id,
                        agent_type=node.agent_type,
                        timeout=int(activity_detector.timeout_seconds),
                    )
                if tracker.has_recent_progress():
                    node.record_heartbeat()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task), timeout=5.0,
                    )
                    return task.result()
                except asyncio.TimeoutError:
                    continue
            return task.result()
        except asyncio.CancelledError:
            cancel_event.set()
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            raise

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_node_timeout(
        self, agent_type: str, artifact_count: int = 0,
    ) -> int:
        if self._node_timeout_config is not None:
            return self._node_timeout_config.timeout_for(
                agent_type, artifact_count=artifact_count,
            )
        interval, threshold = self._watchdog.get_heartbeat_settings(
            agent_type,
        )
        return max(1, int(interval * threshold))

    def _get_stall_timeout(
        self, agent_type: str, node: DAGNode | None = None,
    ) -> int:
        """Return dynamic stall timeout (M4.5)."""
        if self._node_timeout_config is not None:
            from core.node_utils import (
                estimate_feature_count,
                extract_node_complexity,
            )
            file_count, test_count, dep_count = (
                extract_node_complexity(node) if node else (0, 0, 0)
            )
            feature_count = (
                estimate_feature_count(
                    getattr(node, "task_description", ""),
                )
                if node else 0
            )
            return self._node_timeout_config.stall_timeout_for(
                agent_type,
                file_count=file_count,
                test_count=test_count,
                dep_count=dep_count,
                feature_count=feature_count,
            )
        return self._get_node_timeout(agent_type)

    def _get_activity_timeout(self, agent_type: str) -> float:
        """Get semantic inactivity timeout in seconds (M6.6).

        Defaults to 600s (10 min).  Override via
        ``NodeTimeoutConfig.activity_timeout`` if configured.
        """
        if self._node_timeout_config is not None:
            return getattr(
                self._node_timeout_config, "activity_timeout", 600.0,
            )
        return 600.0

    def _collect_input_artifacts(
        self,
        dag: DAG,
        node_id: str,
        failed_soft: list[str] | None = None,
    ) -> list[HandoffArtifact]:
        return self._artifact_handoff.collect(dag, node_id, failed_soft)
