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
import sys
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
from core.backend_models import BackendContext
from core.artifact_handoff import ArtifactHandoffService
from core.quality_gate import QualityGate
from core.retry_policy import RetryPolicyEngine
from core.watchdog import WatchdogService
from core.budget_manager import BudgetManager
from core.evaluation_pipeline import EvaluationPipeline, EvalOutcome

EventHandler = Callable[[ExecutionEvent], Coroutine[Any, Any, None]]

logger = logging.getLogger(__name__)


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
        evaluator: Any | None = None,
        artifact_path: str = "./data/artifacts",
        work_dir: str | None = None,
        quality_gate: QualityGate | None = None,
        artifact_handoff: ArtifactHandoffService | None = None,
        node_timeout_config: Any | None = None,
        backend_manager: Any | None = None,
        job_id: str = "",
        run_id: str = "",
        backoff_base: float = 2.0,
        backoff_cap: float = 60.0,
        backend_registry: Any | None = None,
        session_id: str = "",
        budget_manager: BudgetManager | None = None,
    ) -> None:
        self.agent_executor = agent_executor
        self._emit = emit_func
        self._watchdog = watchdog
        self._evaluator = evaluator
        self.artifact_path = artifact_path
        self.work_dir = work_dir
        self._quality_gate = quality_gate or QualityGate()
        self._artifact_handoff = artifact_handoff or ArtifactHandoffService()
        self._node_timeout_config = node_timeout_config
        self.backend_manager = backend_manager
        self._job_id = job_id
        self._run_id = run_id
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._backend_registry = backend_registry
        self._session_id = session_id
        self._budget_manager = budget_manager
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._eval_pipeline = EvaluationPipeline(
            evaluator=evaluator,
            quality_gate=self._quality_gate,
            budget_manager=budget_manager,
            artifact_path=artifact_path,
            work_dir=work_dir,
            node_timeout_config=node_timeout_config,
            emit_func=emit_func,
        )

    @property
    def evaluator(self):
        return self._evaluator

    @evaluator.setter
    def evaluator(self, value):
        self._evaluator = value
        self._eval_pipeline._evaluator = value

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
                prep = self._prepare_stage(dag, node_id)
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
                except (
                    asyncio.CancelledError,
                    PendingApprovalError,
                    RateLimitError,
                    NodeTimeoutError,
                    BudgetExhaustedError,
                ):
                    raise  # System errors -> outer catch
                except Exception:
                    should_retry = self._handle_exec_error(
                        dag, node_id, node_workspace,
                    )
                    if should_retry:
                        node_workspace = self._cleanup_for_retry(
                            dag, node_id, node_workspace,
                        )
                        backoff = self._compute_backoff(
                            dag.nodes[node_id].retry_count,
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

                # Evaluation failure -> return, dag_engine handles retry
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
            raise

        except (RateLimitError, NodeTimeoutError) as e:
            reason = (
                "rate_limit" if isinstance(e, RateLimitError) else "timeout"
            )
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

    def _prepare_stage(
        self, dag: DAG, node_id: str,
    ) -> _PrepareResult | None:
        """Check deps, budget, set RUNNING, register watchdog, setup workspace."""
        node = dag.nodes[node_id]

        hard_deps = dag.get_hard_dependencies(node_id)
        failed_hard = [
            d for d in hard_deps
            if dag.nodes[d].status in (NodeStatus.FAILED, NodeStatus.SKIPPED)
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
            if dag.nodes[d].status in (NodeStatus.FAILED, NodeStatus.SKIPPED)
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

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._emit(ExecutionEvent(
                node_id=node_id,
                event_type="started",
                details={
                    "agent_type": node.agent_type,
                    "task": node.task_description[:100],
                },
            )))
        except RuntimeError:
            pass

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
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._emit(ExecutionEvent(
                        node_id=node_id,
                        event_type="workspace_isolation_failed",
                        details={"error": str(exc)},
                    )))
                except RuntimeError:
                    pass
            previous_artifacts = node.output_artifacts or []
            reported_artifacts = result.get("artifacts", [])
            # Preserve previous artifacts on retry: agent may only read/test
            # without write/edit, leaving artifacts empty (#165).
            if not reported_artifacts and previous_artifacts:
                reported_artifacts = previous_artifacts
                logger.info(
                    "Node %s: retry produced no new artifacts, preserving %d from previous attempt",
                    node_id, len(previous_artifacts),
                )
            node = dag.update_node(node_id, output_artifacts=reported_artifacts)
            logger.debug(
                "Node %s (%s) produced artifacts: %s",
                node_id, node.agent_type, node.output_artifacts,
            )

            # -- Zero-output fast-fail (#229) --
            # #626: For test nodes, check if upstream produced artifacts.
            # If so, inherit them and continue instead of failing — the
            # evaluator can still run on the implementation files.
            if (
                not node.output_artifacts
                and self._requires_output_artifacts(node)
            ):
                upstream_artifacts = self._collect_upstream_artifacts(
                    dag, node_id,
                )
                if (
                    upstream_artifacts
                    and self._is_test_node(node)
                ):
                    # Inherit artifacts but do NOT set SUCCESS or return —
                    # let the node continue through quality gate and
                    # evaluator, which decide the final status.
                    logger.warning(
                        "Node %s (%s) produced zero output but upstream has "
                        "%d artifacts — inheriting for evaluation (#626)",
                        node_id, node.agent_type, len(upstream_artifacts),
                    )
                    node = dag.update_node(
                        node_id,
                        output_artifacts=upstream_artifacts,
                    )
                    await self._emit(ExecutionEvent(
                        node_id=node_id,
                        event_type="degeneration_recovered",
                        details={
                            "reason": "inherited_upstream_artifacts",
                            "inherited_count": len(upstream_artifacts),
                        },
                    ))
                    # Fall through to quality gate + evaluator
                else:
                    node = dag.update_node(
                        node_id,
                        error=(
                            f"Node produced zero output artifacts. "
                            f"Agent type: {node.agent_type}, "
                            f"task: {node.task_description[:200]}. "
                            f"This typically indicates the agent exhausted "
                            f"its iteration budget without writing any files."
                        ),
                        status=NodeStatus.FAILED,
                        completed_at=datetime.now(timezone.utc),
                        retry_count=node.retry_count + 1,
                    )
                    logger.warning(
                        "Node %s (%s) fast-failed: zero output artifacts",
                        node_id, node.agent_type,
                    )
                    await self._emit(ExecutionEvent(
                        node_id=node_id,
                        event_type="failed",
                        details={
                            "reason": "zero_output_artifacts",
                            "agent_type": node.agent_type,
                        },
                    ))
                    return

            # Test file enforcement (#247): delegated to QualityGate.
            test_check = self._quality_gate.check_test_file_requirement(
                node, node_id,
            )
            if test_check is not None:
                node = dag.update_node(
                    node_id,
                    eval_feedback=test_check.eval_feedback,
                    error=test_check.error,
                    status=test_check.node_status,
                    completed_at=datetime.now(timezone.utc),
                )
                await self._emit(ExecutionEvent(
                    node_id=node_id,
                    event_type=test_check.event_type,
                    details=test_check.event_details,
                ))
                return

            if self.evaluator and node.success_criteria and node.agent_type == "generator":
                if not self.work_dir:
                    logger.error(
                        "Node %s: work_dir not set — cannot evaluate safely. "
                        "Aborting evaluation to prevent incorrect results.",
                        node_id,
                    )
                    node = dag.update_node(
                        node_id,
                        status=NodeStatus.FAILED,
                        error=(
                            "Evaluation skipped: work_dir not configured. "
                            "Pass --project to set the working directory."
                        ),
                        completed_at=datetime.now(timezone.utc),
                    )
                    await self._emit(ExecutionEvent(
                        node_id=node_id,
                        event_type="failed",
                        details={"reason": "no_work_dir"},
                    ))
                    return
                eval_work_dir = workspace_path or self.work_dir
                # M4.5: evaluate_stage with its own stall timeout
                from core.progress import ProgressTracker
                eval_stall_timeout = self._get_stall_timeout("evaluator")
                eval_tracker = ProgressTracker(
                    stall_timeout=eval_stall_timeout,
                )
                eval_timeout = self._get_node_timeout("evaluator")
                try:
                    eval_result = await asyncio.wait_for(
                        asyncio.get_running_loop().run_in_executor(
                            self._executor,
                            functools.partial(
                                self.evaluator.evaluate_stage,
                                node_id, node_id, node.success_criteria, self.artifact_path,
                                work_dir=eval_work_dir,
                                output_artifacts=node.output_artifacts or None,
                                owned_files=node.owned_files or None,
                                progress_tracker=eval_tracker,  # M4.5
                            ),
                        ),
                        timeout=eval_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Node %s: evaluate_stage timed out after %ds",
                        node_id, eval_timeout,
                    )
                    node = dag.update_node(
                        node_id,
                        status=NodeStatus.FAILED,
                        error=f"Evaluation timed out after {eval_timeout}s",
                        completed_at=datetime.now(timezone.utc),
                    )
                    await self._emit(ExecutionEvent(
                        node_id=node_id,
                        event_type="failed",
                        details={"reason": "eval_timeout", "timeout": eval_timeout},
                    ))
                    return
