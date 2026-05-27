"""M6.6: Semantic inactivity timeout detector.

Tracks "last meaningful event" timestamps to detect when a stream-json event
flow (e.g. from a CLI backend) has gone silent.  This generalises the concept
from ``core/stuck_detector.py`` (tool-call-pattern stuck detection) and
``core/progress.py`` StallDetector (progress-report timestamp tracking) into a
single, reusable component suitable for any event-driven backend.
"""

from __future__ import annotations

import threading
import time

from typing import Final

__all__ = ["ActivityDetector", "is_meaningful_event", "MEANINGFUL_EVENTS"]

MEANINGFUL_EVENTS: Final[frozenset[str]] = frozenset(
    {
        "assistant",
        "tool_use",
        "tool_result",
        "content_block_delta",
    }
)
"""Stream event types that reset the inactivity timer."""


def is_meaningful_event(event_type: str) -> bool:
    """Return *True* if *event_type* counts as meaningful activity."""
    return event_type in MEANINGFUL_EVENTS


class ActivityDetector:
    """Thread-safe semantic inactivity timeout detector.

    Create one instance per backend session.  Call :meth:`record_activity`
    whenever a meaningful stream event arrives; call :meth:`check_timeout`
    periodically (e.g. from a watchdog coroutine) to detect silence.

    Args:
        timeout_seconds: Seconds of silence before a timeout is reported.
            Defaults to 600 (10 minutes).
    """

    def __init__(self, timeout_seconds: float = 600.0) -> None:
        self._timeout_seconds = timeout_seconds
        self._last_activity: float = time.monotonic()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_activity(self, event_type: str = "") -> None:
        """Record that a meaningful event occurred, resetting the timer.

        If *event_type* is given and is **not** in :data:`MEANINGFUL_EVENTS`,
        the call is silently ignored so that noisy-but-unimportant events do
        not mask a genuine stall.

        Thread-safe.
        """
        if event_type and not is_meaningful_event(event_type):
            return
        with self._lock:
            self._last_activity = time.monotonic()

    def check_timeout(self) -> tuple[bool, str]:
        """Return ``(True, reason)`` if the timeout has been exceeded.

        Thread-safe.
        """
        with self._lock:
            elapsed = time.monotonic() - self._last_activity
        if elapsed >= self._timeout_seconds:
            reason = (
                f"No meaningful activity for {elapsed:.1f}s "
                f"(timeout: {self._timeout_seconds:.1f}s)"
            )
            return True, reason
        return False, ""

    def reset(self) -> None:
        """Reset the timer to *now* regardless of event semantics."""
        with self._lock:
            self._last_activity = time.monotonic()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def elapsed_since_activity(self) -> float:
        """Seconds since the last recorded meaningful event."""
        with self._lock:
            return time.monotonic() - self._last_activity

    @property
    def timeout_seconds(self) -> float:
        """Configured timeout in seconds."""
        return self._timeout_seconds

    @property
    def is_active(self) -> bool:
        """``True`` if the last activity is within the timeout window."""
        with self._lock:
            return (time.monotonic() - self._last_activity) < self._timeout_seconds
