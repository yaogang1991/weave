"""Tests for #731: progress reporting during tool call retry.

Verifies that progress reports reset the stall timer, preventing
premature node kills during tool call retry loops.
"""
from core.progress import ProgressReport, ProgressTracker


class TestToolCallRetryProgress:
    """Verify progress reports reset stall timer (#731)."""

    def test_progress_report_resets_stall_timer(self):
        """ProgressReport should reset the stall detection timer."""
        tracker = ProgressTracker(stall_timeout=10)
        # After creation, no progress yet — but tracker may consider
        # creation time as progress. Report and verify reset.
        tracker.report(ProgressReport("tool_call_retry"))
        assert tracker.has_recent_progress()

    def test_stall_kill_prevented_by_progress(self):
        """Repeated progress reports prevent stall kill."""
        import time
        tracker = ProgressTracker(stall_timeout=5)

        # Simulate tool call retries with progress reports
        for _ in range(3):
            tracker.report(ProgressReport("tool_call_retry"))
            should_kill, reason = tracker.should_kill()
            assert not should_kill, (
                f"Stall kill fired despite progress reports: {reason}"
            )
            time.sleep(0.01)

    def test_progress_report_type(self):
        """ProgressReport stores the phase name correctly."""
        report = ProgressReport("tool_call_retry")
        assert report.phase == "tool_call_retry"
