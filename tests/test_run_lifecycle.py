"""
Tests for #177 PR 1: RunLifecycleManager and JobResultWriter.

Verifies that extracted lifecycle methods and result generation behave
identically to the inline code they replace.
"""
import json
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from control_plane.models import Job, JobStatus, Run, RunStatus
from control_plane.repository import JobRepository
from control_plane.run_lifecycle import RunLifecycleManager
from control_plane.job_result import JobResultWriter


def _make_run(**overrides) -> Run:
    defaults = dict(
        id="run-1",
        job_id="job-1",
        session_id="sess-1",
        status=RunStatus.RUNNING,
        dag_result={},
        started_at=datetime.now(timezone.utc),
        completed_at=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return Run(**defaults)


def _make_job(**overrides) -> Job:
    defaults = dict(
        id="job-1",
        requirement="test",
        project_path="/tmp/test",
        status=JobStatus.RUNNING,
        attempt=1,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return Job(**defaults)


class TestRunLifecycleManager:
    @pytest.fixture
    def repo(self):
        repo = MagicMock(spec=JobRepository)
        repo.update_run = MagicMock()
        return repo

    @pytest.fixture
    def lifecycle(self, repo):
        return RunLifecycleManager(repo)

    def test_mark_succeeded(self, lifecycle, repo):
        run = _make_run()
        result = lifecycle.mark_succeeded(run, {"all_succeeded": True})
        assert result.status == RunStatus.SUCCEEDED
        assert result.completed_at is not None
        assert result.dag_result == {"all_succeeded": True}
        repo.update_run.assert_called_once_with(run)

    def test_mark_failed(self, lifecycle, repo):
        run = _make_run()
        result = lifecycle.mark_failed(run, {"error": "tests failed"})
        assert result.status == RunStatus.FAILED
        assert result.completed_at is not None
        assert result.dag_result == {"error": "tests failed"}
        repo.update_run.assert_called_once()

    def test_mark_failed_preserves_existing_dag_result(self, lifecycle, repo):
        run = _make_run(dag_result={"nodes": {"n1": "ok"}})
        result = lifecycle.mark_failed(run)  # no dag_result override
        assert result.status == RunStatus.FAILED
        assert result.dag_result == {"nodes": {"n1": "ok"}}

    def test_mark_timed_out(self, lifecycle, repo):
        run = _make_run()
        result = lifecycle.mark_timed_out(run, 300)
        assert result.status == RunStatus.TIMED_OUT
        assert result.completed_at is not None
        assert "300" in result.dag_result["reason"]

    def test_mark_canceled(self, lifecycle, repo):
        run = _make_run()
        result = lifecycle.mark_canceled(run, "user request")
        assert result.status == RunStatus.ABORTED
        assert result.completed_at is not None
        assert result.dag_result["reason"] == "user request"

    def test_mark_pending_approval(self, lifecycle, repo):
        run = _make_run()
        result = lifecycle.mark_pending_approval(run, "ticket-42")
        assert result.status == RunStatus.PENDING_APPROVAL
        assert result.dag_result["ticket_id"] == "ticket-42"
        assert result.completed_at is None  # not terminal

    def test_resolve_external_status_running(self, lifecycle, repo):
        run = _make_run()
        job = _make_job(status=JobStatus.RUNNING)
        result = lifecycle.resolve_external_status(run, job)
        assert result is None  # no change

    def test_resolve_external_status_canceled(self, lifecycle, repo):
        run = _make_run()
        job = _make_job(status=JobStatus.CANCELED)
        result = lifecycle.resolve_external_status(run, job)
        assert result.status == RunStatus.ABORTED

    def test_resolve_external_status_requeued(self, lifecycle, repo):
        run = _make_run()
        job = _make_job(status=JobStatus.QUEUED)
        result = lifecycle.resolve_external_status(run, job)
        assert result.status == RunStatus.FAILED


class TestJobResultWriter:
    def test_generate_basic(self, tmp_path):
        writer = JobResultWriter(artifact_path=str(tmp_path))
        run = _make_run()
        job = _make_job()
        result = writer.generate(job, run, {"all_succeeded": True})

        assert result["job"]["id"] == "job-1"
        assert result["run"]["id"] == "run-1"
        assert result["dag"] == {"all_succeeded": True}
        assert result["errors"] == []

    def test_generate_with_error(self, tmp_path):
        writer = JobResultWriter(artifact_path=str(tmp_path))
        run = _make_run()
        job = _make_job(last_error="timeout exceeded", error_category="timeout")
        result = writer.generate(job, run, {})

        assert len(result["errors"]) == 1
        assert result["errors"][0]["message"] == "timeout exceeded"

    def test_generate_writes_file(self, tmp_path):
        writer = JobResultWriter(artifact_path=str(tmp_path))
        run = _make_run()
        job = _make_job()
        writer.generate(job, run, {})

        result_file = tmp_path / "job-1" / "job_result.json"
        assert result_file.exists()
        data = json.loads(result_file.read_text(encoding="utf-8"))
        assert data["job"]["id"] == "job-1"

    def test_generate_no_error_field(self, tmp_path):
        writer = JobResultWriter(artifact_path=str(tmp_path))
        run = _make_run()
        job = _make_job()  # no last_error
        result = writer.generate(job, run, {})
        assert result["errors"] == []
