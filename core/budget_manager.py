"""M4.2: Thread-safe token budget tracker for a single DAG run."""

from __future__ import annotations

import threading
from typing import Any

from core.config import BudgetConfig


class BudgetManager:
    """Tracks token usage and enforces budget limits for a DAG run.

    Usage::

        bm = BudgetManager(BudgetConfig(total_tokens=500_000))
        if bm.check():
            # ... execute node ...
            bm.record_usage(input_tokens=1500, output_tokens=800)
        else:
            raise BudgetExhaustedError(...)
    """

    def __init__(self, config: BudgetConfig) -> None:
        self._config = config
        self._used_input: int = 0
        self._used_output: int = 0
        self._lock = threading.Lock()
        self._warning_emitted: bool = False

    @property
    def config(self) -> BudgetConfig:
        return self._config

    @property
    def used_input_tokens(self) -> int:
        with self._lock:
            return self._used_input

    @property
    def used_output_tokens(self) -> int:
        with self._lock:
            return self._used_output

    @property
    def used_total_tokens(self) -> int:
        with self._lock:
            return self._used_input + self._used_output

    @property
    def remaining_tokens(self) -> int:
        if self._config.is_unlimited:
            return -1
        with self._lock:
            return max(0, self._config.total_tokens - self._used_input - self._used_output)

    @property
    def usage_fraction(self) -> float:
        if self._config.is_unlimited:
            return 0.0
        with self._lock:
            return (self._used_input + self._used_output) / self._config.total_tokens

    def check(self) -> bool:
        """Return True if budget remains or budget is disabled/unlimited."""
        if not self._config.enabled or self._config.is_unlimited:
            return True
        with self._lock:
            return (self._used_input + self._used_output) < self._config.total_tokens

    def check_warning(self) -> bool:
        """Return True the first time usage crosses the warning threshold."""
        if not self._config.enabled or self._config.is_unlimited:
            return False
        with self._lock:
            if self._warning_emitted:
                return False
            frac = (self._used_input + self._used_output) / self._config.total_tokens
            if frac >= self._config.warning_threshold:
                self._warning_emitted = True
                return True
            return False

    def record_usage(self, input_tokens: int, output_tokens: int) -> None:
        """Add token usage. Thread-safe."""
        with self._lock:
            self._used_input += input_tokens
            self._used_output += output_tokens

    def to_dict(self) -> dict[str, Any]:
        """Serialize budget state for job result artifact.

        All reads are inside a single lock acquisition so the dict
        represents a consistent point-in-time snapshot.
        """
        with self._lock:
            used_input = self._used_input
            used_output = self._used_output
            used_total = used_input + used_output
            if self._config.is_unlimited:
                remaining = -1
                fraction = 0.0
            else:
                remaining = max(0, self._config.total_tokens - used_total)
                fraction = used_total / self._config.total_tokens
            warning = self._warning_emitted
        return {
            "enabled": self._config.enabled,
            "total_budget": self._config.total_tokens,
            "used_input_tokens": used_input,
            "used_output_tokens": used_output,
            "used_total_tokens": used_total,
            "remaining_tokens": remaining,
            "usage_fraction": round(fraction, 4),
            "warning_emitted": warning,
        }
