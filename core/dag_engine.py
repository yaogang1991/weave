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
import traceback
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

from core.models import (
    DAG,
    DAGNode,
    NodeStatus,
    ExecutionEvent,
    FailureDecision,
    HandoffArtifact,
)


EventHandler = Callable[[ExecutionEvent], Coroutine[Any, Any, None]]
ReplanHandler = Callable[[DAG, str], Coroutine[Any, Any, DAG]]


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
        job_timeout: float | None = None,
    ):
        self.agent_executor = agent_executor
        self.failure_handler = failure_handler
        self.replan_handler = replan_handler
        self.max_replans = max_replans
        self.max_parallel = max_parallel
        self.evaluator = evaluator
        self.artifact_path = artifact_path
        self.job_timeout = job_timeout
        self.event_handlers: list[EventHandler] = []

    def on_event(self, handler: EventHandler) -> None:
        """Register an event handler for execution monitoring."""
        self.event_handlers.append(handler)

    async def _emit(self, event: ExecutionEvent) -> None:
        """Emit execution event to all handlers."""
        for handler in self.event_handlers:
            try:
                await handler(event)
            except Exception:
                pass  # Don't let event handlers break execution

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

    async def execute(self, dag: DAG) -> DAG:
        """
        Execute the full DAG with support for replanning.

        Returns the executed DAG with all nodes' status and results populated.
        """
        try:
            levels = dag.topological_levels()
        except ValueError as e:
            raise ValueError(f"Invalid DAG: {e}")

        replan_count = 0
        level_idx = 0

        while level_idx < len(levels):
            level = levels[level_idx]
            semaphore = asyncio.Semaphore(self.max_parallel)

            async def run_with_limit(
                node_id: str, sem: asyncio.Semaphore, dag_ref: DAG,
            ) -> None:
                async with sem:
                    await self._execute_single_node(dag_ref, node_id)

            tasks = [run_with_limit(nid, semaphore, dag) for nid in level]
            await asyncio.gather(*tasks, return_exceptions=True)

            failed_in_level = [
                nid for nid in level
                if dag.nodes[nid].status == NodeStatus.FAILED
            ]

            if failed_in_level:
                for failed_id in failed_in_level:
                    decision = await self.failure_handler(
                        dag, failed_id, dag.nodes[failed_id].error,
                    )

                    if decision.action == "abort":
                        self._skip_remaining(dag, levels, level_idx + 1)
                        return dag

                    elif decision.action == "retry":
                        # Exponential backoff before retry
                        backoff = self._compute_backoff(dag.nodes[failed_id].retry_count)
                        if backoff > 0:
                            await asyncio.sleep(backoff)
                        await self._execute_single_node(dag, failed_id)
                        if dag.nodes[failed_id].status == NodeStatus.FAILED:
                            self._skip_remaining(dag, levels, level_idx + 1)
                            return dag

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

    def _compute_backoff(self, retry_count: int) -> float:
        """Compute exponential backoff delay in seconds."""
        import math
        # Base 2 seconds, exponential with jitter cap at 60s
        delay = min(2 ** retry_count, 60.0)
        return delay

    async def _execute_single_node(self, dag: DAG, node_id: str) -> None:
        """Execute a single DAG node with retry logic."""
        node = dag.nodes[node_id]

        # Skip if already executed (from merged DAG after replan)
        if node.status == NodeStatus.SUCCESS:
            return

        if node.status not in (NodeStatus.PENDING, NodeStatus.RETRYING):
            return

        input_artifacts = self._collect_input_artifacts(dag, node_id)

        node.status = NodeStatus.RUNNING
        node.started_at = datetime.now(timezone.utc)

        await self._emit(ExecutionEvent(
            node_id=node_id,
            event_type="started",
            details={"agent_type": node.agent_type, "task": node.task_description[:100]},
        ))

        try:
            result = await self.agent_executor(node, input_artifacts)

            # -- Evaluation gate --
            if self.evaluator and node.success_criteria:
                eval_result = await asyncio.to_thread(
                    self.evaluator.evaluate_stage,
                    node_id, node_id, node.success_criteria, self.artifact_path,
                )

                if not eval_result.passed:
                    node.retry_count += 1
                    node.eval_feedback = eval_result.feedback
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

        except Exception as e:
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

        # Include evaluation feedback from previous attempt (retry scenario)
        node = dag.nodes[node_id]
        if node.eval_feedback:
            artifacts.append(HandoffArtifact(
                from_agent="evaluator",
                to_agent=node.agent_type,
                content=f"Evaluation feedback from previous attempt:\n{node.eval_feedback}",
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
