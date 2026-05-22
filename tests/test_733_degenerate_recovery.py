"""Tests for #733: stronger recovery for degenerate empty-args loop.

Verifies:
1. Recovery hint on first detection
2. Simplified task hint on second+ detection (consecutive_count >= 2)
"""
from core.stuck_detector import StuckDetector


class TestDegenerateEmptyArgsRecovery:
    """Verify degenerate empty-args recovery escalation (#733)."""

    def test_first_detection_gives_basic_hint(self):
        """First degenerate detection should give basic recovery hint."""
        detector = StuckDetector()
        # Simulate a degenerate tool call (empty args, no tool executed)
        result = detector.observe(
            {"tool_calls": [{"name": "write", "arguments": {}}]},
            any_tool_executed=False,
        )
        # First detection should trigger needs_hint
        assert result.needs_hint
        assert result.consecutive_count == 1

    def test_repeated_detection_triggers_simplified_hint(self):
        """Second detection should trigger simplified task recovery (#733)."""
        detector = StuckDetector(degenerate_call_limit=3)

        # First degenerate call — basic hint
        r1 = detector.observe(
            {"tool_calls": [{"name": "write", "arguments": {}}]},
            any_tool_executed=False,
        )
        assert r1.needs_hint
        assert r1.consecutive_count == 1

        # Second degenerate call — stronger hint (#733)
        r2 = detector.observe(
            {"tool_calls": [{"name": "write", "arguments": {}}]},
            any_tool_executed=False,
        )
        assert r2.needs_hint
        assert r2.consecutive_count == 2

        # Third degenerate call — stuck (count=3 >= limit=3)
        r3 = detector.observe(
            {"tool_calls": [{"name": "write", "arguments": {}}]},
            any_tool_executed=False,
        )
        assert r3.is_stuck

    def test_successful_tool_call_resets_detector(self):
        """A successful tool call should reset the degenerate counter."""
        detector = StuckDetector()

        # First degenerate call
        detector.observe(
            {"tool_calls": [{"name": "write", "arguments": {}}]},
            any_tool_executed=False,
        )

        # Successful tool call
        detector.observe(
            {"tool_calls": [{"name": "write", "arguments": {"file_path": "a.py"}}]},
            any_tool_executed=True,
        )

        # Next degenerate call should reset counter
        r = detector.observe(
            {"tool_calls": [{"name": "write", "arguments": {}}]},
            any_tool_executed=False,
        )
        assert r.consecutive_count == 1  # Reset, not 3
