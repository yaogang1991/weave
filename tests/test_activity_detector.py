"""Unit tests for M6.6 ActivityDetector (semantic inactivity timeout)."""

import threading
import time

from core.activity_detector import ActivityDetector, is_meaningful_event, MEANINGFUL_EVENTS
from core.backend_models import BackendContext


class TestActivityDetector:
    """Tests for the semantic inactivity timeout detector."""

    def test_initial_state(self):
        """is_active is True immediately after creation."""
        det = ActivityDetector(timeout_seconds=60)
        assert det.is_active is True

    def test_no_timeout_within_window(self):
        """check_timeout() returns (False, '') within the timeout window."""
        det = ActivityDetector(timeout_seconds=60)
        det.record_activity("assistant")
        timed_out, reason = det.check_timeout()
        assert timed_out is False
        assert reason == ""

    def test_timeout_after_window(self):
        """Timeout triggers after timeout_seconds have elapsed."""
        det = ActivityDetector(timeout_seconds=0.1)
        det.record_activity("assistant")
        time.sleep(0.2)
        timed_out, reason = det.check_timeout()
        assert timed_out is True
        assert "No meaningful activity" in reason
        assert "timeout: 0.1s" in reason

    def test_record_activity_resets_timer(self):
        """Recording activity after almost timing out resets the timer."""
        det = ActivityDetector(timeout_seconds=0.2)
        det.record_activity("assistant")
        time.sleep(0.1)
        # Not timed out yet
        assert det.check_timeout() == (False, "")
        # Record activity to reset the timer
        det.record_activity("tool_use")
        time.sleep(0.1)
        # Still within the new window
        assert det.check_timeout() == (False, "")

    def test_elapsed_since_activity(self):
        """elapsed_since_activity increases as time passes."""
        det = ActivityDetector(timeout_seconds=60)
        det.record_activity("assistant")
        elapsed_1 = det.elapsed_since_activity
        time.sleep(0.1)
        elapsed_2 = det.elapsed_since_activity
        assert elapsed_2 > elapsed_1

    def test_custom_timeout(self):
        """A custom timeout_seconds can be set via the constructor."""
        det = ActivityDetector(timeout_seconds=42.0)
        assert det.timeout_seconds == 42.0

    def test_reset_clears_timer(self):
        """reset() resets the activity timer to the current time."""
        det = ActivityDetector(timeout_seconds=0.1)
        time.sleep(0.2)
        # Should have timed out
        assert det.check_timeout()[0] is True
        # Reset brings it back to active
        det.reset()
        assert det.is_active is True
        assert det.check_timeout() == (False, "")

    def test_event_type_param(self):
        """record_activity with meaningful event types resets the timer."""
        det = ActivityDetector(timeout_seconds=0.1)
        for event_type in ("assistant", "tool_use", "tool_result", "content_block_delta"):
            time.sleep(0.2)
            # Should be timed out before recording
            assert det.check_timeout()[0] is True
            det.record_activity(event_type)
            # Now active again
            assert det.is_active is True

    def test_is_meaningful_event(self):
        """is_meaningful_event and MEANINGFUL_EVENTS contain the expected set."""
        expected = {"assistant", "tool_use", "tool_result", "content_block_delta"}
        assert set(MEANINGFUL_EVENTS) == expected
        for event_type in expected:
            assert is_meaningful_event(event_type) is True
        # Non-meaningful events should return False
        assert is_meaningful_event("ping") is False
        assert is_meaningful_event("") is False
        assert is_meaningful_event("heartbeat") is False

    def test_non_meaningful_event_ignored(self):
        """record_activity with a non-meaningful event type does not reset the timer."""
        det = ActivityDetector(timeout_seconds=0.1)
        time.sleep(0.2)
        assert det.check_timeout()[0] is True
        # A non-meaningful event should NOT reset the timer
        det.record_activity("heartbeat")
        assert det.check_timeout()[0] is True

    def test_empty_event_type_resets(self):
        """record_activity with an empty event_type resets the timer (manual reset)."""
        det = ActivityDetector(timeout_seconds=0.1)
        time.sleep(0.2)
        assert det.check_timeout()[0] is True
        # Empty string is treated as a direct reset
        det.record_activity("")
        assert det.is_active is True

    def test_thread_safety(self):
        """Concurrent record_activity and check_timeout do not crash."""
        det = ActivityDetector(timeout_seconds=5.0)
        stop = threading.Event()
        errors: list[Exception] = []

        def writer() -> None:
            try:
                for _ in range(500):
                    det.record_activity("assistant")
                    time.sleep(0.001)
            except Exception as exc:
                errors.append(exc)

        def checker() -> None:
            try:
                while not stop.is_set():
                    det.check_timeout()
                    det.is_active
                    det.elapsed_since_activity
                    time.sleep(0.001)
            except Exception as exc:
                errors.append(exc)

        t_writer = threading.Thread(target=writer)
        t_checker = threading.Thread(target=checker)
        t_writer.start()
        t_checker.start()

        t_writer.join()
        stop.set()
        t_checker.join()

        assert errors == []

    def test_activity_detector_with_backend_context(self):
        """BackendContext can carry an activity_detector instance."""
        det = ActivityDetector(timeout_seconds=120)
        ctx = BackendContext(
            node=None,
            session_id="test-session",
            activity_detector=det,
        )
        assert ctx.activity_detector is det
        assert ctx.activity_detector.timeout_seconds == 120
        assert ctx.activity_detector.is_active is True
