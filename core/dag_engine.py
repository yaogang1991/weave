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
import functools
import logging
import os
import threading
import traceback
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

from core.models import (
    DAG,
    DAGNode,
    NodeStatus,
    NodeHealth,
    EvalStatus,
    ExecutionEvent,
    FailureDecision,
    HandoffArtifact,
    CriterionType,
    SuccessCriterion,
)
from core.exceptions import PendingApprovalError
from core.exceptions import RateLimitError, NodeTimeoutError


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
    ):
        self.agent_executor = agent_executor
        self.failure_handler = failure_handler
        self.replan_handler = replan_handler
        self.max_replans = max_replans
        self.max_parallel = max_parallel
        self.evaluator = evaluator
        self.artifact_path = artifact_path
        self.work_dir = work_dir
        self.job_timeout = job_timeout
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.event_handlers: list[EventHandler] = []
        # M2.0: Watchdog configuration
        self.heartbeat_interval_sec = heartbeat_interval_sec
        self.heartbeat_miss_threshold = heartbeat_miss_threshold
        self.enable_watchdog = enable_watchdog
        self._watchdog_overrides = watchdog_overrides or {}
        self._alert_thresholds = alert_thresholds or {}
        self._watchdog_task: asyncio.Task | None = None
        self._running_nodes: dict[str, DAGNode] = {}
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
        # Best-attempt tracking to prevent retry regression (#129).
        self._best_attempts: dict[str, dict] = {}
        # R3: Backend manager for workspace isolation and cleanup (#176, #240)
        self.backend_manager = backend_manager

    def _get_heartbeat_settings(self, agent_type: str) -> tuple[float, int]:
        """Return (interval_sec, miss_threshold) for the given agent type."""
        if agent_type in self._watchdog_overrides:
            return self._watchdog_overrides[agent_type]
        return self.heartbeat_interval_sec, self.heartbeat_miss_threshold

    def _get_alert_threshold(self, agent_type: str) -> int:
        """Minimum missed_count to emit heartbeat_missed event."""
        if agent_type in self._alert_thresholds:
            return self._alert_thresholds[agent_type]
        return 2

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
        """Map EvalStatus from evaluator to NodeStatus for DAG nodes (#270)."""
        mapping = {
            EvalStatus.CLEAN_PASS: NodeStatus.SUCCESS,
            EvalStatus.PARTIAL_PASS: NodeStatus.PARTIAL_PASS,
            EvalStatus.WARNED: NodeStatus.WARNED,
            EvalStatus.FAILED: NodeStatus.FAILED,
        }
        return mapping.get(eval_status, NodeStatus.SUCCESS)

    @staticmethod
    def _is_terminal_success(status: NodeStatus) -> bool:
        """Check if a node status represents a successful terminal state (#270).

        SUCCESS, PARTIAL_PASS, and WARNED all allow downstream to continue.
        """
        return status in (
            NodeStatus.SUCCESS,
            NodeStatus.PARTIAL_PASS,
            NodeStatus.WARNED,
        )

    @staticmethod
    def _is_test_file_exists_criterion(criterion: str | object) -> bool:
        """Check if a criterion requires test files to exist (#247)."""
        # Handle structured SuccessCriterion
        if hasattr(criterion, "type"):
            return getattr(criterion.type, "value", "") == "test_file_exists"
        # Handle string criteria
        if isinstance(criterion, str):
            lower = criterion.lower()
            return "test_file_exist" in lower or "test file exist" in lower
        return False

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
        """
        Watchdog coroutine: pure early-warning event source (#360 PR3).

        Monitors running nodes' heartbeats and emits events for
        observability. Does NOT kill nodes — node timeout is managed
        exclusively by _execute_with_timeout.

        Per-agent-type overrides are respected: generator nodes, for
        example, are allowed longer intervals than planner/evaluator.

        Alert events use a configurable threshold (default 50% of unhealthy
        threshold) to reduce noise from slow-but-healthy LLM APIs (#146).

        This runs as a background task during execute().
        """
        check_interval = self.heartbeat_interval_sec * 1.5
        while True:
            try:
                await asyncio.sleep(check_interval)
            except asyncio.CancelledError:
                return

            for node_id, node in list(self._running_nodes.items()):
                if node.status != NodeStatus.RUNNING:
                    continue

                interval, threshold = self._get_heartbeat_settings(
                    node.agent_type,
                )
                health = node.check_health(interval, threshold)
                alert_min = self._get_alert_threshold(node.agent_type)

                if health == NodeHealth.MISSED:
                    if node.missed_heartbeats >= alert_min:
                        await self._emit(ExecutionEvent(
                            node_id=node_id,
                            event_type="heartbeat_missed",
                            details={
                                "missed_count": node.missed_heartbeats,
                                "threshold": threshold,
                                "agent_type": node.agent_type,
                                "last_heartbeat": (
                                    node.last_heartbeat_at.isoformat()
                                    if node.last_heartbeat_at else None
                                ),
                            },
                        ))
                    else:
                        logger.debug(
                            "Node %s heartbeat missed (count=%d, threshold=%d)"
                            " — below alert_min=%d, not emitting event",
                            node_id, node.missed_heartbeats,
                            threshold, alert_min,
                        )

                elif health == NodeHealth.UNHEALTHY:
                    await self._emit(ExecutionEvent(
                        node_id=node_id,
                        event_type="unhealthy_warning",
                        details={
                            "missed_count": node.missed_heartbeats,
                            "threshold": threshold,
                            "agent_type": node.agent_type,
                            "message": (
                                f"Node {node_id} unhealthy: "
                                f"{node.missed_heartbeats} heartbeats missed "
                                f"(threshold: {threshold}, agent_type: "
                                f"{node.agent_type}). "
                                f"Node timeout is managed by "
                                f"_execute_with_timeout."
                            ),
                        },
                    ))

    def _start_watchdog(self) -> None:
        """Start the watchdog background task."""
        if self.enable_watchdog and self._watchdog_task is None:
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    def _stop_watchdog(self) -> None:
        """Stop the watchdog background task."""
        if self._watchdog_task:
            self._watchdog_task.cancel()
            self._watchdog_task = None

    async def execute(self, dag: DAG) -> DAG:
        """
        Execute the full DAG with support for replanning.

        Returns the executed DAG with all nodes' status and results populated.
        """
        try:
            levels = dag.topological_levels()
        except ValueError as e:
            raise ValueError(f"Invalid DAG: {e}")

        # M2.0: Start watchdog
        self._running_nodes = {}
        self._start_watchdog()

        # R3: Auto-serialize parallel generators without ownership contracts (#272 EC4)
        levels = self._auto_serialize_parallel_generators(dag, levels)

        replan_count = 0
        level_idx = 0

        try:
            while level_idx < len(levels):
                level = levels[level_idx]
                semaphore = asyncio.Semaphore(self.max_parallel)

                logger.info(
                    "Executing level %d/%d: %s",
                    level_idx + 1, len(levels), level,
                )

                async def run_with_limit(
                    node_id: str, sem: asyncio.Semaphore, dag_ref: DAG,
                ) -> None:
                    async with sem:
                        await self._execute_single_node(dag_ref, node_id)

                tasks = [run_with_limit(nid, semaphore, dag) for nid in level]
                results = await asyncio.gather(*tasks, return_exceptions=True)

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
                    nid for nid in level
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
                                        await self._execute_single_node(dag, failed_id)
                                    else:
                                        await self._emit(ExecutionEvent(
                                            node_id=target_id,
                                            event_type="upstream_retry",
                                            details={
                                                "reason": "evaluator_failed",
                                                "evaluator": failed_id,
                                                "feedback": node.eval_feedback[:1000],
                                            },
                                        ))
                                        # Retry generator with feedback.
                                        # Cap retry_count so _execute_single_node's
                                        # internal retry loop gives exactly one attempt.
                                        gen_node.retry_count = gen_node.max_retries - 1
                                        gen_node.status = NodeStatus.RETRYING
                                        gen_node.error = ""
                                        await self._execute_single_node(dag, target_id)

                                        if self._is_terminal_success(dag.nodes[target_id].status):
                                            # Re-run evaluator after generator fix
                                            node.status = NodeStatus.RETRYING
                                            node.error = ""
                                            await self._execute_single_node(dag, failed_id)
                                else:
                                    # No upstream generator found; retry evaluator directly
                                    node.status = NodeStatus.RETRYING
                                    node.error = ""
                                    await self._execute_single_node(dag, failed_id)
                            else:
                                # Normal retry: retry the failed node itself
                                node.status = NodeStatus.RETRYING
                                node.error = ""
                                await self._execute_single_node(dag, failed_id)

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
            self._running_nodes = {}
            self._running_tasks = {}
            # Shutdown dedicated thread pool to avoid RuntimeWarning on exit
            self._executor.shutdown(wait=False)

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
        snapshot: dict[str, str] = {}
        for artifact in (artifacts or []):
            path = os.path.join(work_dir, artifact)
            try:
                if os.path.isfile(path):
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        snapshot[artifact] = f.read()
            except OSError:
                pass
        return snapshot

    @staticmethod
    def _restore_file_snapshot(
        work_dir: str, snapshot: dict[str, str],
    ) -> None:
        """Restore files from a previously captured snapshot."""
        for artifact, content in snapshot.items():
            path = os.path.join(work_dir, artifact)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

    def _compute_backoff(self, retry_count: int) -> float:
        """Compute exponential backoff delay in seconds."""
        return min(self.backoff_base ** retry_count, self.backoff_cap)

    @staticmethod
    def _requires_output_artifacts(node: DAGNode) -> bool:
        """Check whether a node is expected to produce output file artifacts.

        Returns True only when criteria explicitly depend on disk files
        (FILE_EXISTS, FILE_CHANGED, FILE_PATTERN, TEST_FILE_EXISTS, TESTS_PASS).
        Pure analysis/text generators with CUSTOM-only criteria are excluded —
        they may legitimately produce zero file artifacts.
        """
        # Only generator-type nodes (or any non-standard type with file criteria)
        # are expected to produce file artifacts. Planner/evaluator are excluded.
        is_producer = node.agent_type in ("generator", "worker") or node.agent_type not in ("planner", "evaluator")

        file_criteria = {
            CriterionType.FILE_EXISTS,
            CriterionType.FILE_CHANGED,
            CriterionType.FILE_PATTERN,
            CriterionType.TEST_FILE_EXISTS,
            CriterionType.TESTS_PASS,
        }
        # Keywords in legacy string criteria that imply file/test output
        file_keywords = {"file", "coverage", "lint"}
        test_keywords = {"tests pass", "test pass", "test file"}
        for crit in node.success_criteria:
            if isinstance(crit, SuccessCriterion) and crit.type in file_criteria:
                return True
            if isinstance(crit, str) and is_producer:
                lower = crit.lower()
                if any(kw in lower for kw in file_keywords):
                    return True
                if any(kw in lower for kw in test_keywords):
                    return True
        return False

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
    # E999 is intentionally excluded — it indicates SyntaxError, not formatting.
    _RETRY_TOLERABLE_CODES: frozenset[str] = frozenset({
        # Whitespace / formatting
        "E501",  # line too long
        "E303",  # too many blank lines
        "W291",  # trailing whitespace
        "W293",  # whitespace before ':'
        "E203",  # whitespace before ':'
        "E302",  # expected 2 blank lines
        "E261",  # at least two spaces before inline comment
        "E265",  # block comment should start with '# '
        # Unused imports/variables — cosmetic, not functional
        "F401",  # module imported but unused
        "F841",  # local variable assigned but never used
    })

    @classmethod
    def _is_retry_tolerable_lint_issue(cls, issue: str) -> bool:
        """Check if a lint issue is formatting-only and safe to tolerate.

        Expects issues in 'path:line:CODE' format from evaluator metadata.
        E999 (SyntaxError) is NOT tolerable — it blocks execution.
        """
        import re as _re
        m = _re.search(r":([A-Z]\d{2,3})(?:\s|$)", issue)
        if m:
            return m.group(1) in cls._RETRY_TOLERABLE_CODES
        return False

    async def _execute_single_node(self, dag: DAG, node_id: str) -> None:
        """Execute a single DAG node with retry logic."""
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
            node.status = NodeStatus.SKIPPED
            node.error = (
                f"Skipped: hard dependencies {failed_hard} "
                f"failed/were skipped"
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

        node.status = NodeStatus.RUNNING
        node.started_at = datetime.now(timezone.utc)
        node.health_status = NodeHealth.HEALTHY
        node.record_heartbeat()  # M2.0: Initial heartbeat

        logger.info(
            "Node %s (%s) starting — attempt %d/%d",
            node_id, node.agent_type, node.retry_count + 1, node.max_retries,
        )

        # M2.0: Register with watchdog
        self._running_nodes[node_id] = node
        current_task = asyncio.current_task()
        if current_task:
            self._running_tasks[node_id] = current_task

        await self._emit(ExecutionEvent(
            node_id=node_id,
            event_type="started",
            details={"agent_type": node.agent_type, "task": node.task_description[:100]},
        ))

        try:
            result = await self._execute_with_timeout(node, input_artifacts)

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
            node.output_artifacts = reported_artifacts
            logger.debug("Node %s (%s) produced artifacts: %s", node_id, node.agent_type, node.output_artifacts)

            # -- Zero-output fast-fail (#229) --
            # If a node that should produce files has zero artifacts, fail
            # immediately with a clear message — regardless of evaluator presence.
            if (
                not node.output_artifacts
                and self._requires_output_artifacts(node)
            ):
                node.error = (
                    f"Node produced zero output artifacts. "
                    f"Agent type: {node.agent_type}, task: {node.task_description[:200]}. "
                    f"This typically indicates the agent exhausted its iteration "
                    f"budget without writing any files."
                )
                node.status = NodeStatus.FAILED
                node.completed_at = datetime.now(timezone.utc)
                node.retry_count += 1
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

            # Test file enforcement (#247): only enforce when the DAG
            # explicitly includes a TEST_FILE_EXISTS criterion. This is
            # distinct from TESTS_PASS which means "tests must pass", not
            # "must create test files".
            if node.agent_type == "generator" and node.output_artifacts:
                has_test_file_criteria = any(
                    self._is_test_file_exists_criterion(c) for c in node.success_criteria
                )
                if has_test_file_criteria:
                    # Broader test file detection: test_*.py, *_test.py, *_spec.py,
                    # files under tests/ or test/ directories
                    def _is_test_file(artifact_path: str) -> bool:
                        basename = artifact_path.lower().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
                        if basename.startswith("test_") or basename.endswith("_test.py"):
                            return True
                        if basename.endswith("_spec.py"):
                            return True
                        # Files inside tests/ or test/ directories
                        lower_path = artifact_path.lower()
                        if "/tests/" in lower_path or "/test/" in lower_path:
                            if basename.endswith(".py"):
                                return True
                        return False

                    has_test_files = any(
                        _is_test_file(a) for a in node.output_artifacts
                    )
                    if not has_test_files:
                        logger.warning(
                            "Node %s: TEST_FILE_EXISTS required but no test files "
                            "in output_artifacts — failing fast (#247)",
                            node_id,
                        )
                        node.eval_feedback = (
                            f"EVALUATION FAILED: You were required to create test "
                            f"files, but none were found in your output.\n"
                            f"Output artifacts: {node.output_artifacts}\n\n"
                            f"You MUST create test files (e.g., test_*.py) for "
                            f"your implementation. Create them using the write "
                            f"tool BEFORE finishing.\n"
                            f"Focus on: functional tests, edge cases, import "
                            f"validation. Each source module should have a "
                            f"corresponding test file."
                        )
                        node.error = "No test files created (TEST_FILE_EXISTS required)"
                        node.status = NodeStatus.FAILED
                        node.completed_at = datetime.now(timezone.utc)
                        await self._emit(ExecutionEvent(
                            node_id=node_id,
                            event_type="failed",
                            details={
                                "reason": "no_test_files",
                                "artifacts": node.output_artifacts,
                            },
                        ))
                        return

            if self.evaluator and node.success_criteria and node.agent_type == "generator":
                if not self.work_dir:
                    logger.error(
                        "Node %s: work_dir not set — cannot evaluate safely. "
                        "Aborting evaluation to prevent incorrect results.",
                        node_id,
                    )
                    node.status = NodeStatus.FAILED
                    node.error = (
                        "Evaluation skipped: work_dir not configured. "
                        "Pass --project to set the working directory."
                    )
                    node.completed_at = datetime.now(timezone.utc)
                    await self._emit(ExecutionEvent(
                        node_id=node_id,
                        event_type="failed",
                        details={"reason": "no_work_dir"},
                    ))
                    return
                eval_work_dir = self.work_dir
                eval_result = await asyncio.get_running_loop().run_in_executor(
                    self._executor,
                    functools.partial(
                        self.evaluator.evaluate_stage,
                        node_id, node_id, node.success_criteria, self.artifact_path,
                        work_dir=eval_work_dir,
                        output_artifacts=node.output_artifacts or None,
                    ),
                )

                # Store auto-eval result for downstream evaluator agents (#145)
                node.auto_eval_result = eval_result.model_dump()

                if not eval_result.passed:
                    # Track best attempt to detect retry regression (#129, #151).
                    prev_best = self._best_attempts.get(node_id)
                    current_issues = set(
                        eval_result.metadata.get(
                            "lint_new_issues",
                            eval_result.metadata.get("lint_all_issues", []),
                        )
                    )
                    is_regression = False
                    if prev_best is None:
                        self._best_attempts[node_id] = {
                            "score": eval_result.score,
                            "artifacts": node.output_artifacts.copy(),
                            "feedback": eval_result.feedback,
                            "lint_issues": current_issues,
                            "artifact_set": set(node.output_artifacts or []),
                            "file_snapshot": self._capture_file_snapshot(
                                eval_work_dir, node.output_artifacts,
                            ),
                            "criteria_results": getattr(
                                eval_result, "criteria_results", {},
                            ),
                            "passed": eval_result.passed,
                        }
                    elif eval_result.score > prev_best["score"]:
                        self._best_attempts[node_id] = {
                            "score": eval_result.score,
                            "artifacts": node.output_artifacts.copy(),
                            "feedback": eval_result.feedback,
                            "lint_issues": current_issues,
                            "artifact_set": set(node.output_artifacts or []),
                            "file_snapshot": self._capture_file_snapshot(
                                eval_work_dir, node.output_artifacts,
                            ),
                            "criteria_results": getattr(
                                eval_result, "criteria_results", {},
                            ),
                            "passed": eval_result.passed,
                        }
                    else:
                        # Score not improved — check issue-level regression (#151)
                        prev_issues: set[str] = prev_best.get(
                            "lint_issues", set(),
                        )
                        new_in_current = current_issues - prev_issues
                        fixed_from_prev = prev_issues - current_issues

                        # Check if all current issues are lint-only (#154).
                        # Lint-only failures should not block retry progress.
                        all_issues_lint_only = (
                            len(current_issues) > 0
                            and all(
                                self._is_retry_tolerable_lint_issue(iss)
                                for iss in current_issues
                            )
                        )

                        if new_in_current and not fixed_from_prev:
                            # Only new issues, nothing fixed
                            if all_issues_lint_only:
                                # Lint-only — allow retry, update best (#154)
                                is_regression = False
                                self._best_attempts[node_id] = {
                                    "score": eval_result.score,
                                    "artifacts": node.output_artifacts.copy(),
                                    "feedback": eval_result.feedback,
                                    "lint_issues": current_issues,
                                    "artifact_set": set(node.output_artifacts or []),
                                    "file_snapshot": self._capture_file_snapshot(
                                        eval_work_dir, node.output_artifacts,
                                    ),
                                    "criteria_results": getattr(
                                        eval_result, "criteria_results", {},
                                    ),
                                    "passed": eval_result.passed,
                                }
                            else:
                                is_regression = True
                        elif (
                            len(new_in_current) > len(fixed_from_prev)
                            and eval_result.score < prev_best["score"]
                        ):
                            # More new issues than fixed AND score dropped
                            is_regression = True
                        else:
                            # Partial progress: some issues fixed, some new
                            # introduced. Update best if more were fixed.
                            self._best_attempts[node_id] = {
                                "score": eval_result.score,
                                "artifacts": node.output_artifacts.copy(),
                                "feedback": eval_result.feedback,
                                "lint_issues": current_issues,
                                "artifact_set": set(node.output_artifacts or []),
                                "file_snapshot": self._capture_file_snapshot(
                                    eval_work_dir, node.output_artifacts,
                                ),
                                "criteria_results": getattr(
                                    eval_result, "criteria_results", {},
                                ),
                                "passed": eval_result.passed,
                            }
                        logger.warning(
                            "Node %s retry score %.1f <= best %.1f "
                            "(new_issues=%d, fixed=%d, regression=%s)",
                            node_id, eval_result.score, prev_best["score"],
                            len(new_in_current), len(fixed_from_prev),
                            is_regression,
                        )
                        # Regression detected: restore best attempt files (#212).
                        if is_regression and "file_snapshot" in prev_best:
                            logger.info(
                                "Node %s: restoring best attempt artifacts "
                                "(score %.1f > current %.1f)",
                                node_id, prev_best["score"], eval_result.score,
                            )
                            self._restore_file_snapshot(
                                eval_work_dir, prev_best["file_snapshot"],
                            )
                            # Delete extra files added by the regression attempt
                            # that were not present in the best artifact set.
                            best_artifact_set = prev_best.get(
                                "artifact_set", set(prev_best["file_snapshot"].keys()),
                            )
                            for artifact in (node.output_artifacts or []):
                                if artifact not in best_artifact_set:
                                    path = os.path.join(eval_work_dir, artifact)
                                    try:
                                        if os.path.isfile(path):
                                            os.remove(path)
                                            logger.info(
                                                "Node %s: removed extra file %s "
                                                "not in best attempt",
                                                node_id, artifact,
                                            )
                                    except OSError:
                                        pass
                            node.output_artifacts = prev_best["artifacts"]
                    node.retry_count += 1
                    # Build retry feedback with regression awareness.
                    best = self._best_attempts[node_id]
                    regression_hint = ""
                    if is_regression or eval_result.score < best["score"]:
                        regression_hint = (
                            "\n\nWARNING: Your previous attempt scored higher "
                            f"({best['score']:.1f} vs current {eval_result.score:.1f}). "
                            "The code may already be correct — only fix the "
                            "specific issues reported, do NOT rewrite working code."
                        )
                    # Add targeted lint fix guidance (#151)
                    prev_issues = best.get("lint_issues", set())
                    curr_issues = current_issues
                    new_only = curr_issues - prev_issues
                    if new_only and not regression_hint:
                        lint_guidance = (
                            "\n\nLINT_FIX_GUIDANCE: Fix ONLY these new lint "
                            "issues. Do NOT rewrite working code:\n"
                            + "\n".join(
                                f"  - {iss}" for iss in sorted(new_only)[:10]
                            )
                        )
                    else:
                        lint_guidance = ""
                    node.eval_feedback = (
                        f"{eval_result.feedback}\n\n"
                        f"Output artifacts: {node.output_artifacts or 'none'}\n\n"
                        f"IMPORTANT: Fix the issues INCREMENTALLY. Do NOT rewrite working "
                        f"code from scratch. Use the edit tool to fix specific problems.\n"
                        f"Fix ALL issues listed above."
                        f"{regression_hint}"
                        f"{lint_guidance}"
                    )
                    node.error = f"Evaluation failed (score: {eval_result.score}): {eval_result.feedback}"
                    node.status = NodeStatus.FAILED
                    node.completed_at = datetime.now(timezone.utc)

                    # On regression, align auto_eval_result with the best
                    # attempt so downstream evaluators get accurate info
                    # (#145 review feedback).
                    if is_regression and best:
                        node.auto_eval_result = {
                            "passed": False,
                            "score": best["score"],
                            "feedback": best["feedback"],
                            "_note": (
                                "Updated to best-attempt result "
                                "(regression detected)"
                            ),
                        }
                    # If node exhausted retries and is ultimately not
                    # successful, clear auto_eval_result so no stale
                    # result leaks to downstream evaluators (#145).
                    if node.retry_count >= node.max_retries:
                        node.auto_eval_result = None

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
                node.status = self._eval_status_to_node_status(eval_result.eval_status)
            else:
                node.status = NodeStatus.SUCCESS
            node.completed_at = datetime.now(timezone.utc)
            node.result = result
            node.output_artifacts = result.get("artifacts", [])

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
            # CancelledError is BaseException in Python 3.11+, not caught by
            # Exception. We catch it here to handle watchdog cancellation.
            if node.health_status == NodeHealth.DEAD:
                return  # Swallow cancellation for watchdog-killed nodes
            raise  # Re-raise for genuine cancellation requests

        except PendingApprovalError:
            # Agent hit a high-risk tool requiring human approval.
            # Do NOT retry, do NOT mark as failed — just pause and re-raise
            # so the Worker can enter its PENDING_APPROVAL poll loop.
            node.status = NodeStatus.PENDING_APPROVAL
            node.completed_at = datetime.now(timezone.utc)
            raise

        except Exception as e:
            # M2.0: Check if node was already killed by watchdog (DEAD state)
            if node.health_status == NodeHealth.DEAD:
                # Node was killed by watchdog; do not retry
                return

            node.error = f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}"

            # RateLimitError: do NOT consume node retry budget (#360).
            # The error will propagate to service.py which also skips the
            # job-level retry budget for rate_limit errors.
            if isinstance(e, RateLimitError):
                node.status = NodeStatus.FAILED
                node.completed_at = datetime.now(timezone.utc)
                node.auto_eval_result = None
                await self._emit(ExecutionEvent(
                    node_id=node_id,
                    event_type="failed",
                    details={
                        "error": str(e),
                        "reason": "rate_limit",
                        "retry_budget_preserved": True,
                    },
                ))
                return

            node.retry_count += 1

            if node.retry_count < node.max_retries:
                node.status = NodeStatus.RETRYING
                await self._emit(ExecutionEvent(
                    node_id=node_id,
                    event_type="retrying",
                    details={"attempt": node.retry_count, "error": str(e)},
                ))
                backoff = self._compute_backoff(node.retry_count)
                await asyncio.sleep(backoff)
                await self._execute_single_node(dag, node_id)
            else:
                node.status = NodeStatus.FAILED
                node.completed_at = datetime.now(timezone.utc)
                # Clear stale auto_eval_result on terminal failure (#145).
                node.auto_eval_result = None

                await self._emit(ExecutionEvent(
                    node_id=node_id,
                    event_type="failed",
                    details={"error": str(e), "attempts": node.retry_count},
                ))
        finally:
            # M2.0: Unregister from watchdog on completion (unless killed by watchdog)
            if node.health_status != NodeHealth.DEAD:
                self._running_nodes.pop(node_id, None)
                self._running_tasks.pop(node_id, None)

    async def _execute_with_timeout(
        self,
        node: DAGNode,
        input_artifacts: list[HandoffArtifact],
    ) -> dict[str, Any]:
        """Execute a node with unified wall-clock timeout (#360 PR3).

        - Timeout is per-agent-type via node_timeout config
        - Cooperative cancellation: threading.Event passed to agent thread
        - Progress reporting: agent reports heartbeat after each LLM call and
          tool execution via progress_callback, replacing the old auto-heartbeat
          loop. Watchdog detects "no progress" rather than "event loop frozen".
        """
        timeout = self._get_node_timeout(node.agent_type)
        cancel_event = threading.Event()

        # Progress callback: agent thread calls this after each meaningful
        # action (LLM response received, tool executed). This replaces the
        # old _heartbeat_loop auto-heartbeat (#360 PR3).
        loop = asyncio.get_running_loop()

        def _on_progress() -> None:
            # Thread-safe: schedule heartbeat on event loop thread
            # instead of mutating node directly from worker thread.
            try:
                loop.call_soon_threadsafe(node.record_heartbeat)
            except RuntimeError:
                pass  # Event loop closed

        async def _run_with_cancel(n: DAGNode, arts: list[HandoffArtifact]) -> dict:
            return await self.agent_executor(
                n, arts,
                cancel_event=cancel_event,
                progress_callback=_on_progress,
            )

        task = asyncio.create_task(_run_with_cancel(node, input_artifacts))

        try:
            return await asyncio.wait_for(task, timeout=timeout)
        except asyncio.TimeoutError:
            # Signal the thread to stop at next iteration boundary
            cancel_event.set()
            logger.warning(
                "Node %s (%s) timed out after %ds — cancel event set",
                node.id, node.agent_type, timeout,
            )
            raise NodeTimeoutError(
                node_id=node.id,
                agent_type=node.agent_type,
                timeout=timeout,
            )
        except asyncio.CancelledError:
            cancel_event.set()
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            raise

    def _get_node_timeout(self, agent_type: str) -> int:
        """Return node timeout for the given agent type.

        Uses NodeTimeoutConfig from config if available, otherwise falls
        back to watchdog-based calculation for backward compatibility.
        """
        if self._node_timeout_config is not None:
            return self._node_timeout_config.timeout_for(agent_type)
        # Fallback: derive from watchdog settings (backward compat)
        # Floor of 1s prevents timeout=0 when interval*threshold rounds down.
        interval, threshold = self._get_heartbeat_settings(agent_type)
        return max(1, int(interval * threshold))

    def _collect_input_artifacts(
        self,
        dag: DAG,
        node_id: str,
        failed_soft: list[str] | None = None,
    ) -> list[HandoffArtifact]:
        """Collect output artifacts from all dependency nodes."""
        dependencies = dag.get_dependencies(node_id)
        artifacts: list[HandoffArtifact] = []

        for dep_id in dependencies:
            dep_node = dag.nodes[dep_id]
            if self._is_terminal_success(dep_node.status):
                artifact = HandoffArtifact(
                    from_agent=dep_node.agent_type,
                    to_agent=dag.nodes[node_id].agent_type,
                    content=dep_node.result.get("summary", ""),
                    file_paths=dep_node.output_artifacts,
                    metadata={
                        "from_node": dep_id,
                        "task": dep_node.task_description,
                    },
                )
                artifacts.append(artifact)

                # Pass auto-eval results to downstream evaluator agents (#145).
                # Only pass when dep_node is SUCCESS (already guaranteed by the
                # enclosing if) AND auto_eval_result corresponds to the current
                # output_artifacts (i.e., evaluation passed).
                if (
                    dep_node.auto_eval_result
                    and dep_node.auto_eval_result.get("passed") is True
                    and dag.nodes[node_id].agent_type == "evaluator"
                ):
                    eval_info = dep_node.auto_eval_result
                    criteria = eval_info.get("criteria_results", {})
                    has_warnings = (
                        criteria
                        and not all(criteria.values())
                    )
                    header = (
                        "AUTOMATED EVALUATION RESULTS "
                        "(passed via threshold — some criteria have WARNINGS)"
                        if has_warnings
                        else "AUTOMATED EVALUATION RESULTS (already verified)"
                    )
                    summary = (
                        f"{header}:\n"
                        f"- Passed: {eval_info.get('passed')}\n"
                        f"- Score: {eval_info.get('score')}\n"
                        f"- Criteria: {criteria}\n"
                        f"- Feedback:\n{eval_info.get('feedback', '')}\n"
                    )
                    artifacts.append(HandoffArtifact(
                        from_agent="auto_evaluator",
                        to_agent="evaluator",
                        content=summary,
                        metadata={
                            "type": "evaluation_result",
                            "passed": eval_info.get("passed"),
                            "score": eval_info.get("score"),
                            "criteria_results": criteria,
                            "feedback": eval_info.get("feedback"),
                            "has_warnings": has_warnings,
                        },
                    ))

                # M3.2: Share relevant memories from upstream agent
                if (
                    self.memory_manager
                    and self._session_id
                    and dep_node.agent_type != dag.nodes[node_id].agent_type
                ):
                    try:
                        from memory.sharing import MemorySharing
                        sharing = MemorySharing(self.memory_manager)
                        sharing.share_with_downstream(
                            from_agent=dep_node.agent_type,
                            to_agent=dag.nodes[node_id].agent_type,
                            session_id=self._session_id,
                            dag=dag,
                            node_id=node_id,
                        )
                    except Exception as e:
                        logger.debug("Memory sharing failed: %s", e)

        # Include evaluation feedback from previous attempt (retry scenario)
        node = dag.nodes[node_id]
        if node.eval_feedback:
            feedback = node.eval_feedback
            # Detect naming mismatch patterns and add targeted guidance
            # (#311) — helps the generator understand what specifically
            # went wrong with imports or type signatures.
            has_import_error = (
                "ImportError" in feedback
                or "cannot import" in feedback
                or "ModuleNotFoundError" in feedback
            )
            has_type_error = (
                "TypeError" in feedback
                or "unexpected keyword" in feedback
            )
            has_timeout = (
                "timed out" in feedback
                or "TimeoutExpired" in feedback
                or "timeout" in feedback.lower()
            )
            has_coverage_low = (
                "coverage" in feedback.lower()
                and ("below target" in feedback.lower()
                     or "not verified" in feedback.lower()
                     or "could not be parsed" in feedback.lower())
            )
            has_runtime_error = (
                "RuntimeError" in feedback
                or "AttributeError" in feedback
                or "KeyError" in feedback
            )
            naming_guidance = ""
            if has_import_error:
                naming_guidance += (
                    "\nNAMING MISMATCH DETECTED: Your tests import "
                    "symbols that don't exist in the source modules. "
                    "To fix:\n"
                    "1. READ the source files first to discover the "
                    "actual class/function names\n"
                    "2. Run: `python -c 'from module import Symbol'` "
                    "to verify each import\n"
                    "3. Fix your TEST code to match the actual source "
                    "API — do NOT modify the source\n"
                )
            if has_type_error:
                naming_guidance += (
                    "\nTYPE ERROR DETECTED: Your code calls functions "
                    "with wrong arguments or mismatched async/sync "
                    "patterns.\n"
                    "1. Check if async functions are called without "
                    "`await` or `asyncio.run()`\n"
                    "2. Verify function signatures match actual "
                    "parameter names\n"
                )
            if has_timeout:
                naming_guidance += (
                    "\nTIMEOUT DETECTED: Your tests or code hung during "
                    "execution.\n"
                    "1. Check for infinite loops or missing loop "
                    "termination conditions\n"
                    "2. Use daemon threads and proper teardown in tests\n"
                    "3. Add timeouts to any blocking operations "
                    "(network, subprocess)\n"
                    "4. Avoid global state or locks that can deadlock\n"
                )
            if has_coverage_low:
                naming_guidance += (
                    "\nLOW COVERAGE DETECTED: Coverage is below target.\n"
                    "1. Do NOT rewrite existing tests or source code\n"
                    "2. ADD new test functions that cover untested "
                    "branches and edge cases\n"
                    "3. Focus on: error paths, boundary conditions, "
                    "empty inputs\n"
                    "4. Run coverage to see which lines are missed: "
                    "`pytest --cov=module --cov-report=term-missing`\n"
                )
            if has_runtime_error:
                naming_guidance += (
                    "\nRUNTIME ERROR DETECTED: Source code has bugs "
                    "that cause crashes.\n"
                    "1. Read the traceback to find the exact crash "
                    "location\n"
                    "2. You may EDIT source files to fix the bug "
                    "(targeted fix, not rewrite)\n"
                    "3. Common fixes: add missing method calls, "
                    "fix None checks, add initialization\n"
                )

            retry_hint = (
                f"RETRY ATTEMPT #{node.retry_count}: Your previous "
                f"attempt FAILED evaluation.\n\n"
                f"Evaluation feedback:\n{feedback}\n\n"
                f"{naming_guidance}"
                f"IMPORTANT: Do NOT repeat the same approach. "
                f"Analyze what went wrong and try a DIFFERENT "
                f"strategy."
            )
            artifacts.append(HandoffArtifact(
                from_agent="evaluator",
                to_agent=node.agent_type,
                content=retry_hint,
                metadata={
                    "type": "eval_feedback",
                    "attempt": node.retry_count,
                },
            ))

        # Soft dependency warning: downstream gets structured info about
        # failed/skipped soft deps so it can adapt its behavior (#271).
        if failed_soft:
            dep_summaries = []
            for dep_id in failed_soft:
                dep_node = dag.nodes[dep_id]
                dep_summaries.append(
                    f"- {dep_id} ({dep_node.agent_type}): "
                    f"{dep_node.status.value}"
                    f"{'; ' + dep_node.error[:200] if dep_node.error else ''}"
                )
            warning_content = (
                "DEPENDENCY WARNING: The following soft (optional) "
                "dependencies failed or were skipped:\n"
                + "\n".join(dep_summaries)
                + "\n\nYou may proceed, but outputs from these nodes "
                "are NOT available."
            )
            artifacts.append(HandoffArtifact(
                from_agent="dag_engine",
                to_agent=dag.nodes[node_id].agent_type,
                content=warning_content,
                metadata={
                    "type": "dependency_warning",
                    "failed_soft_deps": failed_soft,
                    "dep_statuses": {
                        dep_id: dag.nodes[dep_id].status.value
                        for dep_id in failed_soft
                    },
                },
            ))

        return artifacts

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
