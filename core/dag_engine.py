"""
DAG Execution Engine: Topological scheduling + parallel execution + failure handling.

Key design decisions:
1. Topological levels: Nodes at the same level execute in parallel
2. Failure handling: Delegated to IntelligentOrchestrator (not hardcoded)
3. Replanning: True closed-loop replan with max_replans protection
4. Context isolation: Each agent gets independent context
5. Handoff artifacts: Structured transfer of outputs between agents
6. Exponential backoff: RetryPolicy with configurable backoff
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from pathlib import Path
from typing import Any, Callable, Awaitable

from core.models import (
    DAG,
    DAGNode,
    NodeStatus,
    EvalStatus,
    ExecutionEvent,
    FailureDecision,
    HandoffArtifact,
)
from core.config import NodeTimeoutConfig
from core.exceptions import PendingApprovalError
from core.exceptions import BudgetExhaustedError
from backend.lifecycle import BackendManager
from memory.manager import MemoryManager
from evaluator.engine import EvaluatorEngine
from core.artifact_handoff import ArtifactHandoffService
from core.quality_gate import QualityGate
from core.retry_policy import RetryPolicyEngine
from core.watchdog import WatchdogService
from core.node_executor import NodeExecutor
from core.budget_manager import BudgetManager
from core.dag_checkpoint import CheckpointManager
from monitoring.otel import start_span  # noqa: E402 — optional OTel (#509)


EventHandler = Callable[[ExecutionEvent], Awaitable[None]]
ReplanHandler = Callable[[DAG, str], Awaitable[DAG]]

logger = logging.getLogger(__name__)


class DAGExecutionEngine:
    """
    Executes a DAG by:
    1. Computing topological levels
    2. Running nodes at each level in parallel (up to max_parallel)
    3. Waiting for all to complete before next level
    4. Handling failures via orchestrator callback (retry / skip / abort / replan)
    5. Supporting true replanning with max_replans limit and DAG result merging
    """

    def __init__(
        self,
        agent_executor: Callable[[DAGNode, list[HandoffArtifact]], Awaitable[dict]],
        failure_handler: Callable[[DAG, str, str], Awaitable[FailureDecision]],
        replan_handler: ReplanHandler | None = None,
        max_replans: int = 3,
        max_parallel: int = 5,
        evaluator: EvaluatorEngine | None = None,
        artifact_path: str = "./data/artifacts",
        work_dir: str | None = None,
        job_timeout: float | None = None,
        # M2.0: Watchdog configuration
        heartbeat_interval_sec: float = 30.0,
        heartbeat_miss_threshold: int = 5,
        enable_watchdog: bool = True,
        # M3.2: Memory integration
        memory_manager: MemoryManager | None = None,
        session_id: str | None = None,
        # Retry backoff configuration
        backoff_base: float = 2.0,
        backoff_cap: float = 60.0,
        # Per-agent-type heartbeat overrides: {agent_type: (interval, threshold)}
        watchdog_overrides: dict[str, tuple[float, int]] | None = None,
        # Per-agent-type alert thresholds: {agent_type: min_missed_for_alert}
        alert_thresholds: dict[str, int] | None = None,
        # M3.4: Node timeout configuration (#360)
        node_timeout_config: NodeTimeoutConfig | None = None,
        # R3: Backend manager for workspace isolation and cleanup (#176, #240)
        backend_manager: BackendManager | None = None,
        # Job/run identifiers for workspace isolation (#176 PR2)
        job_id: str = "",
        run_id: str = "",
        # #455: DAG execution state persistence for crash recovery
        checkpoint_dir: str = "./data/dag_progress",
        # M4.0: Backend registry for per-node backend selection
        backend_registry: Any | None = None,
        # M4.2: Token budget manager
        budget_manager: BudgetManager | None = None,
    ):
        # Note: agent_executor is stored in NodeExecutor (created below).
        # The .agent_executor property proxies to it.
        self.failure_handler = failure_handler
        self.replan_handler = replan_handler
        self.max_replans = max_replans
        self.max_parallel = max_parallel
        # Note: evaluator is stored in NodeExecutor (created below).
        # The .evaluator property proxies to it.
        self.artifact_path = artifact_path
        self.work_dir = work_dir
        self.job_timeout = job_timeout
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.event_handlers: list[EventHandler] = []
        # M2.0: Watchdog delegated to WatchdogService (#177 PR4).
        self._watchdog = WatchdogService(
            heartbeat_interval_sec=heartbeat_interval_sec,
            heartbeat_miss_threshold=heartbeat_miss_threshold,
            enabled=enable_watchdog,
            watchdog_overrides=watchdog_overrides or {},
            alert_thresholds=alert_thresholds or {},
            emit_func=self._emit,
        )
        self._running_tasks: dict[str, asyncio.Task] = {}
        # M3.2: Memory integration
        self.memory_manager = memory_manager
        self._session_id = session_id
        # M3.4: Node timeout configuration (#360)
        self._node_timeout_config = node_timeout_config
        # Dedicated thread pool for evaluator calls — avoids global pool
        # join timeout warnings on event loop exit.
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_parallel,
            thread_name_prefix="dag-engine",
        )
        # Best-attempt tracking delegated to RetryPolicyEngine (#177 PR4).
        self._retry_policy = RetryPolicyEngine()
        # Quality gate delegated to QualityGate service (#177 PR4).
        self._quality_gate = QualityGate(retry_policy=self._retry_policy)
        # Artifact handoff delegated to ArtifactHandoffService (#177 PR4).
        self._artifact_handoff = ArtifactHandoffService(
            memory_manager=memory_manager,
            session_id=session_id,
        )
        # Node execution delegated to NodeExecutor (#177 PR5).
        self._node_executor = NodeExecutor(
            agent_executor=agent_executor,
            emit_func=self._emit,
            watchdog=self._watchdog,
            evaluator=evaluator,
            artifact_path=artifact_path,
            work_dir=work_dir,
            quality_gate=self._quality_gate,
            artifact_handoff=self._artifact_handoff,
            node_timeout_config=node_timeout_config,
            backend_manager=backend_manager,
            job_id=job_id,
            run_id=run_id,
            backoff_base=backoff_base,
            backoff_cap=backoff_cap,
            backend_registry=backend_registry,
            session_id=session_id or "",
            budget_manager=budget_manager,
        )
        # R3: Backend manager for workspace isolation and cleanup (#176, #240)
        self.backend_manager = backend_manager
        # Job/run identifiers for per-node workspace isolation (#176 PR2)
        self._job_id = job_id
        self._run_id = run_id
        # #455: DAG execution state persistence for crash recovery
        self._checkpoint = CheckpointManager(checkpoint_dir, session_id)
        # M4.2: Budget manager for token tracking
        self._budget_manager = budget_manager

    def on_event(self, handler: EventHandler) -> None:
        """Register an event handler for execution monitoring."""
        self.event_handlers.append(handler)

    # -- Accessor properties for sub-services (inlined from DAGCompatMixin) --

    @property
    def agent_executor(self):
        """Agent executor callable, delegated to NodeExecutor."""
        return self._node_executor.agent_executor

    @agent_executor.setter
    def agent_executor(self, value):
        self._node_executor.agent_executor = value

    @property
    def evaluator(self):
        """Evaluator engine, delegated to NodeExecutor."""
        return self._node_executor.evaluator

    @evaluator.setter
    def evaluator(self, value):
        self._node_executor.evaluator = value

    @property
    def heartbeat_interval_sec(self) -> float:
        return self._watchdog._interval_sec

    @property
    def heartbeat_miss_threshold(self) -> int:
        return self._watchdog._miss_threshold

    @property
    def enable_watchdog(self) -> bool:
        return self._watchdog._enabled

    @property
    def _running_nodes(self) -> dict[str, Any]:
        return self._watchdog._running_nodes

    def _get_heartbeat_settings(self, agent_type: str) -> tuple[float, int]:
        return self._watchdog.get_heartbeat_settings(agent_type)

    def _get_alert_threshold(self, agent_type: str) -> int:
        return self._watchdog.get_alert_threshold(agent_type)

    @property
    def _best_attempts(self) -> dict[str, dict]:
        """Best-attempt tracking data, delegated to RetryPolicyEngine."""
        return self._retry_policy._best_attempts

    async def _emit(self, event: ExecutionEvent) -> None:
        """Emit execution event to all handlers."""
        for handler in self.event_handlers:
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                # Don't let event handlers break execution, but keep traceability.
                logger.warning("event handler failed: %s: %s", type(exc).__name__, exc)

    def _skip_remaining(self, dag: DAG, levels: list[list[str]], from_level: int) -> None:
        """Mark all pending nodes from from_level onward as SKIPPED."""
        for remaining_level in levels[from_level:]:
            for nid in remaining_level:
                if dag.nodes[nid].status == NodeStatus.PENDING:
                    dag.update_node(nid, status=NodeStatus.SKIPPED)

    def _merge_dag_results(self, old_dag: DAG, new_dag: DAG) -> DAG:
        """
        Merge two DAGs, preserving successful node results from old_dag.

        For each node that succeeded in old_dag and also exists in new_dag,
        copy over its status, result, output_artifacts, and timestamps so
        the re-executed plan does not re-run already-completed work.

        Nodes in old_dag that don't exist in new_dag are also preserved
        so the execution summary counts ALL nodes, not just the replan
        subset (#720).  Their edges are also preserved so
        topological_levels() orders them correctly (#728).
        """
        merged = new_dag
        for node_id, node in old_dag.nodes.items():
            if node_id in merged.nodes:
                # Node exists in both — preserve success state
                if QualityGate.is_terminal_success(node.status):
                    merged.update_node(
                        node_id,
                        status=node.status,
                        result=node.result,
                        output_artifacts=node.output_artifacts,
                        started_at=node.started_at,
                        completed_at=node.completed_at,
                    )
            else:
                # Node only in old DAG — preserve it so summary is
                # accurate (#720).  Pending nodes become SKIPPED since
                # the replan replaced them.
                if node.status == NodeStatus.PENDING:
                    merged.add_node(node.model_copy(update={
                        "status": NodeStatus.SKIPPED,
                    }))
                else:
                    merged.add_node(node.model_copy())

        # #728: Preserve old edges for nodes that were carried over.
        # Without edges, topological_levels() can't order preserved
        # nodes correctly, causing _skip_remaining to miss them.
        merged_edge_set = {
            (e.from_node, e.to_node) for e in merged.edges
        }
        for edge in old_dag.edges:
            if (
                edge.from_node in merged.nodes
                and edge.to_node in merged.nodes
                and (edge.from_node, edge.to_node) not in merged_edge_set
            ):
                merged.edges.append(edge.model_copy())
                merged_edge_set.add((edge.from_node, edge.to_node))

        return merged

    # ------------------------------------------------------------------
    # M2.0: Watchdog
    # ------------------------------------------------------------------

    async def _watchdog_loop(self) -> None:
        """Proxy to WatchdogService._loop for backward compat."""
        await self._watchdog._loop()

    def _start_watchdog(self) -> None:
        """Proxy to WatchdogService.start for backward compat."""
        self._watchdog.start()

    def _stop_watchdog(self) -> None:
        """Proxy to WatchdogService.stop for backward compat."""
        self._watchdog.stop()

    async def execute(self, dag: DAG) -> DAG:
        """Execute the full DAG with replanning support.

        Loads checkpointed node completions on start and skips them.
        Cleans up checkpoint on successful completion.
        """
        with start_span("dag.execute", {
            "dag.node_count": len(dag.nodes),
            "dag.edge_count": len(dag.edges),
        }) as span:
            return await self._execute_inner(dag, span)

    async def _execute_inner(self, dag: DAG, span) -> DAG:
        """Inner execute logic wrapped by OTel span (#509)."""
        try:
            levels = dag.topological_levels()
        except ValueError as e:
            raise ValueError(f"Invalid DAG: {e}")

        # M2.0: Start watchdog
        self._watchdog.clear()
        self._start_watchdog()

        # R3: Auto-serialize parallel generators without ownership contracts (#272 EC4)
        levels = self._auto_serialize_parallel_generators(dag, levels)

        # #455: Restore completed nodes from checkpoint
        completed_nodes = self._load_completed_nodes()
        if completed_nodes:
            restored_count = 0
            for node_id, result_data in completed_nodes.items():
                if node_id in dag.nodes:
                    updates: dict[str, Any] = {"status": NodeStatus.SUCCESS}
                    if result_data:
                        updates["result"] = result_data
                    dag.update_node(node_id, **updates)
                    restored_count += 1
            if restored_count:
                logger.info(
                    "Restored %d completed nodes from checkpoint", restored_count,
                )

        replan_count = 0
        level_idx = 0

        try:
            while level_idx < len(levels):
                level = levels[level_idx]
                # #455: Skip already-completed nodes
                pending = [
                    nid for nid in level
                    if dag.nodes[nid].status != NodeStatus.SUCCESS
                ]
                if not pending:
                    logger.info(
                        "Level %d/%d fully completed (checkpoint), skipping",
                        level_idx + 1, len(levels),
                    )
                    level_idx += 1
                    continue

                semaphore = asyncio.Semaphore(self.max_parallel)

                logger.info(
                    "Executing level %d/%d: %s (%d pending, %d completed)",
                    level_idx + 1, len(levels), level,
                    len(pending), len(level) - len(pending),
                )

                async def run_with_limit(
                    node_id: str, sem: asyncio.Semaphore, dag_ref: DAG,
                ) -> None:
                    async with sem:
                        await self._node_executor.execute_node(dag_ref, node_id)

                tasks = [run_with_limit(nid, semaphore, dag) for nid in pending]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                # #455: Persist completed nodes after each level
                for nid in pending:
                    if QualityGate.is_terminal_success(dag.nodes[nid].status):
                        self._persist_node_completion(nid, dag.nodes[nid].result)

                # Check for CancelledError (timeout/signal) — propagate (#304)
                for r in results:
                    if isinstance(r, asyncio.CancelledError):
                        logger.error(
                            "Node execution cancelled at level %d: %s",
                            level_idx, r,
                        )
                        raise r

                # PendingApprovalError — re-raise so callers (worker_executor,
                # service) can poll for approval and resume (#666).
                for r in results:
                    if isinstance(r, PendingApprovalError):
                        raise r

                # M4.2: Check budget exhaustion after level execution
                budget_exhausted = any(
                    isinstance(r, BudgetExhaustedError) for r in results
                )
                if budget_exhausted:
                    logger.warning(
                        "Budget exhausted after level %d: %d/%d tokens. "
                        "Skipping remaining levels.",
                        level_idx + 1,
                        self._budget_manager.used_total_tokens if self._budget_manager else 0,
                        self._budget_manager.config.total_tokens if self._budget_manager else 0,
                    )
                    self._skip_remaining(dag, levels, level_idx + 1)
                    return dag

                failed_in_level = [
                    nid for nid in pending
                    if dag.nodes[nid].status == NodeStatus.FAILED
                ]

                if failed_in_level:
                    for failed_id in failed_in_level:
                        decision = await self.failure_handler(
                            dag, failed_id, dag.nodes[failed_id].error,
                        )

                        # Emit audit event for the failure decision
                        await self._emit(ExecutionEvent(
                            node_id=failed_id,
                            event_type="failure_decision",
                            details={
                                "action": decision.action,
                                "reasoning": decision.reasoning,
                                "error": dag.nodes[failed_id].error,
                            },
                        ))

                        if decision.action == "abort":
                            self._skip_remaining(dag, levels, level_idx + 1)
                            return dag

                        elif decision.action == "retry":
                            # Exponential backoff before retry
                            backoff = self._compute_backoff(dag.nodes[failed_id].retry_count)
                            if backoff > 0:
                                await asyncio.sleep(backoff)

                            node = dag.nodes[failed_id]

                            # ── Evaluator feedback loop ──────────────────────────
                            # If an evaluator fails, retrying the evaluator alone is
                            # useless — it will just re-run the same broken tests.
                            # Instead, roll back to the target generator, feed it
                            # the evaluation feedback, let it fix the code, then
                            # re-run the evaluator.
                            if node.agent_type == "evaluator":
                                target_id = self._find_evaluator_target(dag, failed_id)
                                # #630: Skip UPSTREAM_RETRY when the target node
                                # already succeeded. Re-triggering a successful
                                # node can cause regression if the API is in a
                                # low-quality phase (degenerate empty-args).
                                target_succeeded = (
                                    target_id
                                    and dag.nodes[target_id].agent_type == "generator"
                                    and QualityGate.is_terminal_success(dag.nodes[target_id].status)
                                )
                                if target_succeeded:
                                    # Target already passed — just retry the evaluator
                                    logger.info(
                                        "Evaluator %s failed but target %s already "
                                        "succeeded — retrying evaluator directly (#630)",
                                        failed_id, target_id,
                                    )
                                    dag.update_node(
                                        failed_id,
                                        status=NodeStatus.RETRYING,
                                        error="",
                                    )
                                    await self._node_executor.execute_node(dag, failed_id)
                                elif target_id and dag.nodes[target_id].agent_type == "generator":
                                    # Check retry budget for upstream generator
                                    gen_node = dag.nodes[target_id]
                                    if gen_node.retry_count >= gen_node.max_retries:
                                        # No budget left; retry evaluator directly
                                        dag.update_node(
                                            failed_id,
                                            status=NodeStatus.RETRYING,
                                            error="",
                                        )
                                        await self._node_executor.execute_node(dag, failed_id)
                                    else:
                                        await self._emit(ExecutionEvent(
                                            node_id=target_id,
                                            event_type="upstream_retry",
                                            details={
                                                "reason": "evaluator_failed",
                                                "evaluator": failed_id,
                                                "feedback": node.eval_feedback[:3000],
                                            },
                                        ))
                                        # Retry generator with feedback.
                                        # Cap retry_count so _execute_single_node's
                                        # internal retry loop gives exactly one attempt.
                                        dag.update_node(
                                            target_id,
                                            retry_count=gen_node.max_retries - 1,
                                            status=NodeStatus.RETRYING,
                                            error="",
                                            eval_feedback=node.eval_feedback,
                                        )
                                        await self._node_executor.execute_node(dag, target_id)

                                        if QualityGate.is_terminal_success(dag.nodes[target_id].status):
                                            # Re-run evaluator after generator fix
                                            dag.update_node(
                                                failed_id,
                                                status=NodeStatus.RETRYING,
                                                error="",
                                            )
                                            await self._node_executor.execute_node(dag, failed_id)
                                else:
                                    # No upstream generator found; retry evaluator directly
                                    dag.update_node(
                                        failed_id,
                                        status=NodeStatus.RETRYING,
                                        error="",
                                    )
                                    await self._node_executor.execute_node(dag, failed_id)
                            else:
                                # Normal retry: retry the failed node itself
                                # Preserve eval_feedback so agent sees what went wrong
                                # on retry (#599).
                                retry_updates: dict[str, Any] = {
                                    "status": NodeStatus.RETRYING,
                                    "error": "",
                                }
                                fb = dag.nodes[failed_id].eval_feedback
                                if fb:
                                    retry_updates["eval_feedback"] = fb
                                dag.update_node(failed_id, **retry_updates)
                                await self._node_executor.execute_node(dag, failed_id)

                            # #455: Persist retried node if it succeeded
                            if QualityGate.is_terminal_success(dag.nodes[failed_id].status):
                                self._persist_node_completion(
                                    failed_id, dag.nodes[failed_id].result,
                                )

                            # Check if retry resolved this failure
                            if not QualityGate.is_terminal_success(dag.nodes[failed_id].status):
                                # Retry failed — ask failure_handler for a
                                # fallback decision (skip / replan / abort)
                                # instead of leaving the node FAILED and
                                # silently skipping all downstream (#670).
                                fallback = await self.failure_handler(
                                    dag, failed_id,
                                    dag.nodes[failed_id].error or "",
                                )
                                await self._emit(ExecutionEvent(
                                    node_id=failed_id,
                                    event_type="failure_decision",
                                    details={
                                        "action": fallback.action,
                                        "reasoning": fallback.reasoning,
                                        "trigger": "retry_exhausted_fallback",
                                    },
                                ))
                                # #747: If LLM returns 'retry' after exhaustion,
                                # remap to 'replan' (first choice) or 'skip'.
                                if fallback.action == "retry":
                                    if (
                                        replan_count < self.max_replans
                                        and self.replan_handler
                                    ):
                                        fallback = FailureDecision(
                                            action="replan",
                                            reasoning=(
                                                "LLM recommended retry after "
                                                "exhaustion — auto-upgraded to "
                                                "replan (#747)"
                                            ),
                                        )
                                    else:
                                        fallback = FailureDecision(
                                            action="skip",
                                            reasoning=(
                                                "LLM recommended retry after "
                                                "exhaustion — auto-downgraded "
                                                "to skip (#747)"
                                            ),
                                        )
                                if fallback.action == "abort":
                                    self._skip_remaining(dag, levels, level_idx + 1)
                                    return dag
                                elif fallback.action == "skip":
                                    dag.update_node(failed_id, status=NodeStatus.SKIPPED)
                                elif fallback.action == "replan":
                                    if replan_count >= self.max_replans:
                                        self._skip_remaining(dag, levels, level_idx + 1)
                                        return dag
                                    dag, levels, level_idx, replan_count, replanned = (
                                        await self._try_execute_replan(
                                            dag, failed_id, levels, level_idx, replan_count,
                                        )
                                    )
                                    if replanned:
                                        break
                                    dag.update_node(failed_id, status=NodeStatus.SKIPPED)
                                else:
                                    logger.warning(
                                        "failure_handler returned '%s' after retry "
                                        "exhaustion; skipping node %s",
                                        fallback.action, failed_id,
                                    )
                                    dag.update_node(failed_id, status=NodeStatus.SKIPPED)

                        elif decision.action == "skip":
                            dag.update_node(failed_id, status=NodeStatus.SKIPPED)

                        elif decision.action == "replan":
                            if replan_count >= self.max_replans:
                                dag.update_node(
                                    failed_id,
                                    error=(
                                        f"Max replans ({self.max_replans}) reached"
                                    ),
                                )
                                self._skip_remaining(dag, levels, level_idx + 1)
                                return dag

                            dag, levels, level_idx, replan_count, replanned = (
                                await self._try_execute_replan(
                                    dag, failed_id, levels, level_idx, replan_count,
                                )
                            )
                            if replanned:
                                break  # Break out of failed_in_level loop
                            # No replan handler available — treat as abort
                            self._skip_remaining(dag, levels, level_idx + 1)
                            return dag
                    else:
                        # All failed nodes in this level were handled without replan
                        # Continue to next level
                        level_idx += 1
                        continue
                    # If we hit the break (replan), we continue the while loop
                    # without incrementing level_idx (it's reset to 0 above)
                    pass
                else:
                    level_idx += 1

            # #455: All levels completed — clean up checkpoint
            self._cleanup_checkpoint()

            return dag
        except asyncio.CancelledError:
            # External cancellation (timeout, signal) — log before cleanup (#304)
            logger.error(
                "DAG execution cancelled at level %d/%d",
                level_idx + 1, len(levels),
            )
            raise
        except Exception:
            logger.exception(
                "DAG execution failed at level %d/%d",
                level_idx + 1, len(levels),
            )
            raise
        finally:
            # M2.0: Stop watchdog
            self._stop_watchdog()
            self._watchdog.clear()
            self._running_tasks = {}
            # Shutdown dedicated thread pool to avoid RuntimeWarning on exit
            self._executor.shutdown(wait=False)

    async def _try_execute_replan(
        self, dag: DAG, failed_id: str,
        levels: list, level_idx: int, replan_count: int,
    ) -> tuple[DAG, list, int, int, bool]:
        """Attempt replan via ``replan_handler``.

        Returns ``(dag, levels, level_idx, replan_count, initiated)``.
        *initiated* is True when a replan was started — caller should
        ``break`` out of the ``failed_in_level`` loop so the outer
        ``while`` re-enters from level 0.
        """
        if self.replan_handler is None:
            return dag, levels, level_idx, replan_count, False

        new_dag = await self.replan_handler(dag, failed_id)
        dag = self._merge_dag_results(dag, new_dag)
        replan_count += 1
        levels = dag.topological_levels()
        level_idx = 0
        return dag, levels, level_idx, replan_count, True

    # -- #455: DAG execution state persistence ----------------------------

    def _checkpoint_file(self) -> Path:
        """Backward-compat proxy to CheckpointManager file path."""
        return self._checkpoint._file_path()

    def _persist_node_completion(
        self, node_id: str, result: dict | None,
    ) -> None:
        """Append node completion record to checkpoint file (#455)."""
        self._checkpoint.persist_node_completion(node_id, result)

    def _load_completed_nodes(self) -> dict[str, dict | None]:
        """Load completed node IDs and their results from checkpoint (#455)."""
        return self._checkpoint.load_completed_nodes()

    def _cleanup_checkpoint(self) -> None:
        """Remove checkpoint file after successful DAG completion (#455)."""
        self._checkpoint.cleanup()

    def _find_evaluator_target(self, dag: DAG, eval_node_id: str) -> str | None:
        """
        Find the generator node that an evaluator is responsible for assessing.

        Heuristic: look for a generator node that is a direct dependency
        (i.e. has an edge → evaluator) and whose ID/domain matches the evaluator.
        """
        # Candidate 1: direct upstream generator with an edge to the evaluator
        candidates = [
            e.from_node for e in dag.edges
            if e.to_node == eval_node_id
            and dag.nodes[e.from_node].agent_type == "generator"
        ]
        if len(candidates) == 1:
            return candidates[0]

        # Candidate 2: name-based matching among upstream nodes only
        # (eval_backend ↔ impl_backend / gen_backend)
        eval_name = eval_node_id.lower().replace("eval_", "")
        upstream_ids = {e.from_node for e in dag.edges if e.to_node == eval_node_id}
        for nid in upstream_ids:
            node = dag.nodes[nid]
            if node.agent_type != "generator":
                continue
            gen_name = nid.lower().replace("impl_", "").replace("gen_", "")
            if gen_name == eval_name:
                return nid

        # Candidate 3: any direct upstream generator
        if candidates:
            return candidates[0]

        return None

    # -- File snapshot for regression rollback (#212) ----------------------

    def _compute_backoff(self, retry_count: int) -> float:
        """Compute exponential backoff delay in seconds."""
        return self._retry_policy.compute_backoff(
            retry_count, base=self.backoff_base, cap=self.backoff_cap,
        )

    def _auto_serialize_parallel_generators(
        self,
        dag: DAG,
        levels: list[list[str]],
    ) -> list[list[str]]:
        """Auto-serialize parallel generators without ownership contracts (#272 EC4).

        When parallel generators at the same level have no owned_files AND
        no edges at all (standalone generators), insert implicit HARD edges
        to serialize them, preventing write conflicts. Generators that are
        part of an existing dependency structure (have incoming or outgoing
        edges) are left alone — they were explicitly planned as parallel.

        Returns recomputed levels if edges were added.
        """
        from core.models import DependencyType

        edges_added = False
        for level in levels:
            generators = [
                nid for nid in level
                if dag.nodes[nid].agent_type == "generator"
            ]
            if len(generators) < 2:
                continue

            # Only auto-serialize generators that lack ownership contracts
            no_contract = [nid for nid in generators if not dag.nodes[nid].owned_files]
            if not no_contract:
                continue  # All have contracts → safe to parallelize

            # Only serialize standalone generators (no incoming or outgoing edges).
            # Generators with existing edges are part of an intentional
            # dependency structure and should not be modified.
            standalone = []
            for nid in no_contract:
                has_edge = any(
                    e.from_node == nid or e.to_node == nid
                    for e in dag.edges
                )
                if not has_edge:
                    standalone.append(nid)

            if len(standalone) < 2:
                continue  # Not enough standalone generators to serialize

            # Auto-serialize: add implicit edges between standalone generators
            for i in range(1, len(standalone)):
                from_id = standalone[i - 1]
                to_id = standalone[i]
                dag.add_edge(from_id, to_id, dependency_type=DependencyType.HARD)
                edges_added = True
                logger.info(
                    "Auto-serialized standalone generators: %s → %s "
                    "(no ownership contracts, no existing edges)",
                    from_id, to_id,
                )

        if edges_added:
            return dag.topological_levels()
        return levels

    def get_execution_summary(self, dag: DAG) -> dict[str, Any]:
        """Generate a summary of DAG execution results."""
        total = len(dag.nodes)
        success = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.SUCCESS)
        partial_pass = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.PARTIAL_PASS)
        warned = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.WARNED)
        failed = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.FAILED)
        skipped = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.SKIPPED)
        # #676: Evaluator failures are non-critical (informational only).
        # Exclude them from all_succeeded so implementation success
        # is not masked by evaluation timeouts.
        non_eval_failed = sum(
            1 for n in dag.nodes.values()
            if n.status == NodeStatus.FAILED and n.agent_type != "evaluator"
        )

        summary = {
            "total_nodes": total,
            "success": success,
            "partial_pass": partial_pass,
            "warned": warned,
            "failed": failed,
            "skipped": skipped,
            "all_succeeded": (
                non_eval_failed == 0
                and skipped == 0
                and partial_pass == 0
            ),
            "node_details": {
                nid: {
                    "status": n.status.value,
                    "agent": n.agent_type,
                    "duration_ms": (
                        (n.completed_at - n.started_at).total_seconds() * 1000
                        if n.completed_at and n.started_at else None
                    ),
                    **(
                        {"eval_feedback": n.eval_feedback}
                        if n.eval_feedback else {}
                    ),
                }
                for nid, n in dag.nodes.items()
            },
        }

        # M4.2: Token usage aggregation
        total_input = 0
        total_output = 0
        for n in dag.nodes.values():
            tu = n.token_usage if hasattr(n, "token_usage") else {}
            total_input += tu.get("input_tokens", 0)
            total_output += tu.get("output_tokens", 0)
        summary["token_usage"] = {
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_tokens": total_input + total_output,
        }
        # M4.6: Aggregate actual_tokens
        actual_total = sum(
            n.actual_tokens for n in dag.nodes.values()
            if hasattr(n, "actual_tokens")
        )
        if actual_total > 0:
            summary["token_usage"]["actual_tokens_total"] = actual_total
        if self._budget_manager and not self._budget_manager.config.is_unlimited:
            summary["budget"] = self._budget_manager.to_dict()

        return summary
