"""Progress-driven timeout tracking for node execution (M4.5).

Replaces static wall-clock timeout with progress-based stall detection.
All work units (LLM calls, subprocesses, tool execution) share a single
ProgressTracker and call ``report()`` when making progress.  The
node_executor polls ``should_kill()`` to decide whether to terminate.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.dag_models import DAGNode

logger = logging.getLogger(__name__)


class ProgressTracker:
    """Thread-safe progress tracker for node execution timeout.

    Usage:
        tracker = ProgressTracker(stall_timeout=120, max_total=900)
        # ... work units call tracker.report() ...
        # ... node_executor polls tracker.should_kill() ...
    """

    def __init__(
        self,
        stall_timeout: int = 120,
        max_total: int = 1200,
    ) -> None:
        self._stall_timeout = stall_timeout
        self._max_total = max_total
        self._start = time.monotonic()
        self._last_progress = self._start
        self._lock = threading.Lock()

    def report(self, phase: str = "") -> None:
        """Report progress.  Called from any thread."""
        with self._lock:
            self._last_progress = time.monotonic()
        if phase:
            logger.debug("Progress reported: %s", phase)

    def should_kill(self) -> tuple[bool, str]:
        """Check if the node should be terminated.

        Returns (should_kill, reason).  Called from the asyncio event loop.
        """
        now = time.monotonic()
        with self._lock:
            stall = now - self._last_progress
            elapsed = now - self._start
        if stall > self._stall_timeout:
            return True, f"stall ({stall:.0f}s > {self._stall_timeout}s)"
        if elapsed > self._max_total:
            return True, f"max_total ({elapsed:.0f}s > {self._max_total}s)"
        return False, ""

    def has_recent_progress(self, window: float = 10.0) -> bool:
        """Return True if progress was reported within the last *window* seconds."""
        with self._lock:
            return (time.monotonic() - self._last_progress) < window

    @property
    def elapsed(self) -> float:
        """Seconds since tracker was created."""
        return time.monotonic() - self._start

    @property
    def stall_timeout(self) -> int:
        return self._stall_timeout

    @property
    def max_total(self) -> int:
        return self._max_total


def estimate_max_timeout(
    agent_type: str,
    node: DAGNode | None = None,
    workspace_path: str | None = None,
) -> int:
    """L1: Dynamically estimate max_total_timeout based on workload.

    Returns seconds.  Falls back to sensible defaults when workspace
    info is unavailable.
    """
    if agent_type == "evaluator":
        base = int(os.getenv("WEAVE_EVAL_TIMEOUT_BASE", "480"))
        per_file = int(os.getenv("WEAVE_EVAL_TIMEOUT_PER_FILE", "8"))
        per_test = int(os.getenv("WEAVE_EVAL_TIMEOUT_PER_TEST", "5"))
        cap = int(os.getenv("WEAVE_EVAL_TIMEOUT_CAP", "1800"))

        file_count = 0
        test_count = 0
        if workspace_path:
            wp = Path(workspace_path)
            if wp.is_dir():
                file_count = sum(
                    1 for p in wp.rglob("*.py")
                    if "test" not in p.name.lower()
                    and "__pycache__" not in str(p)
                )
                test_count = sum(
                    1 for p in wp.rglob("*.py")
                    if "test" in p.name.lower()
                    and "__pycache__" not in str(p)
                )
        return min(base + file_count * per_file + test_count * per_test, cap)

    if agent_type == "generator":
        base = int(os.getenv("WEAVE_GEN_TIMEOUT_BASE", "300"))
        per_dep = int(os.getenv("WEAVE_GEN_TIMEOUT_PER_DEP", "90"))
        cap = int(os.getenv("WEAVE_GEN_TIMEOUT_CAP", "900"))

        deps = 0
        if node and hasattr(node, "dependencies") and node.dependencies:
            deps = len(node.dependencies)
        return min(base + deps * per_dep, cap)

    # planner / default
    base = int(os.getenv("WEAVE_DEFAULT_TIMEOUT_BASE", "300"))
    return base
