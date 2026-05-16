"""
WatchdogService — heartbeat monitoring for running DAG nodes.

Extracted from DAGExecutionEngine as part of #177 PR4.
Behavior-preserving extraction: all logic is identical, just relocated
for testability and separation of concerns.

The watchdog is a pure early-warning event source. It monitors running
nodes' heartbeats and emits events for observability. It does NOT kill
nodes — node timeout is managed exclusively by _execute_with_timeout.
"""
from __future__ import annotations

import asyncio
import logging

from typing import Any, Callable, Coroutine

from core.models import DAGNode, ExecutionEvent, NodeHealth, NodeStatus

logger = logging.getLogger(__name__)


class WatchdogService:
    """Monitors running DAG nodes via heartbeat protocol.

    Responsibilities:
    - Background coroutine that checks node health at regular intervals
    - Per-agent-type heartbeat interval and threshold overrides
    - Configurable alert thresholds to reduce event noise
    - Event emission for heartbeat_missed and unhealthy_warning

    Usage:
        watchdog = WatchdogService(interval_sec=30, miss_threshold=12)
        watchdog.register(node_id, node)
        watchdog.start()  # starts background coroutine
        # ... nodes execute ...
        watchdog.stop()   # cancels background coroutine
    """

    def __init__(
        self,
        heartbeat_interval_sec: float = 30.0,
        heartbeat_miss_threshold: int = 12,
        enabled: bool = True,
        watchdog_overrides: dict[str, tuple[float, int]] | None = None,
        alert_thresholds: dict[str, int] | None = None,
        emit_func: Callable[[ExecutionEvent], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        self._interval_sec = heartbeat_interval_sec
        self._miss_threshold = heartbeat_miss_threshold
        self._enabled = enabled
        self._overrides = watchdog_overrides or {}
        self._alert_thresholds = alert_thresholds or {}
        self._emit_func = emit_func
        self._task: asyncio.Task | None = None
        self._running_nodes: dict[str, DAGNode] = {}

    def get_heartbeat_settings(self, agent_type: str) -> tuple[float, int]:
        """Return (interval_sec, miss_threshold) for the given agent type."""
        if agent_type in self._overrides:
            return self._overrides[agent_type]
        return self._interval_sec, self._miss_threshold

    def get_alert_threshold(self, agent_type: str) -> int:
        """Minimum missed_count to emit heartbeat_missed event."""
        if agent_type in self._alert_thresholds:
            return self._alert_thresholds[agent_type]
        return 2

    def register(self, node_id: str, node: DAGNode) -> None:
        """Register a running node for heartbeat monitoring."""
        self._running_nodes[node_id] = node

    def unregister(self, node_id: str) -> None:
        """Remove a node from heartbeat monitoring."""
        self._running_nodes.pop(node_id, None)

    def clear(self) -> None:
        """Remove all registered nodes."""
        self._running_nodes.clear()

    @property
    def running_nodes(self) -> dict[str, DAGNode]:
        """Read-only access to currently monitored nodes."""
        return dict(self._running_nodes)

    def start(self) -> None:
        """Start the watchdog background task."""
        if self._enabled and self._task is None:
            self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        """Stop the watchdog background task."""
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
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
        check_interval = self._interval_sec * 1.5
        while True:
            try:
                await asyncio.sleep(check_interval)
            except asyncio.CancelledError:
                return

            for node_id, node in list(self._running_nodes.items()):
                if node.status != NodeStatus.RUNNING:
                    continue

                interval, threshold = self.get_heartbeat_settings(
                    node.agent_type,
                )
                health = node.check_health(interval, threshold)
                alert_min = self.get_alert_threshold(node.agent_type)

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
                        event_type="health_alert",
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

    async def _emit(self, event: ExecutionEvent) -> None:
        """Emit event via the configured emit function."""
        if self._emit_func is not None:
            try:
                await self._emit_func(event)
            except Exception as exc:
                logger.warning(
                    "WatchdogService emit failed: %s: %s",
                    type(exc).__name__, exc,
                )