>>>>>>> origin/refactor/m45-timeout-redesign

        return _PrepareResult(
            input_artifacts=input_artifacts,
            workspace_path=workspace_path,
            node_workspace=node_workspace,
        )

    # ------------------------------------------------------------------
    # Error handling helpers
    # ------------------------------------------------------------------

    def _handle_exec_error(
        self,
        dag: DAG,
        node_id: str,
        node_workspace: Any,
    ) -> bool:
        """Classify execution error and decide retry.

        Returns True if should retry, False if exhausted.
        """
        node = dag.nodes[node_id]

        if node.health_status == NodeHealth.DEAD:
            return False

        exc_type, exc_value, exc_tb = sys.exc_info()
        error_str = (
            f"{exc_type.__name__}: {exc_value}\n"
            f"{''.join(traceback.format_tb(exc_tb))}"
        )
        dag.update_node(node_id, error=error_str)

        node = dag.update_node(
            node_id, retry_count=node.retry_count + 1,
        )

        if node.retry_count < node.max_retries:
            dag.update_node(node_id, status=NodeStatus.RETRYING)
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._emit(ExecutionEvent(
                    node_id=node_id,
                    event_type="retrying",
                    details={
                        "attempt": node.retry_count,
                        "error": str(exc_value),
                    },
                )))
            except RuntimeError:
                pass
            return True
        else:
            dag.update_node(
                node_id,
                status=NodeStatus.FAILED,
                completed_at=datetime.now(timezone.utc),
                auto_eval_result=None,
            )
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._emit(ExecutionEvent(
                    node_id=node_id,
                    event_type="failed",
                    details={
                        "error": str(exc_value),
                        "attempts": node.retry_count,
                    },
                )))
            except RuntimeError:
                pass
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
            node.agent_type, node=node, workspace_path=workspace_path,
        )

        tracker = ProgressTracker(stall_timeout=stall_timeout)
        cancel_event = threading.Event()

        loop = asyncio.get_running_loop()

        def _on_progress() -> None:
            try:
                loop.call_soon_threadsafe(node.record_heartbeat)
            except RuntimeError:
                pass

        if self._backend_registry is not None:
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
            )
            backend_name = getattr(node, 'backend', 'builtin')

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
                    raise NodeTimeoutError(
                        node_id=node.id,
                        agent_type=node.agent_type,
                        timeout=stall_timeout,
                    )
                should_kill, reason = tracker.should_kill()
                if should_kill:
                    cancel_event.set()
                    logger.warning(
                        "Node %s (%s) killed: %s (elapsed %.0fs)",
                        node.id, node.agent_type, reason,
                        tracker.elapsed,
                    )
                    raise NodeTimeoutError(
                        node_id=node.id,
                        agent_type=node.agent_type,
                        timeout=stall_timeout,
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
        workspace_path: str | None = None,
    ) -> int:
        """Return dynamic stall timeout (M4.5)."""
        if self._node_timeout_config is not None:
            return self._node_timeout_config.stall_timeout_for(
                agent_type, node=node, workspace_path=workspace_path,
            )
        return self._get_node_timeout(agent_type)

    def _collect_input_artifacts(
        self,
        dag: DAG,
        node_id: str,
        failed_soft: list[str] | None = None,
    ) -> list[HandoffArtifact]:
        return self._artifact_handoff.collect(dag, node_id, failed_soft)

    def _compute_backoff(self, retry_count: int) -> float:
        return RetryPolicyEngine.compute_backoff(
            retry_count, base=self.backoff_base, cap=self.backoff_cap,
        )
