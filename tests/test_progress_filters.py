"""Tests for M4.5 progress tracking: ProgressReport, Filter/Observer, AnomalyDetector."""
import time
import pytest

from core.progress import (
    ProgressReport,
    ProgressTracker,
    StallDetector,
    AnomalyDetector,
    AuditLogger,
)


class TestProgressReport:
    def test_basic_creation(self):
        r = ProgressReport("writing_file", "file 3/10", 0.3)
        assert r.phase == "writing_file"
        assert r.message == "file 3/10"
        assert r.progress == 0.3

    def test_defaults(self):
        r = ProgressReport("llm_call")
        assert r.message == ""
        assert r.progress == 0.0


class TestProgressTracker:
    def test_string_report_backward_compat(self):
        tracker = ProgressTracker(stall_timeout=60)
        tracker.report("some_phase")
        assert not tracker.should_kill()[0]

    def test_structured_report(self):
        tracker = ProgressTracker(stall_timeout=60)
        tracker.report(ProgressReport("tool_exec", "running pytest", 0.5))
        assert not tracker.should_kill()[0]

    def test_stall_kill(self):
        tracker = ProgressTracker(stall_timeout=1)
        time.sleep(1.1)
        kill, reason = tracker.should_kill()
        assert kill
        assert "stall" in reason

    def test_no_stall_with_progress(self):
        tracker = ProgressTracker(stall_timeout=300)
        tracker.report(ProgressReport("work"))
        kill, _ = tracker.should_kill()
        assert not kill


class TestStallDetector:
    def test_extends_on_progress(self):
        det = StallDetector(stall_timeout=300)
        assert det.should_extend(ProgressReport("work"))
        assert not det.should_kill()[0]


class TestAnomalyDetector:
    def test_no_anomaly_on_varied_reports(self):
        det = AnomalyDetector(max_repetitions=3)
        for i in range(5):
            assert det.should_extend(
                ProgressReport("work", f"step {i}", i / 5.0)
            )
        assert not det.is_anomalous

    def test_repetition_detected(self):
        det = AnomalyDetector(max_repetitions=3)
        report = ProgressReport("stuck", "same message", 0.5)
        det.should_extend(report)
        det.should_extend(report)
        result = det.should_extend(report)
        assert not result  # Anomaly: should not extend
        assert det.is_anomalous

    def test_progress_over_1_detected(self):
        det = AnomalyDetector(max_repetitions=3)
        det.should_extend(ProgressReport("work", "step 1", 0.5))
        det.should_extend(ProgressReport("work", "step 2", 0.8))
        det.should_extend(ProgressReport("work", "step 3", 1.5))
        assert det.is_anomalous

    def test_does_not_directly_kill(self):
        det = AnomalyDetector()
        assert not det.should_kill()[0]

    def test_should_kill_returns_true_after_anomaly(self):
        """#659: should_kill() returns (True, 'anomaly detected') after
        anomaly is flagged by should_extend()."""
        det = AnomalyDetector(max_repetitions=3)
        report = ProgressReport("stuck", "same message", 0.5)
        det.should_extend(report)
        det.should_extend(report)
        det.should_extend(report)  # Triggers repetition anomaly
        kill, reason = det.should_kill()
        assert kill
        assert reason == "anomaly detected"


class TestAuditLogger:
    def test_records_history(self):
        logger = AuditLogger()
        logger.on_progress(ProgressReport("a", "msg a"))
        logger.on_progress(ProgressReport("b", "msg b"))
        assert len(logger.history) == 2
        assert logger.history[0].phase == "a"

    def test_history_is_copy(self):
        logger = AuditLogger()
        logger.on_progress(ProgressReport("a"))
        h = logger.history
        h.clear()
        assert len(logger.history) == 1  # Original untouched


class TestFilterObserverIntegration:
    def test_anomaly_stops_renewal_stall_kills(self):
        tracker = ProgressTracker(
            stall_timeout=1,
            filters=[AnomalyDetector(max_repetitions=2)],
        )
        report = ProgressReport("stuck", "same", 0.5)
        tracker.report(report)
        tracker.report(report)  # Repetition: anomaly stops renewal
        time.sleep(1.1)
        kill, reason = tracker.should_kill()
        assert kill

    def test_observer_receives_all_reports(self):
        audit = AuditLogger()
        tracker = ProgressTracker(stall_timeout=60, observers=[audit])
        tracker.report(ProgressReport("a"))
        tracker.report(ProgressReport("b"))
        assert len(audit.history) == 2

    def test_tracker_history_from_audit(self):
        audit = AuditLogger()
        tracker = ProgressTracker(stall_timeout=60, observers=[audit])
        tracker.report(ProgressReport("phase1", "msg"))
        assert len(tracker.history) == 1
