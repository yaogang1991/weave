"""Tests for #991: evaluator stall timeout increased to match workload.

Evaluators run test suites (DB init, fixtures, multiple test cases).
120s base was too aggressive — raised to 300s base / 900s cap to match
generator scaling (#978).
"""
from core.config import EvaluatorStallScaleConfig, NodeTimeoutConfig


class TestEvaluatorStallDefaults:
    """Evaluator stall defaults should accommodate complex test suites."""

    def test_evaluator_stall_base_is_at_least_300(self):
        config = EvaluatorStallScaleConfig()
        assert config.base >= 300

    def test_evaluator_stall_cap_is_at_least_900(self):
        config = EvaluatorStallScaleConfig()
        assert config.cap >= 900

    def test_stall_timeout_for_evaluator_uses_dynamic_scale(self):
        node_config = NodeTimeoutConfig()
        # With file/test counts, evaluator should exceed the base 300s
        timeout = node_config.stall_timeout_for(
            "evaluator", file_count=10, test_count=5,
        )
        assert timeout >= 300

    def test_stall_timeout_for_evaluator_caps_at_900(self):
        node_config = NodeTimeoutConfig()
        # Even with huge counts, should not exceed cap
        timeout = node_config.stall_timeout_for(
            "evaluator", file_count=200, test_count=100,
        )
        assert timeout <= 900
