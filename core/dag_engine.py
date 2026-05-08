"""
DAG Execution Engine: Topological scheduling + parallel execution + failure handling.

Key design decisions:
1. Topological levels: Nodes at the same level execute in parallel
2. Failure handling: Delegated to IntelligentOrchestrator (not hardcoded)
3. Context isolation: Each agent gets independent context
4. Handoff artifacts: Structured transfer of outputs between agents
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


class DAGExecutionEngine:
    """
    Executes a DAG by:
    1. Computing topological levels
    2. Running nodes at each level in parallel
    3. Waiting for all to complete before next level
    4. Handling failures via orchestrator callback
    """

    def __init__(
        self,
        agent_executor: Callable[[DAGNode, list[HandoffArtifact]], Coroutine[Any, Any, dict]],
        failure_handler: Callable[[DAG, str, str], Coroutine[Any, Any, FailureDecision]],
        max_parallel: int = 5,
    ):
        self.agent_executor = agent_executor
        self.failure_handler = failure_handler
        self.max_parallel = max_parallel
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

    async def execute(self, dag: DAG) -> DAG:
        """
        Execute the full DAG.

        Returns the executed DAG with all nodes' status and results populated.
        """
        try:
            levels = dag.topological_levels()
        except ValueError as e:
            raise ValueError(f"Invalid DAG: {e}")

        for level_idx, level in enumerate(levels):
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
                        dag, failed_id, dag.nodes[failed_id].error
                    )

                    if decision.action == "abort":
                        self._skip_remaining(dag, levels, level_idx + 1)
                        return dag

                    elif decision.action == "retry":
                        await self._execute_single_node(dag, failed_id)
                        # If retry did not resolve the failure, abort to keep state consistent
                        if dag.nodes[failed_id].status == NodeStatus.FAILED:
                            self._skip_remaining(dag, levels, level_idx + 1)
                            return dag

                    elif decision.action == "skip":
                        dag.nodes[failed_id].status = NodeStatus.SKIPPED

                    elif decision.action == "replan":
                        return dag

        return dag

    async def _execute_single_node(self, dag: DAG, node_id: str) -> None:
        """Execute a single DAG node with retry logic."""
        node = dag.nodes[node_id]

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
                await asyncio.sleep(1)
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
