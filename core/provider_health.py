"""Cross-node provider health detection (#900).

Tracks consecutive LLM call failures per (provider, model) pair.
When failures exceed a threshold, marks the provider as unhealthy
so the DAG engine can skip subsequent nodes that would also fail.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ProviderHealthConfig:
    failure_threshold: int = 3
    recovery_cooldown_sec: float = 300.0


@dataclass
class _ProviderState:
    consecutive_failures: int = 0
    marked_unhealthy_at: float = 0.0


class ProviderHealthTracker:
    """Thread-safe tracker for per-provider health state."""

    def __init__(self, config: ProviderHealthConfig | None = None) -> None:
        self._config = config or ProviderHealthConfig()
        self._states: dict[str, _ProviderState] = {}
        self._lock = threading.Lock()

    def _key(self, provider: str, model: str) -> str:
        return f"{provider}/{model}"

    def record_failure(self, provider: str, model: str) -> None:
        key = self._key(provider, model)
        with self._lock:
            state = self._states.setdefault(key, _ProviderState())
            state.consecutive_failures += 1
            if (
                state.consecutive_failures >= self._config.failure_threshold
                and not state.marked_unhealthy_at
            ):
                state.marked_unhealthy_at = time.monotonic()
                logger.warning(
                    "Provider %s marked unhealthy after %d consecutive failures (#900)",
                    key, state.consecutive_failures,
                )

    def record_success(self, provider: str, model: str) -> None:
        key = self._key(provider, model)
        with self._lock:
            state = self._states.setdefault(key, _ProviderState())
            if state.consecutive_failures > 0:
                logger.info(
                    "Provider %s recovered after %d failures (#900)",
                    key, state.consecutive_failures,
                )
            state.consecutive_failures = 0
            state.marked_unhealthy_at = 0.0

    def is_healthy(self, provider: str, model: str) -> bool:
        key = self._key(provider, model)
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return True
            if not state.marked_unhealthy_at:
                return True
            # Auto-recover after cooldown
            elapsed = time.monotonic() - state.marked_unhealthy_at
            if elapsed >= self._config.recovery_cooldown_sec:
                state.consecutive_failures = 0
                state.marked_unhealthy_at = 0.0
                logger.info("Provider %s auto-recovered after %.0fs (#900)", key, elapsed)
                return True
            return False

    @property
    def config(self) -> ProviderHealthConfig:
        return self._config
