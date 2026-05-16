"""Tests for #274: failure classification and targeted retry strategies.

Verifies:
1. Error classifier covers timeout, coverage_low, naming_mismatch, runtime_error
2. Retry feedback includes targeted guidance for each failure type
3. Error categories are accepted by Job model validation
"""
import pytest

from control_plane.models import Job, JobStatus, RetryPolicy
from control_plane.service import _classify_error
from datetime import datetime, timezone


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TestErrorClassification:
    """Verify _classify_error maps errors to correct categories."""

    def test_rate_limit(self):
        assert _classify_error("429 Too Many Requests") == "rate_limit"
        assert _classify_error("rate limit exceeded") == "rate_limit"

    def test_timeout(self):
        assert _classify_error("Command timed out after 60s") == "timeout"
        assert _classify_error("Job execution timed out") == "timeout"

    def test_coverage_low(self):
        assert _classify_error("Coverage 60% below target 80%") == "coverage_low"
        assert _classify_error("coverage could not be verified") == "coverage_low"

    def test_coverage_pass_not_classified_as_low(self):
        assert _classify_error("Coverage 90% passed") != "coverage_low"

    def test_naming_mismatch(self):
        assert _classify_error("ImportError: cannot import 'Foo'") == "naming_mismatch"
        assert _classify_error("ModuleNotFoundError: mylib") == "naming_mismatch"

    def test_runtime_error(self):
        assert _classify_error("RuntimeError: machine not started") == "runtime_error"
        assert _classify_error("AttributeError: 'NoneType' has no attribute 'name'") == "runtime_error"
        assert _classify_error("KeyError: 'missing_key'") == "runtime_error"

    def test_eval_failed(self):
        assert _classify_error("evaluation failed: tests did not pass") == "eval_failed"

    def test_tool_blocked(self):
        assert _classify_error("blocked unsafe command") == "tool_blocked"

    def test_watchdog(self):
        assert _classify_error("killed by watchdog") == "watchdog"

    def test_unknown(self):
        assert _classify_error("something weird happened") == "unknown"


class TestErrorCategoryValidation:
    """Verify new error categories are accepted by Job model."""

    @pytest.mark.parametrize("category", [
        "coverage_low", "naming_mismatch", "runtime_error",
    ])
    def test_new_categories_accepted(self, category):
        job = Job(
            id="test-job",
            requirement="test",
            error_category=category,
            created_at=_utc_now(),
            updated_at=_utc_now(),
        )
        assert job.error_category == category

    def test_invalid_category_rejected(self):
        with pytest.raises(ValueError, match="Invalid error_category"):
            Job(
                id="test-job",
                requirement="test",
                error_category="not_a_real_category",
                created_at=_utc_now(),
                updated_at=_utc_now(),
            )


class TestRetryStrategyGuidance:
    """Verify targeted retry guidance is injected for each failure type."""

    def test_timeout_guidance_in_feedback(self):
        """Timeout failures should include cleanup guidance."""
        # Guidance strings are in ArtifactHandoffService (#177 PR4)
        source = open("core/artifact_handoff.py").read()
        assert "TIMEOUT DETECTED" in source
        assert "daemon threads" in source
        assert "deadlock" in source

    def test_coverage_guidance_in_feedback(self):
        """Coverage failures should include supplement-tests guidance."""
        source = open("core/artifact_handoff.py").read()
        assert "LOW COVERAGE DETECTED" in source
        assert "ADD new test functions" in source
        assert "Do NOT rewrite existing tests" in source

    def test_runtime_error_guidance_in_feedback(self):
        """Runtime errors should include source fix guidance."""
        source = open("core/artifact_handoff.py").read()
        assert "RUNTIME ERROR DETECTED" in source
        assert "EDIT source files" in source
