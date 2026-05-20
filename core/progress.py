"""Progress-driven timeout tracking for node execution (M4.5).

Replaces static wall-clock timeout with progress-based stall detection.
All work units (LLM calls, subprocesses, tool execution) share a single
ProgressTracker and call ``report()`` with structured ``ProgressReport``
when making progress.  The node_executor polls ``should_kill()`` to decide
whether to terminate.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
logger = logging.getLogger(__name__)


@dataclass
class ProgressReport:
    """Structured progress report from a work unit."""

    phase: str          # "writing_file" | "llm_call" | "tool_exec" | "subprocess"
    message: str = ""   # "writing file 3/10"
    progress: float = 0.0  # 0.0~1.0


@runtime_checkable
class ProgressObserver(Protocol):
    """Receives progress reports without influencing lease decisions."""

    def on_progress(self, report: ProgressReport) -> None: ...


@runtime_checkable
class ProgressFilter(Protocol):
    """Votes on lease renewal and can request immediate kill."""

    def should_extend(self, report: ProgressReport) -> bool: ...
    def should_kill(self) -> tuple[bool, str]: ...


class StallDetector:
    """Filter: resets stall timer on progress, kills on stall timeout."""

    def __init__(self, stall_timeout: int) -> None:
        self._stall_timeout = stall_timeout
        self._last_progress = time.monotonic()
        self._lock = threading.Lock()

    def should_extend(self, report: ProgressReport) -> bool:
        with self._lock:
            self._last_progress = time.monotonic()
        return True

    def should_kill(self) -> tuple[bool, str]:
        with self._lock:
            stall = time.monotonic() - self._last_progress
        if stall > self._stall_timeout:
            return True, f"stall ({stall:.0f}s > {self._stall_timeout}s)"
        return False, ""


class AnomalyDetector:
    """Filter: detects repetition, oscillation, and cyclic progress patterns."""

    _MAX_HISTORY = 100

    def __init__(self, max_repetitions: int = 3) -> None:
        self._max_repetitions = max_repetitions
        self._history: list[ProgressReport] = []
        self._anomalous = False
        self._lock = threading.Lock()

    def should_extend(self, report: ProgressReport) -> bool:
        with self._lock:
            self._history.append(report)
            if len(self._history) > self._MAX_HISTORY:
                self._history = self._history[-self._MAX_HISTORY:]
            if self._detect_anomaly():
                self._anomalous = True
                return False  # Don't renew lease
        return True

    def should_kill(self) -> tuple[bool, str]:
        return False, ""  # Let stall handle it naturally

    @property
    def is_anomalous(self) -> bool:
        return self._anomalous

    def _detect_anomaly(self) -> bool:
        history = self._history
        n = len(history)
        if n < self._max_repetitions:
            return False

        # Pattern 1: Same message repeated N times
        recent = history[-self._max_repetitions:]
        if all(r.message == recent[0].message and r.phase == recent[0].phase for r in recent):
            return True

        # Pattern 2: Progress oscillation (last 4 values go up then down)
        if n >= 4:
            tail = [h.progress for h in history[-4:]]
            if tail[0] < tail[1] > tail[2] < tail[3] and tail[0] > 0:
                return True

        # Pattern 3: Progress exceeds 1.0
        if history[-1].progress > 1.0:
            return True

        return False


class AuditLogger:
    """Observer: records all progress reports for observability."""

    def __init__(self) -> None:
        self._log: list[ProgressReport] = []

    def on_progress(self, report: ProgressReport) -> None:
        self._log.append(report)

    @property
    def history(self) -> list[ProgressReport]:
        return list(self._log)


class ProgressTracker:
    """Thread-safe progress tracker for node execution timeout.

    Uses Filter/Observer pub/sub:
    - Observers receive all reports (audit, dashboards).
    - Filters vote on lease renewal; any False stops renewal.
    - should_kill() checks stall + all filters.
    """

    def __init__(
        self,
        stall_timeout: int = 120,
        observers: list[ProgressObserver] | None = None,
        filters: list[ProgressFilter] | None = None,
    ) -> None:
        self._stall_timeout = stall_timeout
        self._start = time.monotonic()
        self._last_progress = self._start
        self._lock = threading.Lock()
        self._observers = observers or []
        # Always include StallDetector as the first filter
        self._stall_detector = StallDetector(stall_timeout)
        self._filters = [self._stall_detector] + (filters or [])

    def report(self, report_or_phase: ProgressReport | str, message: str = "") -> None:
        """Report progress.  Called from any thread."""
        if isinstance(report_or_phase, str):
            report = ProgressReport(phase=report_or_phase, message=message)
        else:
            report = report_or_phase

        # Notify observers
        for obs in self._observers:
            try:
                obs.on_progress(report)
            except Exception:
                pass

        # Ask filters: extend lease only if ALL agree
        should_extend = all(f.should_extend(report) for f in self._filters)
        if should_extend:
            with self._lock:
                self._last_progress = time.monotonic()

        if report.phase:
            logger.debug("Progress reported: %s %s", report.phase, report.message)

    def should_kill(self) -> tuple[bool, str]:
        """Check if the node should be terminated."""
        for f in self._filters:
            kill, reason = f.should_kill()
            if kill:
                return True, reason
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
    def history(self) -> list[ProgressReport]:
        """Get audit history from any AuditLogger observer."""
        for obs in self._observers:
            if isinstance(obs, AuditLogger):
                return obs.history
        return []
