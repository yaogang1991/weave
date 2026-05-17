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
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine

from core.models import (
    DAG,
    DAGNode,
    NodeStatus,
    EvalStatus,
    ExecutionEvent,
    FailureDecision,
    HandoffArtifact,
)
from core.exceptions import PendingApprovalError
from core.artifact_handoff import ArtifactHandoffService
from core.quality_gate import QualityGate
from core.retry_policy import RetryPolicyEngine
from core.watchdog import WatchdogService
from core.node_executor import NodeExecutor


EventHandler = Callable[[ExecutionEvent], Coroutine[Any, Any, None]]
ReplanHandler = Callable[[DAG, str], Coroutine[Any, Any, DAG]]

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
        agent_executor: Callable[[DAGNode, list[HandoffArtifact]], Coroutine[Any, Any, dict]],
        failure_handler: Callable[[DAG, str, str], Coroutine[Any, Any, FailureDecision]],
        replan_handler: ReplanHandler | None = None,
        max_replans: int = 3,
        max_parallel: int = 5,
        evaluator: Any | None = None,
        artifact_path: str = "./data/artifacts",
        work_dir: str | None = None,
        job_timeout: float | None = None,
        # M2.0: Watchdog configuration
        heartbeat_interval_sec: float = 30.0,
        heartbeat_miss_threshold: int = 5,
        enable_watchdog: bool = True,
        # M3.2: Memory integration
        memory_manager: Any | None = None,
        session_id: str | None = None,
        # Retry backoff configuration
        backoff_base: float = 2.0,
        backoff_cap: float = 60.0,
        # Per-agent-type heartbeat overrides: {agent_type: (interval, threshold)}
        watchdog_overrides: dict[str, tuple[float, int]] | None = None,
        # Per-agent-type alert thresholds: {agent_type: min_missed_for_alert}
        alert_thresholds: dict[str, int] | None = None,
        # M3.4: Node timeout configuration (#360)
        node_timeout_config: Any | None = None,
        # R3: Backend manager for workspace isolation and cleanup (#176, #240)
        backend_manager: Any | None = None,
        # Job/run identifiers for workspace isolation (#176 PR2)
        job_id: str = "",
        run_id: str = "",
        # #455: DAG execution state persistence for crash recovery
        checkpoint_dir: str = "./data/dag_progress",
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
        )
        # R3: Backend manager for workspace isolation and cleanup (#176, #240)
        self.backend_manager = backend_manager
        # Job/run identifiers for per-node workspace isolation (#176 PR2)
        self._job_id = job_id
        self._run_id = run_id
        # #455: DAG execution state persistence for crash recovery
        self._checkpoint_dir = Path(checkpoint_dir)
        self._checkpoint_dir_created = False

    # -- Backward-compat proxies for extracted services (#177 PR4) ------------

    @property
    def agent_executor(self):
        """Proxy to NodeExecutor's agent_executor for backward compat.

        Tests may override engine.agent_executor; this ensures the node
        executor sees the updated value (#177 PR5).
        """
        return self._node_executor.agent_executor

    @agent_executor.setter
    def agent_executor(self, value):
        self._node_executor.agent_executor = value

    @property
    def evaluator(self):
        """Proxy to NodeExecutor's evaluator for backward compat."""
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
    def _running_nodes(self) -> dict[str, DAGNode]:
        return self._watchdog._running_nodes

    @property
    def _best_attempts(self) -> dict[str, dict]:
        """Proxy to RetryPolicyEngine._best_attempts for backward compat."""
        return self._retry_policy._best_attempts

    def _get_heartbeat_settings(self, agent_type: str) -> tuple[float, int]:
        """Proxy to WatchdogService for backward compat."""
        return self._watchdog.get_heartbeat_settings(agent_type)

    def _get_alert_threshold(self, agent_type: str) -> int:
        """Proxy to WatchdogService for backward compat."""
        return self._watchdog.get_alert_threshold(agent_type)

    def on_event(self, handler: EventHandler) -> None:
        """Register an event handler for execution monitoring."""
        self.event_handlers.append(handler)

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

    @staticmethod
    def _eval_status_to_node_status(eval_status: EvalStatus) -> NodeStatus:
        """Map EvalStatus from evaluator to NodeStatus for DAG nodes (#270).

        Delegated to QualityGate (#177 PR4).
        """
        return QualityGate.eval_status_to_node_status(eval_status)

    @staticmethod
    def _is_terminal_success(status: NodeStatus) -> bool:
        """Check if a node status represents a successful terminal state (#270).

        Delegated to QualityGate (#177 PR4).
        """
        return QualityGate.is_terminal_success(status)

    @staticmethod
    def _is_test_file_exists_criterion(criterion: str | object) -> bool:
        """Check if a criterion requires test files to exist (#247).

        Delegated to QualityGate (#177 PR4).
        """
        return QualityGate.is_test_file_exists_criterion(criterion)

    def _skip_remaining(self, dag: DAG, levels: list[list[str]], from_level: int) -> None:
        """Mark all pending nodes from from_level onward as SKIPPED."""
        for remaining_level in levels[from_level:]:
            for nid in remaining_level:
                if dag.nodes[nid].status == NodeStatus.PENDING:
                    dag.nodes[nid].status = NodeStatus.SKIPPED

    def _merge_dag_results(self, old_dag: DAG, new_dag: DAG) -> DAG:
        """
        Merge two DAGs, preserving successful node results from old_dag.

        For each node that succeeded in old_dag and also exists in new_dag,
        copy over its status, result, output_artifacts, and timestamps so
        the re-executed plan does not re-run already-completed work.
        """
        merged = new_dag
        for node_id, node in old_dag.nodes.items():
            if self._is_terminal_success(node.status) and node_id in merged.nodes:
                merged.nodes[node_id].status = node.status
                merged.nodes[node_id].result = node.result
                merged.nodes[node_id].output_artifacts = node.output_artifacts
                merged.nodes[node_id].started_at = node.started_at
                merged.nodes[node_id].completed_at = node.completed_at
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
                    dag.nodes[node_id].status = NodeStatus.SUCCESS
                    if result_data:
                        dag.nodes[node_id].result = result_data
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
                    if self._is_terminal_success(dag.nodes[nid].status):
                        self._persist_node_completion(nid, dag.nodes[nid].result)

                # Check for CancelledError (timeout/signal) — propagate (#304)
                for r in results:
                    if isinstance(r, asyncio.CancelledError):
                        logger.error(
                            "Node execution cancelled at level %d: %s",
                            level_idx, r,
                        )
                        raise r

                # Check for PendingApprovalError — must re-raise immediately.
                for r in results:
                    if isinstance(r, PendingApprovalError):
                        raise r

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
                                if target_id and dag.nodes[target_id].agent_type == "generator":
                                    # Check retry budget for upstream generator
                                    gen_node = dag.nodes[target_id]
                                    if gen_node.retry_count >= gen_node.max_retries:
                                        # No budget left; retry evaluator directly
                                        node.status = NodeStatus.RETRYING
                                        node.error = ""
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
                                        gen_node.retry_count = gen_node.max_retries - 1
                                        gen_node.status = NodeStatus.RETRYING
                                        gen_node.error = ""
                                        gen_node.eval_feedback = node.eval_feedback
                                        await self._node_executor.execute_node(dag, target_id)

                                        if self._is_terminal_success(dag.nodes[target_id].status):
                                            # Re-run evaluator after generator fix
                                            node.status = NodeStatus.RETRYING
                                            node.error = ""
                                            await self._node_executor.execute_node(dag, failed_id)
                                else:
                                    # No upstream generator found; retry evaluator directly
                                    node.status = NodeStatus.RETRYING
                                    node.error = ""
                                    await self._node_executor.execute_node(dag, failed_id)
                            else:
                                # Normal retry: retry the failed node itself
                                node.status = NodeStatus.RETRYING
                                node.error = ""
                                await self._node_executor.execute_node(dag, failed_id)

                            # #455: Persist retried node if it succeeded
                            if self._is_terminal_success(dag.nodes[failed_id].status):
                                self._persist_node_completion(
                                    failed_id, dag.nodes[failed_id].result,
                                )

                            # Check if retry resolved this failure
                            if not self._is_terminal_success(dag.nodes[failed_id].status):
                                # This failure was not resolved — but don't skip
                                # all remaining levels (#259). Instead, let
                                # downstream nodes decide via dependency check
                                # in _execute_single_node. Only abort (full
                                # skip) happens on explicit "abort" decision.
                                pass

                        elif decision.action == "skip":
                            dag.nodes[failed_id].status = NodeStatus.SKIPPED

                        elif decision.action == "replan":
                            if replan_count >= self.max_replans:
                                dag.nodes[failed_id].error = (
                                    f"Max replans ({self.max_replans}) reached"
                                )
                                self._skip_remaining(dag, levels, level_idx + 1)
                                return dag

                            if self.replan_handler is not None:
                                new_dag = await self.replan_handler(dag, failed_id)
                                dag = self._merge_dag_results(dag, new_dag)
                                replan_count += 1
                                # Recompute topological levels and restart from beginning
                                # so that newly added nodes are accounted for
                                levels = dag.topological_levels()
                                level_idx = 0
                                break  # Break out of failed_in_level loop
                            else:
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

    # -- #455: DAG execution state persistence ----------------------------

    def _checkpoint_file(self) -> Path:
        """Return checkpoint file path for current session."""
        if not self._checkpoint_dir_created:
            self._checkpoint_dir.mkdir(parents=True, exist_ok=True)
            self._checkpoint_dir_created = True
        return self._checkpoint_dir / f"{self._session_id}.jsonl"

    def _persist_node_completion(
        self, node_id: str, result: dict | None,
    ) -> None:
        """Append node completion record to checkpoint file (#455).

        Stores the full result dict so downstream nodes can access
        artifacts and data after crash recovery.
        """
        if not self._session_id:
            return
        path = self._checkpoint_file()
        entry = {
            "node_id": node_id,
            "status": "completed",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if result:
            entry["result"] = result
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError as exc:
            logger.warning(
                "Failed to persist node %s checkpoint: %s", node_id, exc,
            )

    def _load_completed_nodes(self) -> dict[str, dict | None]:
        """Load completed node IDs and their results from checkpoint (#455).

        Returns dict mapping node_id to its persisted result dict (or None).
        Corrupt entries are skipped.
        """
        if not self._session_id:
            return {}
        path = self._checkpoint_file()
        if not path.exists():
            return {}
        completed: dict[str, dict | None] = {}
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("status") == "completed":
                        completed[entry["node_id"]] = entry.get("result")
                except (json.JSONDecodeError, KeyError):
                    continue
        except OSError as exc:
            logger.warning(
                "Failed to load checkpoint for session %s: %s",
                self._session_id, exc,
            )
        return completed

    def _cleanup_checkpoint(self) -> None:
        """Remove checkpoint file after successful DAG completion (#455)."""
        if not self._session_id:
            return
        path = self._checkpoint_file()
        if path.exists():
            try:
                path.unlink()
                logger.info(
                    "Cleaned up checkpoint for session %s", self._session_id,
                )
            except OSError as exc:
                logger.warning(
                    "Failed to cleanup checkpoint for session %s: %s",
                    self._session_id, exc,
                )

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

    @staticmethod
    def _capture_file_snapshot(
        work_dir: str, artifacts: list[str],
    ) -> dict[str, str]:
        """Capture file contents of artifacts for later rollback."""
        return RetryPolicyEngine.capture_file_snapshot(work_dir, artifacts)

    @staticmethod
    def _restore_file_snapshot(
        work_dir: str, snapshot: dict[str, str],
    ) -> None:
        """Restore files from a previously captured snapshot."""
        return RetryPolicyEngine.restore_file_snapshot(work_dir, snapshot)

    def _compute_backoff(self, retry_count: int) -> float:
        """Compute exponential backoff delay in seconds."""
        return self._retry_policy.compute_backoff(
            retry_count, base=self.backoff_base, cap=self.backoff_cap,
        )

    @staticmethod
    def _requires_output_artifacts(node: DAGNode) -> bool:
        """Check whether a node is expected to produce output file artifacts."""
        return RetryPolicyEngine.requires_output_artifacts(node)

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

    # Codes that are purely formatting/whitespace and safe to tolerate on retry.
    # Delegated to RetryPolicyEngine (#177 PR4). Kept as alias for backward compat.
    _RETRY_TOLERABLE_CODES = RetryPolicyEngine.RETRY_TOLERABLE_CODES

    @classmethod
    def _is_retry_tolerable_lint_issue(cls, issue: str) -> bool:
        """Check if a lint issue is formatting-only and safe to tolerate."""
        return RetryPolicyEngine.is_tolerable_lint_issue(issue)

    # -- Backward-compat proxies for NodeExecutor (#177 PR5) ------------------

    async def _execute_single_node(self, dag: DAG, node_id: str) -> None:
        """Proxy to NodeExecutor.execute_node for backward compat."""
        await self._node_executor.execute_node(dag, node_id)

    @property
    def _get_node_timeout(self):
        """Proxy to NodeExecutor._get_node_timeout for backward compat."""
        return self._node_executor._get_node_timeout

    @_get_node_timeout.setter
    def _get_node_timeout(self, value):
        self._node_executor._get_node_timeout = value

    def _collect_input_artifacts(
        self,
        dag: DAG,
        node_id: str,
        failed_soft: list[str] | None = None,
    ) -> list[HandoffArtifact]:
        """Proxy to NodeExecutor._collect_input_artifacts for backward compat."""
        return self._node_executor._collect_input_artifacts(dag, node_id, failed_soft)

    def get_execution_summary(self, dag: DAG) -> dict[str, Any]:
        """Generate a summary of DAG execution results."""
        total = len(dag.nodes)
        success = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.SUCCESS)
        partial_pass = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.PARTIAL_PASS)
        warned = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.WARNED)
        failed = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.FAILED)
        skipped = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.SKIPPED)

        return {
            "total_nodes": total,
            "success": success,
            "partial_pass": partial_pass,
            "warned": warned,
            "failed": failed,
            "skipped": skipped,
            "all_succeeded": failed == 0 and skipped == 0,
            "node_details": {
                nid: {
                    "status": n.status.value,
                    "agent": n.agent_type,
                    "duration_ms": (
                        (n.completed_at - n.started_at).total_seconds() * 1000
                        if n.completed_at and n.started_at else None
                    ),
                }
                for nid, n in dag.nodes.items()
            },
        }
