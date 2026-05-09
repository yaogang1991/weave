"""
Tests for monitoring.metrics — MetricsCollector and MetricsReporter.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from control_plane.models import Job, JobStatus, Run, RunStatus
from control_plane.repository import JobRepository
from monitoring.metrics import MetricsCollector, MetricsReporter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc(dt_str: str) -> datetime:
    """Parse ISO string to timezone-aware datetime."""
    return datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)


def make_job(
    repo: JobRepository,
    status: JobStatus = JobStatus.SUCCEEDED,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    attempt: int = 1,
    last_error: str = "",
    error_category: str = "",
) -> Job:
    """Create and persist a Job with the given parameters."""
    now = datetime.now(timezone.utc)
    job = Job(
        id=f"job_{created_at.isoformat() if created_at else now.isoformat()}_{status.value}",
        requirement="test requirement",
        status=status,
        created_at=created_at or now,
        updated_at=updated_at or (created_at or now),
        attempt=attempt,
        last_error=last_error,
        error_category=error_category,
    )
    repo._persist_job(job)  # type: ignore[attr-defined]
    return job


def make_run(
    repo: JobRepository,
    job_id: str,
    started_at: datetime,
    completed_at: datetime | None = None,
    status: RunStatus = RunStatus.SUCCEEDED,
) -> Run:
    """Create and persist a Run associated with *job_id*."""
    now = datetime.now(timezone.utc)
    run = Run(
        id=f"run_{job_id}_{started_at.isoformat()}",
        job_id=job_id,
        session_id="sess_test",
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        created_at=started_at,
        updated_at=completed_at or now,
    )
    repo._persist_run(run)  # type: ignore[attr-defined]
    return run


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path) -> JobRepository:
    """Fresh JobRepository in a temporary directory."""
    base = tmp_path / "jobs"
    base.mkdir()
    return JobRepository(str(base))


@pytest.fixture
def collector(repo: JobRepository) -> MetricsCollector:
    return MetricsCollector(repo)


@pytest.fixture
def reporter() -> MetricsReporter:
    return MetricsReporter()


# ---------------------------------------------------------------------------
# MetricsCollector.collect() — completeness
# ---------------------------------------------------------------------------


class TestCollectCompleteness:
    def test_returns_expected_top_level_keys(self, collector: MetricsCollector):
        """collect() must return all expected top-level sections."""
        metrics = collector.collect()
        assert "timestamp" in metrics
        assert "period" in metrics
        assert "summary" in metrics
        assert "duration" in metrics
        assert "retries" in metrics
        assert "failures" in metrics
        assert "throughput" in metrics

    def test_timestamp_is_iso_format(self, collector: MetricsCollector):
        metrics = collector.collect()
        # Should be parseable
        datetime.fromisoformat(metrics["timestamp"])

    def test_period_fields(self, collector: MetricsCollector):
        """period.since / period.until should be None when not filtered."""
        metrics = collector.collect()
        assert metrics["period"]["since"] is None
        assert metrics["period"]["until"] is None

    def test_period_with_since_until(self, collector: MetricsCollector):
        since = datetime(2024, 1, 1, tzinfo=timezone.utc)
        until = datetime(2024, 12, 31, tzinfo=timezone.utc)
        metrics = collector.collect(since=since, until=until)
        assert metrics["period"]["since"] == "2024-01-01T00:00:00+00:00"
        assert metrics["period"]["until"] == "2024-12-31T00:00:00+00:00"


# ---------------------------------------------------------------------------
# job_success_rate
# ---------------------------------------------------------------------------


class TestSuccessRate:
    def test_all_succeeded(self, repo: JobRepository, collector: MetricsCollector):
        for i in range(5):
            make_job(repo, JobStatus.SUCCEEDED)
        metrics = collector.collect()
        assert metrics["summary"]["total"] == 5
        assert metrics["summary"]["succeeded"] == 5
        assert metrics["summary"]["success_rate"] == 100.0

    def test_half_failed(self, repo: JobRepository, collector: MetricsCollector):
        for i in range(3):
            make_job(repo, JobStatus.SUCCEEDED)
        for i in range(3):
            make_job(repo, JobStatus.FAILED)
        metrics = collector.collect()
        assert metrics["summary"]["total"] == 6
        assert metrics["summary"]["succeeded"] == 3
        assert metrics["summary"]["failed"] == 3
        assert metrics["summary"]["success_rate"] == 50.0

    def test_zero_jobs(self, repo: JobRepository, collector: MetricsCollector):
        metrics = collector.collect()
        assert metrics["summary"]["total"] == 0
        assert metrics["summary"]["success_rate"] == 0.0

    def test_mixed_statuses(self, repo: JobRepository, collector: MetricsCollector):
        make_job(repo, JobStatus.SUCCEEDED)
        make_job(repo, JobStatus.FAILED)
        make_job(repo, JobStatus.CANCELED)
        make_job(repo, JobStatus.DEAD_LETTER)
        metrics = collector.collect()
        assert metrics["summary"]["total"] == 4
        assert metrics["summary"]["succeeded"] == 1
        assert metrics["summary"]["failed"] == 1
        assert metrics["summary"]["canceled"] == 1
        assert metrics["summary"]["dead_letter"] == 1
        assert metrics["summary"]["success_rate"] == 25.0

    def test_time_filter_excludes_outside_range(
        self, repo: JobRepository, collector: MetricsCollector
    ):
        """Jobs outside the since..until range must not be counted."""
        make_job(
            repo,
            JobStatus.FAILED,
            created_at=datetime(2023, 6, 1, tzinfo=timezone.utc),
            updated_at=datetime(2023, 6, 1, tzinfo=timezone.utc),
        )
        make_job(
            repo,
            JobStatus.SUCCEEDED,
            created_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
            updated_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
        )
        metrics = collector.collect(
            since=datetime(2024, 1, 1, tzinfo=timezone.utc),
            until=datetime(2024, 12, 31, tzinfo=timezone.utc),
        )
        assert metrics["summary"]["total"] == 1
        assert metrics["summary"]["succeeded"] == 1
        assert metrics["summary"]["success_rate"] == 100.0


# ---------------------------------------------------------------------------
# job_duration_p95
# ---------------------------------------------------------------------------


class TestDurationStats:
    def test_no_runs(self, repo: JobRepository, collector: MetricsCollector):
        make_job(repo, JobStatus.SUCCEEDED)
        metrics = collector.collect()
        assert metrics["duration"]["count"] == 0
        assert metrics["duration"]["p95_sec"] == 0

    def test_single_run(self, repo: JobRepository, collector: MetricsCollector):
        job = make_job(repo, JobStatus.SUCCEEDED)
        make_run(
            repo,
            job.id,
            started_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            completed_at=datetime(2024, 1, 1, 12, 0, 30, tzinfo=timezone.utc),
        )
        metrics = collector.collect()
        assert metrics["duration"]["count"] == 1
        assert metrics["duration"]["mean_sec"] == 30.0
        assert metrics["duration"]["p50_sec"] == 30.0
        assert metrics["duration"]["p95_sec"] == 30.0  # n < 20 => last value
        assert metrics["duration"]["max_sec"] == 30.0

    def test_p95_with_many_runs(
        self, repo: JobRepository, collector: MetricsCollector
    ):
        """With >=20 runs, p95_sec should pick the 95th percentile element."""
        job = make_job(repo, JobStatus.SUCCEEDED)
        durations = [float(i) for i in range(25)]  # 0..24 seconds
        for i, sec in enumerate(durations):
            make_run(
                repo,
                job.id,
                started_at=datetime(
                    2024, 1, 1, 12, 0, i, tzinfo=timezone.utc
                ),
                completed_at=datetime(
                    2024, 1, 1, 12, 0, i + int(sec), tzinfo=timezone.utc
                ),
            )
        metrics = collector.collect()
        assert metrics["duration"]["count"] == 25
        # sorted durations: [0,1,...,24]; p95 index = int(25 * 0.95) = 23 => 23.0
        assert metrics["duration"]["p95_sec"] == 23.0
        assert metrics["duration"]["p50_sec"] == 12.0

    def test_p95_fallback_when_fewer_than_20(
        self, repo: JobRepository, collector: MetricsCollector
    ):
        """With <20 runs, p95 falls back to the max value."""
        job = make_job(repo, JobStatus.SUCCEEDED)
        for i, sec in enumerate([10, 20, 30, 40, 50]):
            make_run(
                repo,
                job.id,
                started_at=datetime(
                    2024, 1, 1, 12, 0, i, tzinfo=timezone.utc
                ),
                completed_at=datetime(
                    2024, 1, 1, 12, 0, i + sec, tzinfo=timezone.utc
                ),
            )
        metrics = collector.collect()
        assert metrics["duration"]["count"] == 5  # n < 20
        assert metrics["duration"]["p95_sec"] == 50.0  # max fallback

    def test_uncompleted_run_is_ignored(
        self, repo: JobRepository, collector: MetricsCollector
    ):
        """Runs without completed_at must not contribute to duration stats."""
        job = make_job(repo, JobStatus.RUNNING)
        make_run(
            repo,
            job.id,
            started_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            completed_at=None,
            status=RunStatus.RUNNING,
        )
        metrics = collector.collect()
        assert metrics["duration"]["count"] == 0


# ---------------------------------------------------------------------------
# Retry stats
# ---------------------------------------------------------------------------


class TestRetryStats:
    def test_no_retries(self, repo: JobRepository, collector: MetricsCollector):
        for _ in range(3):
            make_job(repo, JobStatus.SUCCEEDED, attempt=1)
        metrics = collector.collect()
        assert metrics["retries"]["total_attempts"] == 3
        assert metrics["retries"]["avg_attempts"] == 1.0
        assert metrics["retries"]["jobs_with_retries"] == 0
        assert metrics["retries"]["retry_rate"] == 0.0

    def test_some_retries(self, repo: JobRepository, collector: MetricsCollector):
        make_job(repo, JobStatus.SUCCEEDED, attempt=1)
        make_job(repo, JobStatus.SUCCEEDED, attempt=2)
        make_job(repo, JobStatus.SUCCEEDED, attempt=3)
        metrics = collector.collect()
        assert metrics["retries"]["total_attempts"] == 6
        assert metrics["retries"]["jobs_with_retries"] == 2
        assert metrics["retries"]["retry_rate"] == pytest.approx(
            66.67, abs=0.01
        )

    def test_zero_jobs(self, repo: JobRepository, collector: MetricsCollector):
        metrics = collector.collect()
        assert metrics["retries"]["total_attempts"] == 0
        assert metrics["retries"]["avg_attempts"] == 0
        assert metrics["retries"]["retry_rate"] == 0.0


# ---------------------------------------------------------------------------
# Failure TOP N
# ---------------------------------------------------------------------------


class TestFailureTopN:
    def test_empty_when_no_failures(
        self, repo: JobRepository, collector: MetricsCollector
    ):
        make_job(repo, JobStatus.SUCCEEDED)
        metrics = collector.collect()
        assert metrics["failures"]["total_failures"] == 0
        assert metrics["failures"]["top_errors"] == []

    def test_groups_by_error_category(
        self, repo: JobRepository, collector: MetricsCollector
    ):
        for _ in range(3):
            make_job(
                repo,
                JobStatus.FAILED,
                error_category="timeout",
                last_error="connection timeout",
            )
        for _ in range(2):
            make_job(
                repo,
                JobStatus.FAILED,
                error_category="eval_failed",
                last_error="assertion failed",
            )
        metrics = collector.collect()
        assert metrics["failures"]["total_failures"] == 5
        top = metrics["failures"]["top_errors"]
        assert len(top) == 2
        assert top[0]["reason"] == "timeout"
        assert top[0]["count"] == 3
        assert top[1]["reason"] == "eval_failed"
        assert top[1]["count"] == 2

    def test_fallback_to_last_error_when_no_category(
        self, repo: JobRepository, collector: MetricsCollector
    ):
        """When error_category is empty, use truncated last_error as key."""
        make_job(
            repo,
            JobStatus.FAILED,
            error_category="",
            last_error="something went wrong here",
        )
        metrics = collector.collect()
        top = metrics["failures"]["top_errors"]
        assert len(top) == 1
        assert top[0]["reason"] == "something went wrong here"[:50]
        assert top[0]["count"] == 1

    def test_top_n_limit(self, repo: JobRepository, collector: MetricsCollector):
        """Only top N (default 5) errors are returned."""
        # Use valid error_category values and last_error for additional variety
        cats = ["timeout", "eval_failed", "tool_blocked", "unknown", "timeout", "eval_failed", "tool_blocked"]
        for i, cat in enumerate(cats):
            make_job(
                repo,
                JobStatus.FAILED,
                error_category=cat,
                last_error=f"error msg {i}",
            )
        metrics = collector.collect()
        assert len(metrics["failures"]["top_errors"]) <= 5

    def test_dead_letter_included(
        self, repo: JobRepository, collector: MetricsCollector
    ):
        make_job(
            repo,
            JobStatus.DEAD_LETTER,
            error_category="unknown",
            last_error="fatal",
        )
        metrics = collector.collect()
        assert metrics["failures"]["total_failures"] == 1
        assert metrics["failures"]["top_errors"][0]["reason"] == "unknown"


# ---------------------------------------------------------------------------
# Throughput
# ---------------------------------------------------------------------------


class TestThroughput:
    def test_basic(self, repo: JobRepository, collector: MetricsCollector):
        now = datetime.now(timezone.utc)
        for _ in range(5):
            make_job(repo, JobStatus.SUCCEEDED, updated_at=now)
        metrics = collector.collect()
        assert metrics["throughput"]["jobs_per_hour"] == 5.0
        assert metrics["throughput"]["peak_count"] == 5

    def test_empty(self, repo: JobRepository, collector: MetricsCollector):
        metrics = collector.collect()
        assert metrics["throughput"]["jobs_per_hour"] == 0
        assert metrics["throughput"]["peak_hour"] is None


# ---------------------------------------------------------------------------
# MetricsReporter
# ---------------------------------------------------------------------------


class TestMetricsReporterJSON:
    def test_generate_json_report(self, reporter: MetricsReporter):
        metrics = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "period": {"since": None, "until": None},
            "summary": {
                "total": 10,
                "succeeded": 8,
                "failed": 1,
                "canceled": 0,
                "dead_letter": 1,
                "success_rate": 80.0,
            },
            "duration": {
                "count": 10,
                "mean_sec": 5.0,
                "p50_sec": 4.0,
                "p95_sec": 12.0,
                "p99_sec": 15.0,
                "max_sec": 15.0,
            },
            "retries": {
                "total_attempts": 12,
                "avg_attempts": 1.2,
                "jobs_with_retries": 2,
                "retry_rate": 20.0,
            },
            "failures": {
                "total_failures": 2,
                "top_errors": [
                    {"reason": "timeout", "count": 1},
                    {"reason": "eval_failed", "count": 1},
                ],
            },
            "throughput": {
                "jobs_per_hour": 5.0,
                "peak_hour": "2024-01-01 12:00",
                "peak_count": 5,
            },
        }
        report = reporter.generate_json_report(metrics)
        parsed = json.loads(report)
        assert parsed["summary"]["total"] == 10
        assert parsed["summary"]["success_rate"] == 80.0

    def test_generate_json_report_with_file_output(
        self, reporter: MetricsReporter, tmp_path
    ):
        metrics = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "period": {"since": None, "until": None},
            "summary": {
                "total": 1,
                "succeeded": 1,
                "failed": 0,
                "canceled": 0,
                "dead_letter": 0,
                "success_rate": 100.0,
            },
            "duration": {
                "count": 0,
                "mean_sec": 0,
                "p50_sec": 0,
                "p95_sec": 0,
                "p99_sec": 0,
                "max_sec": 0,
            },
            "retries": {
                "total_attempts": 1,
                "avg_attempts": 1.0,
                "jobs_with_retries": 0,
                "retry_rate": 0.0,
            },
            "failures": {"total_failures": 0, "top_errors": []},
            "throughput": {
                "jobs_per_hour": 0,
                "peak_hour": None,
                "peak_count": 0,
            },
        }
        path = str(tmp_path / "report.json")
        reporter.generate_json_report(metrics, output_path=path)
        assert json.loads(open(path).read())["summary"]["total"] == 1


class TestMetricsReporterMarkdown:
    def test_generate_markdown_report(self, reporter: MetricsReporter):
        metrics = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "period": {"since": None, "until": None},
            "summary": {
                "total": 10,
                "succeeded": 8,
                "failed": 1,
                "canceled": 0,
                "dead_letter": 1,
                "success_rate": 80.0,
            },
            "duration": {
                "count": 10,
                "mean_sec": 5.0,
                "p50_sec": 4.0,
                "p95_sec": 12.0,
                "p99_sec": 15.0,
                "max_sec": 15.0,
            },
            "retries": {
                "total_attempts": 12,
                "avg_attempts": 1.2,
                "jobs_with_retries": 2,
                "retry_rate": 20.0,
            },
            "failures": {
                "total_failures": 2,
                "top_errors": [
                    {"reason": "timeout", "count": 1},
                    {"reason": "eval_failed", "count": 1},
                ],
            },
            "throughput": {
                "jobs_per_hour": 5.0,
                "peak_hour": "2024-01-01 12:00",
                "peak_count": 5,
            },
        }
        report = reporter.generate_markdown_report(metrics)
        assert "# Harness M1 指标报告" in report
        assert "10" in report  # total
        assert "80.0%" in report  # success rate
        assert "timeout" in report
        assert "eval_failed" in report

    def test_generate_markdown_report_empty_failures(
        self, reporter: MetricsReporter
    ):
        metrics = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "period": {"since": None, "until": None},
            "summary": {
                "total": 1,
                "succeeded": 1,
                "failed": 0,
                "canceled": 0,
                "dead_letter": 0,
                "success_rate": 100.0,
            },
            "duration": {
                "count": 0,
                "mean_sec": 0,
                "p50_sec": 0,
                "p95_sec": 0,
                "p99_sec": 0,
                "max_sec": 0,
            },
            "retries": {
                "total_attempts": 1,
                "avg_attempts": 1.0,
                "jobs_with_retries": 0,
                "retry_rate": 0.0,
            },
            "failures": {"total_failures": 0, "top_errors": []},
            "throughput": {
                "jobs_per_hour": 1.0,
                "peak_hour": "2024-01-01 00:00",
                "peak_count": 1,
            },
        }
        report = reporter.generate_markdown_report(metrics)
        assert "(无失败)" in report

    def test_markdown_file_output(self, reporter: MetricsReporter, tmp_path):
        metrics = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "period": {"since": None, "until": None},
            "summary": {
                "total": 1,
                "succeeded": 1,
                "failed": 0,
                "canceled": 0,
                "dead_letter": 0,
                "success_rate": 100.0,
            },
            "duration": {
                "count": 0,
                "mean_sec": 0,
                "p50_sec": 0,
                "p95_sec": 0,
                "p99_sec": 0,
                "max_sec": 0,
            },
            "retries": {
                "total_attempts": 1,
                "avg_attempts": 1.0,
                "jobs_with_retries": 0,
                "retry_rate": 0.0,
            },
            "failures": {"total_failures": 0, "top_errors": []},
            "throughput": {
                "jobs_per_hour": 0,
                "peak_hour": None,
                "peak_count": 0,
            },
        }
        path = str(tmp_path / "report.md")
        reporter.generate_markdown_report(metrics, output_path=path)
        content = open(path).read()
        assert "# Harness M1 指标报告" in content
