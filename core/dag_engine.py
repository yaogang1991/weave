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
import traceback
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

from core.models import (
    DAG,
    DAGNode,
    NodeStatus,
    NodeHealth,
    ExecutionEvent,
    FailureDecision,
    HandoffArtifact,
    CriterionType,
)
from core.exceptions import PendingApprovalError


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
        # Dedicated thread pool for evaluator calls — avoids global pool
        # join timeout warnings on event loop exit.
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_parallel,
            thread_name_prefix="dag-engine",
        )
        # Best-attempt tracking to prevent retry regression (#129).
        self._best_attempts: dict[str, dict] = {}

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
            if node.status == NodeStatus.SUCCESS and node_id in merged.nodes:
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
        Watchdog coroutine: monitors running nodes' heartbeats.

        Per-agent-type overrides are respected: generator nodes get more
        generous intervals and thresholds.

        Nodes exceeding miss_threshold are marked UNHEALTHY and killed.

        Per-agent-type overrides are respected: generator nodes, for
        example, are allowed longer timeouts than planner/evaluator.

        Alert events use a configurable threshold (default 50% of kill
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
                        event_type="unhealthy_killed",
                        details={
                            "missed_count": node.missed_heartbeats,
                            "threshold": threshold,
                            "agent_type": node.agent_type,
                            "action": "fail_fast",
                        },
                    ))

                    node.health_status = NodeHealth.DEAD
                    node.status = NodeStatus.FAILED
                    node.error = (
                        f"Node killed by watchdog: "
                        f"{node.missed_heartbeats} heartbeats missed "
                        f"(threshold: {threshold}, agent_type: {node.agent_type})"
                    )
                    node.completed_at = datetime.now(timezone.utc)

                    task = self._running_tasks.pop(node_id, None)
                    if task and not task.done():
                        task.cancel()

                    self._running_nodes.pop(node_id, None)

                    await self._emit(ExecutionEvent(
                        node_id="",
                        event_type="health_alert",
                        details={
                            "alert_type": "node_unhealthy_killed",
                            "node_id": node_id,
                            "agent_type": node.agent_type,
                            "message": (
                                f"Node {node_id} killed after "
                                f"{node.missed_heartbeats} missed heartbeats"
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
        replan_count = 0
        level_idx = 0

        try:
            while level_idx < len(levels):
                level = levels[level_idx]
                semaphore = asyncio.Semaphore(self.max_parallel)

                async def run_with_limit(
                    node_id: str, sem: asyncio.Semaphore, dag_ref: DAG,
                ) -> None:
                    async with sem:
                        await self._execute_single_node(dag_ref, node_id)

                tasks = [run_with_limit(nid, semaphore, dag) for nid in level]
                results = await asyncio.gather(*tasks, return_exceptions=True)

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

                                        if dag.nodes[target_id].status == NodeStatus.SUCCESS:
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
                            if dag.nodes[failed_id].status != NodeStatus.SUCCESS:
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

    def _compute_backoff(self, retry_count: int) -> float:
        """Compute exponential backoff delay in seconds."""
        return min(self.backoff_base ** retry_count, self.backoff_cap)

    async def _execute_single_node(self, dag: DAG, node_id: str) -> None:
        """Execute a single DAG node with retry logic."""
        node = dag.nodes[node_id]

        # Skip if already executed (from merged DAG after replan)
        if node.status == NodeStatus.SUCCESS:
            return

        if node.status not in (NodeStatus.PENDING, NodeStatus.RETRYING):
            return

        # Dependency-aware skip: only skip if a direct dependency failed (#259).
        # Unlike _skip_remaining (which skips entire levels), this allows nodes
        # whose dependencies all succeeded to continue executing.
        deps = dag.get_dependencies(node_id)
        failed_deps = [
            d for d in deps
            if dag.nodes[d].status in (NodeStatus.FAILED, NodeStatus.SKIPPED)
        ]
        if failed_deps:
            node.status = NodeStatus.SKIPPED
            node.error = (
                f"Skipped: upstream dependencies {failed_deps} "
                f"failed/were skipped"
            )
            logger.info(
                "Node %s skipped due to failed dependencies: %s",
                node_id, failed_deps,
            )
            return

        input_artifacts = self._collect_input_artifacts(dag, node_id)

        node.status = NodeStatus.RUNNING
        node.started_at = datetime.now(timezone.utc)
        node.health_status = NodeHealth.HEALTHY
        node.record_heartbeat()  # M2.0: Initial heartbeat

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
            result = await self._execute_with_heartbeat(node, input_artifacts)

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
                eval_work_dir = self.work_dir or os.getcwd()
                eval_result = await asyncio.get_running_loop().run_in_executor(
                    self._executor,
                    functools.partial(
                        self.evaluator.evaluate_stage,
                        node_id, node_id, node.success_criteria, self.artifact_path,
                        work_dir=eval_work_dir,
                        output_artifacts=node.output_artifacts or None,
                    ),
                )

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
                        }
                    elif eval_result.score > prev_best["score"]:
                        self._best_attempts[node_id] = {
                            "score": eval_result.score,
                            "artifacts": node.output_artifacts.copy(),
                            "feedback": eval_result.feedback,
                            "lint_issues": current_issues,
                        }
                    else:
                        # Score not improved — check issue-level regression (#151)
                        prev_issues: set[str] = prev_best.get(
                            "lint_issues", set(),
                        )
                        new_in_current = current_issues - prev_issues
                        fixed_from_prev = prev_issues - current_issues
                        if new_in_current and not fixed_from_prev:
                            # Only new issues, nothing fixed → true regression
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
                            }
                        logger.warning(
                            "Node %s retry score %.1f <= best %.1f "
                            "(new_issues=%d, fixed=%d, regression=%s)",
                            node_id, eval_result.score, prev_best["score"],
                            len(new_in_current), len(fixed_from_prev),
                            is_regression,
                        )
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

            node.status = NodeStatus.SUCCESS
            node.completed_at = datetime.now(timezone.utc)
            node.result = result
            node.output_artifacts = result.get("artifacts", [])

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

    async def _execute_with_heartbeat(
        self,
        node: DAGNode,
        input_artifacts: list[HandoffArtifact],
    ) -> dict[str, Any]:
        """Execute a node with a dedicated heartbeat coroutine.

        A background task records heartbeats at the per-agent-type interval
        independently of the executor task.  This avoids timing gaps that
        could occur with the previous ``asyncio.wait_for`` polling approach
        when the LLM API response time is close to the heartbeat interval.
        """
        task = asyncio.create_task(self.agent_executor(node, input_artifacts))
        heartbeat_interval, _ = self._get_heartbeat_settings(node.agent_type)

        async def _heartbeat_loop() -> None:
            while not task.done():
                await asyncio.sleep(heartbeat_interval)
                if not task.done():
                    node.record_heartbeat()

        hb = asyncio.create_task(_heartbeat_loop())
        try:
            return await task
        except asyncio.CancelledError:
            # Watchdog (or caller) cancelled the task.
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            raise
        finally:
            if not hb.done():
                hb.cancel()
            try:
                await hb
            except asyncio.CancelledError:
                pass

    def _collect_input_artifacts(self, dag: DAG, node_id: str) -> list[HandoffArtifact]:
        """Collect output artifacts from all dependency nodes."""
        dependencies = dag.get_dependencies(node_id)
        artifacts = []

        for dep_id in dependencies:
            dep_node = dag.nodes[dep_id]
            if dep_node.status == NodeStatus.SUCCESS:
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
            retry_hint = (
                f"RETRY ATTEMPT #{node.retry_count}: Your previous attempt FAILED evaluation.\n\n"
                f"Evaluation feedback:\n{node.eval_feedback}\n\n"
                f"IMPORTANT: Do NOT repeat the same approach. Analyze what went wrong "
                f"and try a DIFFERENT strategy."
            )
            artifacts.append(HandoffArtifact(
                from_agent="evaluator",
                to_agent=node.agent_type,
                content=retry_hint,
                metadata={"type": "eval_feedback", "attempt": node.retry_count},
            ))

        return artifacts

    def get_execution_summary(self, dag: DAG) -> dict[str, Any]:
        """Generate a summary of DAG execution results."""
        total = len(dag.nodes)
        success = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.SUCCESS)
        failed = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.FAILED)
        skipped = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.SKIPPED)

        return {
            "total_nodes": total,
            "success": success,
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
