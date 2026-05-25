"""Tests for provider health failure classification (#921/#924).

Verifies that only API-level failures count toward the unhealthy threshold.
Evaluation failures, stall timeouts, and other non-API errors should not
trigger provider health degradation.
"""
import pytest

from core.provider_health import (
    FailureCategory,
    ProviderHealthConfig,
    ProviderHealthTracker,
)


class TestFailureCategory:
    """Test category-aware record_failure behavior."""

    def test_rate_limit_category_counts(self):
        tracker = ProviderHealthTracker(ProviderHealthConfig(failure_threshold=3))
        key = ("anthropic", "claude-sonnet")
        for _ in range(3):
            tracker.record_failure(*key, category=FailureCategory.RATE_LIMIT)
        assert not tracker.is_healthy(*key)

    def test_api_error_category_counts(self):
        tracker = ProviderHealthTracker(ProviderHealthConfig(failure_threshold=3))
        key = ("anthropic", "claude-sonnet")
        for _ in range(3):
            tracker.record_failure(*key, category=FailureCategory.API_ERROR)
        assert not tracker.is_healthy(*key)

    def test_evaluation_failure_does_not_count(self):
        tracker = ProviderHealthTracker(ProviderHealthConfig(failure_threshold=3))
        key = ("anthropic", "claude-sonnet")
        for _ in range(10):
            tracker.record_failure(*key, category=FailureCategory.EVALUATION)
        assert tracker.is_healthy(*key)

    def test_stall_timeout_does_not_count(self):
        tracker = ProviderHealthTracker(ProviderHealthConfig(failure_threshold=3))
        key = ("anthropic", "claude-sonnet")
        for _ in range(10):
            tracker.record_failure(*key, category=FailureCategory.STALL)
        assert tracker.is_healthy(*key)

    def test_local_timeout_does_not_count(self):
        tracker = ProviderHealthTracker(ProviderHealthConfig(failure_threshold=3))
        key = ("anthropic", "claude-sonnet")
        for _ in range(10):
            tracker.record_failure(*key, category=FailureCategory.TIMEOUT)
        assert tracker.is_healthy(*key)

    def test_unknown_treated_as_api_error(self):
        tracker = ProviderHealthTracker(ProviderHealthConfig(failure_threshold=3))
        key = ("anthropic", "claude-sonnet")
        for _ in range(3):
            tracker.record_failure(*key, category=FailureCategory.UNKNOWN)
        assert not tracker.is_healthy(*key)

    def test_mixed_failures_no_api_still_healthy(self):
        tracker = ProviderHealthTracker(ProviderHealthConfig(failure_threshold=3))
        key = ("anthropic", "claude-sonnet")
        tracker.record_failure(*key, category=FailureCategory.EVALUATION)
        tracker.record_failure(*key, category=FailureCategory.STALL)
        tracker.record_failure(*key, category=FailureCategory.TIMEOUT)
        assert tracker.is_healthy(*key)

    def test_mixed_failures_with_api_unhealthy(self):
        tracker = ProviderHealthTracker(ProviderHealthConfig(failure_threshold=3))
        key = ("anthropic", "claude-sonnet")
        tracker.record_failure(*key, category=FailureCategory.EVALUATION)
        tracker.record_failure(*key, category=FailureCategory.STALL)
        tracker.record_failure(*key, category=FailureCategory.API_ERROR)
        tracker.record_failure(*key, category=FailureCategory.API_ERROR)
        tracker.record_failure(*key, category=FailureCategory.API_ERROR)
        assert not tracker.is_healthy(*key)

    def test_success_resets_api_counter(self):
        tracker = ProviderHealthTracker(ProviderHealthConfig(failure_threshold=3))
        key = ("anthropic", "claude-sonnet")
        tracker.record_failure(*key, category=FailureCategory.API_ERROR)
        tracker.record_failure(*key, category=FailureCategory.API_ERROR)
        tracker.record_success(*key)
        tracker.record_failure(*key, category=FailureCategory.API_ERROR)
        assert tracker.is_healthy(*key)

    def test_default_backward_compatible(self):
        tracker = ProviderHealthTracker(ProviderHealthConfig(failure_threshold=3))
        key = ("anthropic", "claude-sonnet")
        for _ in range(3):
            tracker.record_failure(*key)
        assert not tracker.is_healthy(*key)


class TestClassifyFailure:
    """Test the _classify_failure helper in dag_engine."""

    def test_rate_limit_string(self):
        from core.dag_engine import _classify_failure
        assert _classify_failure("Rate limit exhausted for anthropic/claude") == FailureCategory.RATE_LIMIT

    def test_429_in_error(self):
        from core.dag_engine import _classify_failure
        assert _classify_failure("HTTP 429 Too Many Requests") == FailureCategory.RATE_LIMIT

    def test_stall_in_error(self):
        from core.dag_engine import _classify_failure
        assert _classify_failure("Node killed: stall (160s > 160s)") == FailureCategory.STALL

    def test_eval_in_error(self):
        from core.dag_engine import _classify_failure
        assert _classify_failure("Evaluation score 0.0 below threshold") == FailureCategory.EVALUATION

    def test_timeout_in_error(self):
        from core.dag_engine import _classify_failure
        assert _classify_failure("Node exceeded 120s timeout") == FailureCategory.TIMEOUT

    def test_empty_error_is_api(self):
        from core.dag_engine import _classify_failure
        assert _classify_failure("") == FailureCategory.API_ERROR

    def test_generic_error_is_api(self):
        from core.dag_engine import _classify_failure
        assert _classify_failure("Connection refused") == FailureCategory.API_ERROR

    def test_case_insensitive(self):
        from core.dag_engine import _classify_failure
        assert _classify_failure("RATE_LIMIT exhausted") == FailureCategory.RATE_LIMIT
        assert _classify_failure("STALL detected") == FailureCategory.STALL
        assert _classify_failure("Timeout occurred") == FailureCategory.TIMEOUT
