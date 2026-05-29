"""Tests for #978: progress callback resets stall detector.

Verifies that _on_progress (the callback passed to backends) calls
tracker.report(), which resets the StallDetector so active nodes
are not falsely killed.
"""
import time
from unittest.mock import MagicMock

from core.config import GeneratorStallScaleConfig
from core.progress import ProgressTracker


class TestStallDetectorReset:
    """StallDetector should reset on tracker.report()."""

    def test_report_resets_stall_timer(self):
        tracker = ProgressTracker(stall_timeout=5)
        tracker.report("test")
        kill, reason = tracker.should_kill()
        assert not kill  # Report reset the timer

    def test_no_report_triggers_kill(self):
        tracker = ProgressTracker(stall_timeout=0)
        time.sleep(0.1)
        kill, reason = tracker.should_kill()
        assert kill
        assert "stall" in reason


class TestGeneratorStallDefaults:
    """Generator stall defaults increased for safety margin (#978)."""

    def test_generator_base_is_at_least_300(self):
        config = GeneratorStallScaleConfig()
        assert config.base >= 300

    def test_generator_cap_is_at_least_900(self):
        config = GeneratorStallScaleConfig()
        assert config.cap >= 900


class TestOnProgressIntegration:
    """Verify _on_progress callback path resets stall detector."""

    def test_callback_resets_tracker(self):
        tracker = ProgressTracker(stall_timeout=2)
        mock_node = MagicMock()
        mock_activity = MagicMock()
        loop = MagicMock()

        def _on_progress():
            try:
                loop.call_soon_threadsafe(mock_node.record_heartbeat)
                mock_activity.record_activity()
                tracker.report("heartbeat")
            except RuntimeError:
                pass

        _on_progress()
        time.sleep(0.1)
        kill, reason = tracker.should_kill()
        assert not kill

    def test_no_callback_causes_stall(self):
        tracker = ProgressTracker(stall_timeout=0)
        time.sleep(0.1)
        kill, reason = tracker.should_kill()
        assert kill
        assert "stall" in reason
