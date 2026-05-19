"""Unit tests for M4.2 StuckDetector."""

import pytest

from core.stuck_detector import StuckDetector, StuckPattern


def _msg(tool_calls=None, content=""):
    m = {"role": "assistant", "content": content}
    if tool_calls is not None:
        m["tool_calls"] = tool_calls
    return m


def _tc(name="bash", arguments=None):
    if arguments is None:
        arguments = {"command": "ls"}
    return {"id": "tc_1", "name": name, "arguments": arguments}


class TestStuckDetector:
    def test_not_stuck_with_valid_tool_calls(self):
        sd = StuckDetector()
        result = sd.observe(_msg(tool_calls=[_tc()]), any_tool_executed=True)
        assert result.is_stuck is False

    def test_empty_args_increment_counter(self):
        sd = StuckDetector(empty_call_limit=3)
        for _ in range(2):
            r = sd.observe(
                _msg(tool_calls=[_tc(arguments={"command": ""})]),
                any_tool_executed=False,
            )
            assert not r.is_stuck
        r = sd.observe(
            _msg(tool_calls=[_tc(arguments={"command": ""})]),
            any_tool_executed=False,
        )
        assert r.is_stuck
        assert r.pattern == StuckPattern.EMPTY_ARGS
        assert r.consecutive_count == 3

    def test_successful_tool_resets_counters(self):
        sd = StuckDetector(empty_call_limit=3)
        sd.observe(
            _msg(tool_calls=[_tc(arguments={"command": ""})]),
            any_tool_executed=False,
        )
        sd.observe(
            _msg(tool_calls=[_tc(arguments={"command": ""})]),
            any_tool_executed=False,
        )
        r = sd.observe(_msg(tool_calls=[_tc()]), any_tool_executed=True)
        assert not r.is_stuck
        assert sd.state["consecutive_empty"] == 0

    def test_degenerate_args_triggers_at_threshold(self):
        sd = StuckDetector(degenerate_call_limit=3)
        for _ in range(2):
            r = sd.observe(
                _msg(tool_calls=[_tc(arguments={})]),
                any_tool_executed=False,
            )
            assert not r.is_stuck
        r = sd.observe(
            _msg(tool_calls=[_tc(arguments={})]),
            any_tool_executed=False,
        )
        assert r.is_stuck
        assert r.pattern == StuckPattern.DEGENERATE_ARGS
        assert r.consecutive_count == 3

    def test_repeat_content_triggers(self):
        sd = StuckDetector(repeat_content_limit=2)
        msg = _msg(content="I cannot proceed further.")
        # First call sets _last_content, not counted as repeat
        r = sd.observe(msg, any_tool_executed=False)
        assert not r.is_stuck
        # Second call: first repeat (count=1)
        r = sd.observe(msg, any_tool_executed=False)
        assert not r.is_stuck
        # Third call: second repeat (count=2 >= limit=2)
        r = sd.observe(msg, any_tool_executed=False)
        assert r.is_stuck
        assert r.pattern == StuckPattern.REPEAT_CONTENT

    def test_mixed_patterns_reset_correctly(self):
        sd = StuckDetector(empty_call_limit=5, degenerate_call_limit=3)
        sd.observe(
            _msg(tool_calls=[_tc(arguments={"command": ""})]),
            any_tool_executed=False,
        )
        sd.observe(_msg(tool_calls=[_tc()]), any_tool_executed=True)
        assert sd.state["consecutive_empty"] == 0
        assert sd.state["consecutive_degenerate"] == 0

    def test_reset_clears_all_state(self):
        sd = StuckDetector(empty_call_limit=3)
        sd.observe(
            _msg(tool_calls=[_tc(arguments={})]),
            any_tool_executed=False,
        )
        sd.reset()
        assert sd.state["consecutive_empty"] == 0
        assert sd.state["consecutive_degenerate"] == 0
        assert sd.state["consecutive_repeat"] == 0

    def test_state_reflects_current_counters(self):
        sd = StuckDetector()
        sd.observe(
            _msg(tool_calls=[_tc(arguments={})]),
            any_tool_executed=False,
        )
        assert sd.state["consecutive_empty"] == 1
        assert sd.state["consecutive_degenerate"] == 1

    def test_no_tool_calls_with_different_content_not_stuck(self):
        sd = StuckDetector(repeat_content_limit=3)
        r = sd.observe(_msg(content="first"), any_tool_executed=False)
        assert not r.is_stuck
        r = sd.observe(_msg(content="second"), any_tool_executed=False)
        assert not r.is_stuck

    def test_degenerate_has_lower_threshold_than_empty(self):
        sd = StuckDetector(empty_call_limit=10, degenerate_call_limit=2)
        r = sd.observe(
            _msg(tool_calls=[_tc(arguments={})]),
            any_tool_executed=False,
        )
        assert not r.is_stuck
        r = sd.observe(
            _msg(tool_calls=[_tc(arguments={})]),
            any_tool_executed=False,
        )
        assert r.is_stuck
        assert r.pattern == StuckPattern.DEGENERATE_ARGS
