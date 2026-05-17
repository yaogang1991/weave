"""
Backward-compat proxy properties for DAGExecutionEngine (#516).

These properties delegate to extracted services (NodeExecutor, WatchdogService,
RetryPolicyEngine) so external code that accesses engine.agent_executor etc.
continues to work.
"""
from __future__ import annotations

from core.models import DAGNode


class DAGCompatMixin:
    """Backward-compat proxies for extracted services (#177 PR4).

    Mixed into DAGExecutionEngine to keep the public API stable while
    delegating to the actual service instances.
    """

    # -- NodeExecutor proxies -------------------------------------------

    @property
    def agent_executor(self):
        """Proxy to NodeExecutor's agent_executor for backward compat."""
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

    # -- WatchdogService proxies ----------------------------------------

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

    def _get_heartbeat_settings(self, agent_type: str) -> tuple[float, int]:
        """Proxy to WatchdogService for backward compat."""
        return self._watchdog.get_heartbeat_settings(agent_type)

    def _get_alert_threshold(self, agent_type: str) -> int:
        """Proxy to WatchdogService for backward compat."""
        return self._watchdog.get_alert_threshold(agent_type)

    # -- RetryPolicyEngine proxies --------------------------------------

    @property
    def _best_attempts(self) -> dict[str, dict]:
        """Proxy to RetryPolicyEngine._best_attempts for backward compat."""
        return self._retry_policy._best_attempts
