"""Progress-aware subprocess execution (M4.5).

Replaces raw ``subprocess.run`` with a poll-loop wrapper that:
1. Reports progress via ProgressTracker every 5 seconds.
2. Checks cancel_event for responsive termination.
3. Uses ``communicate(timeout=5)`` to prevent pipe buffer deadlock.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from core.progress import ProgressTracker, ProgressReport

logger = logging.getLogger(__name__)


@dataclass
class SubprocessResult:
    """Unified result from run_with_progress."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False


def run_with_progress(
    cmd: list[str] | str,
    *,
    progress_tracker: ProgressTracker | None = None,
    cancel_event: threading.Event | None = None,
    timeout: float | None = None,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    shell: bool = False,
    poll_interval: float = 5.0,
    # subprocess.run drop-in compatibility (accepted, no-op)
    capture_output: bool = True,
    text: bool = True,
    encoding: str | None = None,
    errors: str | None = None,
) -> SubprocessResult:
    """Run a subprocess with progress reporting and cancel support.

    Uses ``Popen + communicate(timeout=poll_interval)`` to:
    - Drain stdout/stderr pipes (prevents 64KB pipe buffer deadlock)
    - Report progress every poll_interval seconds
    - Check cancel_event for responsive termination

    Args:
        cmd: Command to execute (list or string if shell=True).
        progress_tracker: Optional tracker to report progress.
        cancel_event: Optional event to check for cancellation.
        timeout: Maximum wall-clock time for the subprocess.
        cwd: Working directory.
        env: Environment variables.
        shell: Run in shell mode.
        poll_interval: Seconds between progress reports / cancel checks.

    Returns:
        SubprocessResult with returncode, stdout, stderr, and status flags.
    """
    start = time.monotonic()

    if progress_tracker:
        progress_tracker.report(ProgressReport("subprocess_start"))

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
            env=env,
            shell=shell,
        )
    except FileNotFoundError as e:
        return SubprocessResult(returncode=127, stdout="", stderr=str(e))
    except Exception as e:
        return SubprocessResult(returncode=1, stdout="", stderr=str(e))

    try:
        while True:
            try:
                stdout_bytes, stderr_bytes = proc.communicate(timeout=poll_interval)
                stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
                stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
                return SubprocessResult(
                    returncode=proc.returncode,
                    stdout=stdout,
                    stderr=stderr,
                )
            except subprocess.TimeoutExpired:
                elapsed = time.monotonic() - start

                if progress_tracker:
                    progress_tracker.report(
                        ProgressReport("subprocess_poll", f"elapsed {elapsed:.0f}s"),
                    )

                if cancel_event and cancel_event.is_set():
                    proc.kill()
                    proc.communicate()
                    logger.info("Subprocess cancelled after %.0fs", elapsed)
                    return SubprocessResult(
                        returncode=-9,
                        stdout="",
                        stderr=f"Cancelled after {elapsed:.0f}s",
                        cancelled=True,
                    )

                if timeout and elapsed > timeout:
                    proc.kill()
                    proc.communicate()
                    logger.info(
                        "Subprocess timed out after %.0fs (limit %s)",
                        elapsed, timeout,
                    )
                    return SubprocessResult(
                        returncode=-9,
                        stdout="",
                        stderr=f"Timed out after {timeout}s",
                        timed_out=True,
                    )
    except Exception:
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        raise
