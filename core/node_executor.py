"""
NodeExecutor — single-node DAG execution with retry, evaluation, and timeouts.

Extracted from DAGExecutionEngine as part of #177 PR5.
Behavior-preserving extraction: all logic is identical, just relocated
for testability and separation of concerns.

NodeExecutor handles the full lifecycle of a single node:
- Dependency-aware skip (hard/soft)
- Workspace isolation
- Agent execution with timeout
- Evaluation gate (quality, test files, zero-output)
- Retry logic with exponential backoff
- Error classification (rate limit, timeout, approval, generic)
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import logging
import re
import threading
import traceback
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

EventHandler = Callable[[ExecutionEvent], Coroutine[Any, Any, None]]

logger = logging.getLogger(__name__)


class NodeExecutor:
    """Executes a single DAG node with retry, evaluation, and timeout logic.

    Extracted from DAGExecutionEngine._execute_single_node (#177 PR5).
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
        self.evaluator = evaluator
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
        # Thread pool for synchronous evaluator calls
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        # Track running tasks for cancellation support
        self._running_tasks: dict[str, asyncio.Task] = {}

    async def execute_node(self, dag: DAG, node_id: str) -> None:
        """Execute a single DAG node with retry logic.

        This is the main entry point, called by DAGExecutionEngine for each
        node in topological level order.
        """
        node = dag.nodes[node_id]

        # Skip if already executed (from merged DAG after replan)
        if self._is_terminal_success(node.status):
            return

        if node.status not in (NodeStatus.PENDING, NodeStatus.RETRYING):
            return

        # Dependency-aware skip with hard/soft semantics (#271).
        # HARD deps: upstream FAILED/SKIPPED → downstream SKIP.
        # SOFT deps: upstream FAILED/SKIPPED → downstream continues with warning.
        hard_deps = dag.get_hard_dependencies(node_id)
        failed_hard = [
            d for d in hard_deps
            if dag.nodes[d].status in (NodeStatus.FAILED, NodeStatus.SKIPPED)
        ]
        if failed_hard:
            node = dag.update_node(
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
            return

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

        # M4.2: Budget check before starting node
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
        node.record_heartbeat()  # M2.0: Initial heartbeat

        logger.info(
            "Node %s (%s) starting — attempt %d/%d",
            node_id, node.agent_type, node.retry_count + 1, node.max_retries,
        )

        # M2.0: Register with watchdog
        self._watchdog.register(node_id, node)
        current_task = asyncio.current_task()
        if current_task:
            self._running_tasks[node_id] = current_task

        await self._emit(ExecutionEvent(
            node_id=node_id,
            event_type="started",
            details={"agent_type": node.agent_type, "task": node.task_description[:100]},
        ))

        # Per-node workspace isolation (#176 PR2).
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

        try:
            result = await self._execute_with_timeout(
                node, input_artifacts,
                workspace_path=workspace_path,
            )

            # Defensive: agent_executor may return None (e.g. in tests)
            if result is None:
                result = {}

            # M4.2: Record token usage from node result
            token_usage = result.get("token_usage", {})
            if token_usage and self._budget_manager:
                self._budget_manager.record_usage(
                    input_tokens=token_usage.get("input_tokens", 0),
                    output_tokens=token_usage.get("output_tokens", 0),
                )
                total = token_usage.get("input_tokens", 0) + token_usage.get("output_tokens", 0)
                dag.update_node(node_id, token_usage={
                    "input_tokens": token_usage.get("input_tokens", 0),
                    "output_tokens": token_usage.get("output_tokens", 0),
                    "total_tokens": total,
                })

            # -- Evaluation gate --
            # Assign output_artifacts BEFORE evaluation so evaluator can use them
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
                # M4.5: evaluate_stage with its own timeout + progress tracker
                from core.progress import ProgressTracker, estimate_max_timeout as _est
                eval_max_total = _est("evaluator", node, eval_work_dir)
                eval_tracker = ProgressTracker(
                    stall_timeout=self._get_stall_timeout("evaluator"),
                    max_total=eval_max_total,
                )
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
                        timeout=eval_max_total,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Node %s: evaluate_stage timed out after %ds",
                        node_id, eval_max_total,
                    )
                    node = dag.update_node(
                        node_id,
                        status=NodeStatus.FAILED,
                        error=f"Evaluation timed out after {eval_max_total}s",
                        completed_at=datetime.now(timezone.utc),
                    )
                    await self._emit(ExecutionEvent(
                        node_id=node_id,
                        event_type="failed",
                        details={"reason": "eval_timeout", "timeout": eval_max_total},
                    ))
                    return

                # Delegate evaluation result processing to QualityGate.
                outcome = self._quality_gate.evaluate(
                    eval_result, node, node_id, eval_work_dir,
                )

                node = dag.update_node(
                    node_id, auto_eval_result=outcome.auto_eval_result,
                )

                if not outcome.passed:
                    new_retry_count = node.retry_count + outcome.retry_count_increment
                    updates: dict[str, Any] = {
                        "retry_count": new_retry_count,
                        "eval_feedback": outcome.eval_feedback,
                        "error": outcome.error,
                        "status": outcome.node_status,
                        "completed_at": datetime.now(timezone.utc),
                    }
                    # Update artifacts from regression restoration
                    if outcome.restored_artifacts is not None:
                        updates["output_artifacts"] = outcome.restored_artifacts
                    # If node exhausted retries, clear auto_eval_result
                    if new_retry_count >= node.max_retries:
                        updates["auto_eval_result"] = None
                    node = dag.update_node(node_id, **updates)

                    await self._emit(ExecutionEvent(
                        node_id=node_id,
                        event_type="failed",
                        details={
                            "reason": "evaluation_failed",
                            "score": eval_result.score,
                            "attempt": node.retry_count,
                        },
                    ))
                    return

            # Map evaluator result to node status (#270).
            if self.evaluator and node.success_criteria and node.agent_type == "generator":
                final_status = QualityGate.eval_status_to_node_status(eval_result.eval_status)
            else:
                final_status = NodeStatus.SUCCESS
            node = dag.update_node(
                node_id,
                status=final_status,
                completed_at=datetime.now(timezone.utc),
                result=result,
                output_artifacts=result.get("artifacts", []),
            )

            # R3: Cleanup leftover artifacts after node success (#240)
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
                    logger.debug("Artifact cleanup failed for node %s: %s", node_id, e)

            await self._emit(ExecutionEvent(
                node_id=node_id,
                event_type="completed",
                details={"output_count": len(node.output_artifacts)},
            ))

        except asyncio.CancelledError:
            # M2.0: Check if node was killed by watchdog (DEAD state)
            if node.health_status == NodeHealth.DEAD:
                return  # Swallow cancellation for watchdog-killed nodes
            raise  # Re-raise for genuine cancellation requests

        except PendingApprovalError:
            # Agent hit a high-risk tool requiring human approval.
            # Do NOT retry, do NOT mark as failed — just pause and re-raise
            # so the Worker can enter its PENDING_APPROVAL poll loop.
            dag.update_node(
                node_id,
                status=NodeStatus.PENDING_APPROVAL,
                completed_at=datetime.now(timezone.utc),
            )
            raise

        except Exception as e:
            # M2.0: Check if node was already killed by watchdog (DEAD state)
            if node.health_status == NodeHealth.DEAD:
                # Node was killed by watchdog; do not retry
                return

            node = dag.update_node(
                node_id,
                error=f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}",
            )

            # RateLimitError / NodeTimeoutError: do NOT consume retry budget.
            # These are system-level failures (API throttling, LLM latency),
            # not agent logic errors (#360, #432).
            if isinstance(e, (RateLimitError, NodeTimeoutError)):
                reason = (
                    "rate_limit" if isinstance(e, RateLimitError)
                    else "timeout"
                )
                node = dag.update_node(
                    node_id,
                    status=NodeStatus.FAILED,
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
                return

            # M4.2: Budget exhausted — skip this and remaining nodes
            if isinstance(e, BudgetExhaustedError):
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

            node = dag.update_node(
                node_id, retry_count=node.retry_count + 1,
            )

            if node.retry_count < node.max_retries:
                # Clean up workspace before retry so setup_node gets a clean
                # slate (e.g. git worktree add would fail if dir exists).
                if node_workspace and self.backend_manager:
                    try:
                        self.backend_manager.cleanup_node(
                            self._job_id, self._run_id, node_id,
                        )
                        node_workspace = None
                        workspace_path = None
                    except Exception as cleanup_exc:
                        logger.warning(
                            "Node %s: pre-retry cleanup failed: %s",
                            node_id, cleanup_exc,
                        )
                dag.update_node(node_id, status=NodeStatus.RETRYING)
                await self._emit(ExecutionEvent(
                    node_id=node_id,
                    event_type="retrying",
                    details={"attempt": node.retry_count, "error": str(e)},
                ))
                backoff = self._compute_backoff(node.retry_count)
                await asyncio.sleep(backoff)
                await self.execute_node(dag, node_id)
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
                    details={"error": str(e), "attempts": node.retry_count},
                ))
        finally:
            # M2.0: Unregister from watchdog on completion (unless killed by watchdog)
            if node.health_status != NodeHealth.DEAD:
                self._watchdog.unregister(node_id)
                self._running_tasks.pop(node_id, None)
            # Clean up per-node workspace (#176 PR2)
            if node_workspace and self.backend_manager:
                try:
                    self.backend_manager.cleanup_node(
                        self._job_id, self._run_id, node_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "Node %s: cleanup_node failed: %s", node_id, exc,
                    )

    # -- Timeout execution --------------------------------------------------

    async def _execute_with_timeout(
        self,
        node: DAGNode,
        input_artifacts: list[HandoffArtifact],
        workspace_path: str | None = None,
    ) -> dict[str, Any]:
        """Execute a node with progress-driven timeout (M4.5).

        Replaces static wall-clock ``asyncio.wait_for`` with a poll loop
        that checks ProgressTracker.should_kill().  Work units (LLM calls,
        subprocesses, tool execution) report progress via the shared tracker.
        """
        from core.progress import ProgressTracker, estimate_max_timeout

        # #621: Pass artifact count for dynamic evaluator timeout scaling.
        configured_timeout = self._get_node_timeout(
            node.agent_type,
            artifact_count=sum(
                len(a.file_paths) for a in input_artifacts
            ),
        )
        estimated_timeout = estimate_max_timeout(node.agent_type, node, workspace_path)
        max_total = min(configured_timeout, estimated_timeout)
        # Stall timeout: use configured value, but never exceed max_total
        stall_timeout = min(
            self._get_stall_timeout(node.agent_type), max_total,
        )

        tracker = ProgressTracker(stall_timeout=stall_timeout, max_total=max_total)
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
                progress_tracker=tracker,  # M4.5
            )
            backend_name = getattr(node, 'backend', 'builtin')

            async def _run_via_registry() -> dict:
                result = await self._backend_registry.execute_for_node(
                    backend_name, context,
                )
                return result.to_dict()

            task = asyncio.create_task(_run_via_registry())
        else:
            async def _run_with_cancel(n: DAGNode, arts: list[HandoffArtifact]) -> dict:
                return await self.agent_executor(
                    n, arts,
                    cancel_event=cancel_event,
                    progress_callback=_on_progress,
                    workspace_path=workspace_path,
                    progress_tracker=tracker,  # M4.5
                )

            task = asyncio.create_task(_run_with_cancel(node, input_artifacts))

        # M4.5: Poll loop replacing asyncio.wait_for
        try:
            while not task.done():
                # Check if watchdog killed this node
                if node.health_status == NodeHealth.DEAD:
                    cancel_event.set()
                    if not task.done():
                        task.cancel()
                    raise NodeTimeoutError(
                        node_id=node.id,
                        agent_type=node.agent_type,
                        timeout=max_total,
                    )
                should_kill, reason = tracker.should_kill()
                if should_kill:
                    cancel_event.set()
                    logger.warning(
                        "Node %s (%s) killed: %s (elapsed %.0fs, max %ds)",
                        node.id, node.agent_type, reason,
                        tracker.elapsed, max_total,
                    )
                    raise NodeTimeoutError(
                        node_id=node.id,
                        agent_type=node.agent_type,
                        timeout=max_total,
                    )
                # Sync heartbeat to watchdog when progress reported
                if tracker.has_recent_progress():
                    node.record_heartbeat()
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
                    return task.result()
                except asyncio.TimeoutError:
                    continue  # Poll loop: check tracker again
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

    # -- Helpers -------------------------------------------------------------

    def _get_node_timeout(
        self, agent_type: str, artifact_count: int = 0,
    ) -> int:
        """Return node timeout for the given agent type.

        Uses NodeTimeoutConfig from config if available, otherwise falls
        back to watchdog-based calculation for backward compatibility.
        """
        if self._node_timeout_config is not None:
            return self._node_timeout_config.timeout_for(
                agent_type, artifact_count=artifact_count,
            )
        interval, threshold = self._watchdog.get_heartbeat_settings(agent_type)
        return max(1, int(interval * threshold))

    def _get_stall_timeout(self, agent_type: str) -> int:
        """Return stall timeout for the given agent type (M4.5)."""
        if self._node_timeout_config is not None:
            return self._node_timeout_config.stall_timeout_for(agent_type)
        return 120  # Default stall timeout

    def _collect_input_artifacts(
        self,
        dag: DAG,
        node_id: str,
        failed_soft: list[str] | None = None,
    ) -> list[HandoffArtifact]:
        """Collect output artifacts from all dependency nodes."""
        return self._artifact_handoff.collect(dag, node_id, failed_soft)

    def _compute_backoff(self, retry_count: int) -> float:
        """Compute exponential backoff for retry."""
        return RetryPolicyEngine.compute_backoff(
            retry_count, base=self.backoff_base, cap=self.backoff_cap,
        )

    @staticmethod
    def _is_terminal_success(status: NodeStatus) -> bool:
        """Check if a node status represents a successful terminal state."""
        return QualityGate.is_terminal_success(status)

    @staticmethod
    def _requires_output_artifacts(node: DAGNode) -> bool:
        """Check whether a node is expected to produce output file artifacts."""
        return RetryPolicyEngine.requires_output_artifacts(node)

    # -- #626: Test node deep degeneration recovery helpers --

    _TEST_NODE_PATTERN: re.Pattern = re.compile(
        r'\b(tests?|specs?)\b', re.IGNORECASE,
    )

    @classmethod
    def _is_test_node(cls, node: DAGNode) -> bool:
        """Heuristic: detect test-generation nodes by task description (#626)."""
        return bool(cls._TEST_NODE_PATTERN.search(node.task_description))

    @staticmethod
    def _collect_upstream_artifacts(dag: DAG, node_id: str) -> list[str]:
        """Collect output_artifacts from successful upstream dependency nodes (#626).

        Used when a test node fails to produce its own artifacts but upstream
        implementation nodes succeeded — allows evaluation to continue on the
        implementation files.
        """
        # Collect all dependency node IDs from DAG edges
        dep_ids = set()
        for edge in dag.edges:
            if edge.to_node == node_id:
                dep_ids.add(edge.from_node)

        seen: set[str] = set()
        artifacts: list[str] = []
        for dep_id in dep_ids:
            dep_node = dag.nodes.get(dep_id)
            if (
                dep_node
                and QualityGate.is_terminal_success(dep_node.status)
                and dep_node.output_artifacts
            ):
                for a in dep_node.output_artifacts:
                    if a not in seen:
                        seen.add(a)
                        artifacts.append(a)
        return artifacts
