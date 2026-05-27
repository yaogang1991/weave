"""Fixed-size ring buffer for capturing stderr output (M6.6).

Provides ``StderrTail`` — a thread-safe, bounded buffer that retains the
most recent bytes written to it.  Used by agent backends to capture the
tail end of stderr so that error context is available even when the full
output is discarded.
"""

from __future__ import annotations

import threading


__all__ = ["StderrTail"]


class StderrTail:
    """Thread-safe fixed-size ring buffer for stderr output.

    Stores written chunks in a list, trims from the front when the
    cumulative byte count exceeds *max_bytes*.  The result of
    :meth:`tail` is always the most recent content that fits within
    the configured capacity.

    Args:
        max_bytes: Maximum number of bytes (characters) to retain.
            Defaults to 2048 (2 KB).
    """

    def __init__(self, max_bytes: int = 2048) -> None:
        self._max_bytes: int = max_bytes
        self._chunks: list[str] = []
        self._total: int = 0
        self._lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write(self, data: str) -> None:
        """Append *data* to the buffer, discarding oldest chunks on overflow.

        Args:
            data: String chunk to record (typically a line or block of
                stderr output).
        """
        if not data:
            return

        with self._lock:
            self._chunks.append(data)
            self._total += len(data)

            # Trim oldest chunks until we are back within capacity.
            while self._total > self._max_bytes and len(self._chunks) > 1:
                oldest = self._chunks.pop(0)
                self._total -= len(oldest)

    def tail(self) -> str:
        """Return the most recent content, up to *max_bytes* characters.

        Returns:
            Concatenated string of the retained chunks.
        """
        with self._lock:
            return "".join(self._chunks)

    def clear(self) -> None:
        """Reset the buffer, discarding all stored content."""
        with self._lock:
            self._chunks.clear()
            self._total = 0

    @property
    def size(self) -> int:
        """Current number of bytes (characters) stored in the buffer."""
        with self._lock:
            return self._total

    @property
    def max_size(self) -> int:
        """Maximum buffer capacity in bytes (characters)."""
        return self._max_bytes
