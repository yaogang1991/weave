"""Tests for cli/jobs.py — submit, status, list, cancel, worker, recover, console.

Covers each command's happy path and error cases. Mocks _make_repository,
_make_run_service, _resolve_project_path, and _write_error to isolate CLI
logic from infrastructure.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_job(
    job_id="job_001",
    status="queued",
    requirement="Build a REST API",
    project_path="/tmp/project",
    attempt=0,
    last_error="",
    error_category="",
):
    """Build a Job-like object with sensible defaults."""
    from control_plane.models import Job, JobStatus
    now = datetime.now(timezone.utc)
    return Job(
        id=job_id,
        requirement=requirement,
        status=JobStatus(status),
        project_path=project_path,
        attempt=attempt,
        last_error=last_error,
        error_category=error_category,
        created_at=now,
        updated_at=now,
    )


def _make_run(
    run_id="run_001",
    job_id="job_001",
    session_id="sess_001",
    status="running",
    completed_at=None,
):
    """Build a Run-like object with sensible defaults."""
    from control_plane.models import Run, RunStatus
    now = datetime.now(timezone.utc)
    return Run(
        id=run_id,
        job_id=job_id,
        session_id=session_id,
        status=RunStatus(status),
        started_at=now,
        completed_at=completed_at,
        created_at=now,
        updated_at=now,
    )


# ===========================================================================
# cmd_submit
# ===========================================================================

class TestCmdSubmit:
    """Tests for cmd_submit."""

    @patch("cli.jobs._make_run_service")
    @patch("cli.jobs._make_repository")
    @patch("cli.jobs._resolve_project_path")
    def test_submit_happy_path(self, mock_resolve, mock_make_repo, mock_make_svc, capsys):
        """Submit prints JSON with job_id, status, and message."""
        from cli.jobs import cmd_submit

        mock_resolve.return_value = "/tmp/project"
        mock_repo = MagicMock()
        mock_make_repo.return_value = mock_repo
        mock_svc = MagicMock()
        mock_make_svc.return_value = mock_svc

        job = _make_job(job_id="job_abc", status="queued")
        mock_svc.submit_job = AsyncMock(return_value=job)

        args = argparse.Namespace(
            project="/tmp/project",
            requirement="Build a REST API",
            timeout=1800,
            max_attempts=3,
            allow_self_modify=False,
        )

        import asyncio
        asyncio.run(cmd_submit(args))

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["job_id"] == "job_abc"
        assert output["status"] == "queued"
        assert output["message"] == "Job submitted"
        mock_svc.submit_job.assert_awaited_once_with(
            requirement="Build a REST API",
            project_path="/tmp/project",
            timeout=1800,
            max_attempts=3,
        )

    @patch("cli.jobs._write_error")
    @patch("cli.jobs._make_run_service")
    @patch("cli.jobs._make_repository")
    @patch("cli.jobs._resolve_project_path")
    def test_submit_service_raises(self, mock_resolve, mock_make_repo, mock_make_svc, mock_write_err):
        """Submit calls _write_error when service.submit_job raises."""
        from cli.jobs import cmd_submit

        mock_resolve.return_value = "/tmp/project"
        mock_repo = MagicMock()
        mock_make_repo.return_value = mock_repo
        mock_svc = MagicMock()
        mock_make_svc.return_value = mock_svc
        mock_svc.submit_job = AsyncMock(side_effect=RuntimeError("DB down"))

        args = argparse.Namespace(
            project="/tmp/project",
            requirement="Build a REST API",
            timeout=1800,
            max_attempts=3,
            allow_self_modify=False,
        )

        import asyncio
        asyncio.run(cmd_submit(args))

        mock_write_err.assert_called_once()
        call_args = mock_write_err.call_args
        assert call_args[0][0] == "E_SUBMIT_FAILED"
        assert "DB down" in call_args[0][1]

    @patch("cli.jobs._make_run_service")
    @patch("cli.jobs._make_repository")
    @patch("cli.jobs._resolve_project_path")
    def test_submit_passes_allow_self_modify(self, mock_resolve, mock_make_repo, mock_make_svc, capsys):
        """Submit passes allow_self_modify flag to _resolve_project_path."""
        from cli.jobs import cmd_submit

        mock_resolve.return_value = "/opt/weave"
        mock_repo = MagicMock()
        mock_make_repo.return_value = mock_repo
        mock_svc = MagicMock()
        mock_make_svc.return_value = mock_svc

        job = _make_job()
        mock_svc.submit_job = AsyncMock(return_value=job)

        args = argparse.Namespace(
            project="/opt/weave",
            requirement="Self-modify task",
            timeout=600,
            max_attempts=1,
            allow_self_modify=True,
        )

        import asyncio
        asyncio.run(cmd_submit(args))

        mock_resolve.assert_called_once_with(
            "/opt/weave", allow_self_modify=True
        )

    @patch("cli.jobs._write_error")
    @patch("cli.jobs._make_run_service")
    @patch("cli.jobs._make_repository")
    @patch("cli.jobs._resolve_project_path")
    def test_submit_default_allow_self_modify_false(self, mock_resolve, mock_make_repo, mock_make_svc, capsys):
        """When allow_self_modify attr is missing from args, defaults to False."""
        from cli.jobs import cmd_submit

        mock_resolve.return_value = "/tmp/project"
        mock_repo = MagicMock()
        mock_make_repo.return_value = mock_repo
        mock_svc = MagicMock()
        mock_make_svc.return_value = mock_svc

        job = _make_job()
        mock_svc.submit_job = AsyncMock(return_value=job)

        # Deliberately omit allow_self_modify
        args = argparse.Namespace(
            project="/tmp/project",
            requirement="Task",
            timeout=1800,
            max_attempts=3,
        )

        import asyncio
        asyncio.run(cmd_submit(args))

        mock_resolve.assert_called_once_with(
            "/tmp/project", allow_self_modify=False
        )


# ===========================================================================
# cmd_status
# ===========================================================================

class TestCmdStatus:
    """Tests for cmd_status."""

    @patch("cli.jobs._make_repository")
    def test_status_job_found(self, mock_make_repo, capsys):
        """Status prints JSON with job details and runs when job exists."""
        from cli.jobs import cmd_status

        mock_repo = MagicMock()
        mock_make_repo.return_value = mock_repo

        job = _make_job(job_id="job_xyz", status="running")
        run = _make_run(run_id="run_1", job_id="job_xyz", session_id="sess_1")
        mock_repo.get_job.return_value = job
        mock_repo.list_runs_by_job.return_value = [run]

        args = argparse.Namespace(job_id="job_xyz")

        import asyncio
        asyncio.run(cmd_status(args))

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["job_id"] == "job_xyz"
        assert output["status"] == "running"
        assert output["requirement"] == "Build a REST API"
        assert output["project_path"] == "/tmp/project"
        assert output["attempt"] == 0
        assert output["last_error"] == ""
        assert output["error_category"] == ""
        assert len(output["runs"]) == 1
        assert output["runs"][0]["run_id"] == "run_1"

    @patch("cli.jobs._make_repository")
    def test_status_job_found_no_runs(self, mock_make_repo, capsys):
        """Status prints empty runs list when job has no runs."""
        from cli.jobs import cmd_status

        mock_repo = MagicMock()
        mock_make_repo.return_value = mock_repo

        job = _make_job(job_id="job_empty")
        mock_repo.get_job.return_value = job
        mock_repo.list_runs_by_job.return_value = []

        args = argparse.Namespace(job_id="job_empty")

        import asyncio
        asyncio.run(cmd_status(args))

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["runs"] == []

    @patch("cli.jobs._write_error")
    @patch("cli.jobs._make_repository")
    def test_status_job_not_found_no_session(self, mock_make_repo, mock_write_err):
        """Status calls _write_error when job not found and no session exists."""
        from cli.jobs import cmd_status

        mock_repo = MagicMock()
        mock_make_repo.return_value = mock_repo
        mock_repo.get_job.return_value = None

        mock_session_store = MagicMock()
        mock_session_store.exists.return_value = False

        args = argparse.Namespace(job_id="nonexistent")

        with patch("cli.jobs.SessionStore", return_value=mock_session_store), \
             patch("cli.jobs.WeaveConfig") as MockConfig:
            MockConfig.from_env.return_value = MagicMock(event_store_path="/tmp/events")

            import asyncio
            asyncio.run(cmd_status(args))

        mock_write_err.assert_called_once()
        call_args = mock_write_err.call_args
        assert call_args[0][0] == "E_JOB_NOT_FOUND"
        assert "nonexistent" in call_args[0][1]

    @patch("cli.jobs._make_repository")
    def test_status_falls_back_to_session(self, mock_make_repo, capsys):
        """Status falls back to session store when job not in repository."""
        from cli.jobs import cmd_status

        mock_repo = MagicMock()
        mock_make_repo.return_value = mock_repo
        mock_repo.get_job.return_value = None

        mock_session_store = MagicMock()
        mock_session_store.exists.return_value = True
        mock_session_store.get_summary.return_value = {
            "session_id": "sess_123",
            "status": "completed",
        }

        args = argparse.Namespace(job_id="sess_123")

        with patch("cli.jobs.SessionStore", return_value=mock_session_store), \
             patch("cli.jobs.WeaveConfig") as MockConfig:
            MockConfig.from_env.return_value = MagicMock(event_store_path="/tmp/events")

            import asyncio
            asyncio.run(cmd_status(args))

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["session_id"] == "sess_123"
        assert output["status"] == "completed"

    @patch("cli.jobs._write_error")
    @patch("cli.jobs._make_repository")
    def test_status_exception_handling(self, mock_make_repo, mock_write_err):
        """Status catches unexpected exceptions and calls _write_error."""
        from cli.jobs import cmd_status

        mock_repo = MagicMock()
        mock_make_repo.return_value = mock_repo
        mock_repo.get_job.side_effect = RuntimeError("disk error")

        args = argparse.Namespace(job_id="job_x")

        import asyncio
        asyncio.run(cmd_status(args))

        mock_write_err.assert_called_once()
        call_args = mock_write_err.call_args
        assert call_args[0][0] == "E_STATUS_FAILED"
        assert "disk error" in call_args[0][1]

    @patch("cli.jobs._make_repository")
    def test_status_run_with_completed_at(self, mock_make_repo, capsys):
        """Status serializes completed_at for finished runs."""
        from cli.jobs import cmd_status

        mock_repo = MagicMock()
        mock_make_repo.return_value = mock_repo

        now = datetime.now(timezone.utc)
        job = _make_job(job_id="job_done", status="succeeded")
        run = _make_run(
            run_id="run_done",
            job_id="job_done",
            status="succeeded",
            completed_at=now,
        )
        mock_repo.get_job.return_value = job
        mock_repo.list_runs_by_job.return_value = [run]

        args = argparse.Namespace(job_id="job_done")

        import asyncio
        asyncio.run(cmd_status(args))

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["runs"][0]["completed_at"] is not None


# ===========================================================================
# cmd_list_jobs
# ===========================================================================

class TestCmdListJobs:
    """Tests for cmd_list_jobs."""

    @patch("cli.jobs._make_repository")
    def test_list_all_jobs(self, mock_make_repo, capsys):
        """List all jobs when no status filter provided."""
        from cli.jobs import cmd_list_jobs

        mock_repo = MagicMock()
        mock_make_repo.return_value = mock_repo

        job1 = _make_job(job_id="job_a", status="queued")
        job2 = _make_job(job_id="job_b", status="running", requirement="Fix bug")
        mock_repo.list_jobs.return_value = [job1, job2]

        args = argparse.Namespace(status=None)

        import asyncio
        asyncio.run(cmd_list_jobs(args))

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert len(output) == 2
        assert output[0]["job_id"] == "job_a"
        assert output[0]["status"] == "queued"
        assert output[1]["job_id"] == "job_b"
        assert output[1]["status"] == "running"
        mock_repo.list_jobs.assert_called_once_with(status=None)

    @patch("cli.jobs._make_repository")
    def test_list_with_status_filter(self, mock_make_repo, capsys):
        """List jobs filtered by status string."""
        from control_plane.models import JobStatus
        from cli.jobs import cmd_list_jobs

        mock_repo = MagicMock()
        mock_make_repo.return_value = mock_repo

        job = _make_job(job_id="job_q", status="queued")
        mock_repo.list_jobs.return_value = [job]

        args = argparse.Namespace(status="queued")

        import asyncio
        asyncio.run(cmd_list_jobs(args))

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert len(output) == 1
        assert output[0]["status"] == "queued"
        # Verify the filter was constructed as JobStatus
        mock_repo.list_jobs.assert_called_once_with(status=JobStatus.QUEUED)

    @patch("cli.jobs._write_error")
    @patch("cli.jobs._make_repository")
    def test_list_invalid_status_filter(self, mock_make_repo, mock_write_err):
        """List calls _write_error for invalid status strings."""
        from cli.jobs import cmd_list_jobs

        mock_repo = MagicMock()
        mock_make_repo.return_value = mock_repo

        args = argparse.Namespace(status="invalid_status")

        import asyncio
        asyncio.run(cmd_list_jobs(args))

        mock_write_err.assert_called_once()
        call_args = mock_write_err.call_args
        assert call_args[0][0] == "E_LIST_FAILED"

    @patch("cli.jobs._make_repository")
    def test_list_empty_result(self, mock_make_repo, capsys):
        """List returns empty JSON array when no jobs."""
        from cli.jobs import cmd_list_jobs

        mock_repo = MagicMock()
        mock_make_repo.return_value = mock_repo
        mock_repo.list_jobs.return_value = []

        args = argparse.Namespace(status=None)

        import asyncio
        asyncio.run(cmd_list_jobs(args))

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output == []

    @patch("cli.jobs._write_error")
    @patch("cli.jobs._make_repository")
    def test_list_repository_exception(self, mock_make_repo, mock_write_err):
        """List calls _write_error when repository raises."""
        from cli.jobs import cmd_list_jobs

        mock_repo = MagicMock()
        mock_make_repo.return_value = mock_repo
        mock_repo.list_jobs.side_effect =OSError("IO failure")

        args = argparse.Namespace(status=None)

        import asyncio
        asyncio.run(cmd_list_jobs(args))

        mock_write_err.assert_called_once()
        call_args = mock_write_err.call_args
        assert call_args[0][0] == "E_LIST_FAILED"
        assert "IO failure" in call_args[0][1]

    @patch("cli.jobs._make_repository")
    def test_list_output_includes_required_fields(self, mock_make_repo, capsys):
        """Each job in list output has job_id, status, requirement, created_at, updated_at, attempt, last_error."""
        from cli.jobs import cmd_list_jobs

        mock_repo = MagicMock()
        mock_make_repo.return_value = mock_repo

        job = _make_job(
            job_id="job_full",
            status="failed",
            last_error="timeout",
            attempt=2,
        )
        mock_repo.list_jobs.return_value = [job]

        args = argparse.Namespace(status=None)

        import asyncio
        asyncio.run(cmd_list_jobs(args))

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        entry = output[0]
        required_keys = {"job_id", "status", "requirement", "created_at", "updated_at", "attempt", "last_error"}
        assert required_keys.issubset(set(entry.keys()))
        assert entry["last_error"] == "timeout"
        assert entry["attempt"] == 2


# ===========================================================================
# cmd_cancel
# ===========================================================================

class TestCmdCancel:
    """Tests for cmd_cancel."""

    @patch("cli.jobs._make_repository")
    def test_cancel_happy_path(self, mock_make_repo, capsys):
        """Cancel prints JSON confirming the job was canceled."""
        from control_plane.models import JobStatus
        from cli.jobs import cmd_cancel

        mock_repo = MagicMock()
        mock_make_repo.return_value = mock_repo

        job = _make_job(job_id="job_cancel", status="canceled")
        mock_repo.transition_job_status.return_value = job

        args = argparse.Namespace(job_id="job_cancel")

        import asyncio
        asyncio.run(cmd_cancel(args))

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["job_id"] == "job_cancel"
        assert output["status"] == "canceled"
        assert output["message"] == "Job canceled"
        mock_repo.transition_job_status.assert_called_once_with(
            "job_cancel", JobStatus.CANCELED
        )

    @patch("cli.jobs._write_error")
    @patch("cli.jobs._make_repository")
    def test_cancel_value_error(self, mock_make_repo, mock_write_err):
        """Cancel calls _write_error with ValueError message."""
        from cli.jobs import cmd_cancel

        mock_repo = MagicMock()
        mock_make_repo.return_value = mock_repo
        mock_repo.transition_job_status.side_effect = ValueError(
            "Invalid transition: succeeded -> canceled"
        )

        args = argparse.Namespace(job_id="job_succeeded")

        import asyncio
        asyncio.run(cmd_cancel(args))

        mock_write_err.assert_called_once()
        call_args = mock_write_err.call_args
        assert call_args[0][0] == "E_CANCEL_FAILED"
        assert "Invalid transition" in call_args[0][1]

    @patch("cli.jobs._write_error")
    @patch("cli.jobs._make_repository")
    def test_cancel_generic_exception(self, mock_make_repo, mock_write_err):
        """Cancel calls _write_error for non-ValueError exceptions."""
        from cli.jobs import cmd_cancel

        mock_repo = MagicMock()
        mock_make_repo.return_value = mock_repo
        mock_repo.transition_job_status.side_effect = RuntimeError("connection lost")

        args = argparse.Namespace(job_id="job_x")

        import asyncio
        asyncio.run(cmd_cancel(args))

        mock_write_err.assert_called_once()
        call_args = mock_write_err.call_args
        assert call_args[0][0] == "E_CANCEL_FAILED"
        assert "connection lost" in call_args[0][1]


# ===========================================================================
# cmd_worker
# ===========================================================================

class TestCmdWorker:
    """Tests for cmd_worker."""

    @patch("cli.jobs.run_worker", new_callable=AsyncMock)
    @patch("cli.jobs._get_non_interactive_env", return_value="false")
    @patch("cli.jobs._make_run_service")
    @patch("cli.jobs._make_repository")
    def test_worker_default_settings(self, mock_make_repo, mock_make_svc, mock_env, mock_run_worker):
        """Worker starts with default concurrency and poll interval."""
        from cli.jobs import cmd_worker, WorkerConfig

        mock_repo = MagicMock()
        mock_make_repo.return_value = mock_repo
        mock_svc = MagicMock()
        mock_make_svc.return_value = mock_svc

        args = argparse.Namespace(
            concurrency=1,
            poll_interval=5,
            non_interactive=False,
        )

        import asyncio
        asyncio.run(cmd_worker(args))

        mock_run_worker.assert_awaited_once()
        call_args = mock_run_worker.call_args
        config = call_args[0][2]
        assert isinstance(config, WorkerConfig)
        assert config.concurrency == 1
        assert config.poll_interval_sec == 5

    @patch("cli.jobs.run_worker", new_callable=AsyncMock)
    @patch("cli.jobs._get_non_interactive_env", return_value="false")
    @patch("cli.jobs._make_run_service")
    @patch("cli.jobs._make_repository")
    def test_worker_non_interactive_from_args(self, mock_make_repo, mock_make_svc, mock_env, mock_run_worker):
        """Worker passes non_interactive=True when --non-interactive flag set."""
        from cli.jobs import cmd_worker

        mock_repo = MagicMock()
        mock_make_repo.return_value = mock_repo
        mock_svc = MagicMock()
        mock_make_svc.return_value = mock_svc

        args = argparse.Namespace(
            concurrency=2,
            poll_interval=10,
            non_interactive=True,
        )

        import asyncio
        asyncio.run(cmd_worker(args))

        # _make_run_service should be called with non_interactive=True
        mock_make_svc.assert_called_once_with(mock_repo, non_interactive=True)

    @patch("cli.jobs.run_worker", new_callable=AsyncMock)
    @patch("cli.jobs._get_non_interactive_env", return_value="true")
    @patch("cli.jobs._make_run_service")
    @patch("cli.jobs._make_repository")
    def test_worker_non_interactive_from_env(self, mock_make_repo, mock_make_svc, mock_env, mock_run_worker):
        """Worker reads non_interactive from WEAVE_NON_INTERACTIVE env var."""
        from cli.jobs import cmd_worker

        mock_repo = MagicMock()
        mock_make_repo.return_value = mock_repo
        mock_svc = MagicMock()
        mock_make_svc.return_value = mock_svc

        args = argparse.Namespace(
            concurrency=1,
            poll_interval=5,
            non_interactive=False,
        )

        import asyncio
        asyncio.run(cmd_worker(args))

        # Env var "true" should make it non_interactive=True
        mock_make_svc.assert_called_once_with(mock_repo, non_interactive=True)


# ===========================================================================
# cmd_recover
# ===========================================================================

class TestCmdRecover:
    """Tests for cmd_recover."""

    @patch("cli.jobs._make_repository")
    def test_recover_with_orphans(self, mock_make_repo, capsys):
        """Recover prints JSON with recovered job details."""
        from control_plane.models import JobStatus
        from cli.jobs import cmd_recover

        mock_repo = MagicMock()
        mock_make_repo.return_value = mock_repo

        recovered_job = _make_job(job_id="job_orphan", status="queued")
        mock_repo.recover_orphan_jobs.return_value = [recovered_job]

        args = argparse.Namespace()

        import asyncio
        asyncio.run(cmd_recover(args))

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["recovered_count"] == 1
        assert len(output["recovered_jobs"]) == 1
        assert output["recovered_jobs"][0]["job_id"] == "job_orphan"
        assert output["recovered_jobs"][0]["old_status"] == "leased|running"
        assert output["recovered_jobs"][0]["new_status"] == "queued"
        assert "Recovered 1 orphan jobs" in output["message"]

    @patch("cli.jobs._make_repository")
    def test_recover_no_orphans(self, mock_make_repo, capsys):
        """Recover prints zero count when no orphaned jobs found."""
        from cli.jobs import cmd_recover

        mock_repo = MagicMock()
        mock_make_repo.return_value = mock_repo
        mock_repo.recover_orphan_jobs.return_value = []

        args = argparse.Namespace()

        import asyncio
        asyncio.run(cmd_recover(args))

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["recovered_count"] == 0
        assert output["recovered_jobs"] == []
        assert "Recovered 0 orphan jobs" in output["message"]

    @patch("cli.jobs._make_repository")
    def test_recover_multiple_orphans(self, mock_make_repo, capsys):
        """Recover handles multiple orphaned jobs."""
        from cli.jobs import cmd_recover

        mock_repo = MagicMock()
        mock_make_repo.return_value = mock_repo

        jobs = [
            _make_job(job_id="job_a", status="queued"),
            _make_job(job_id="job_b", status="queued"),
            _make_job(job_id="job_c", status="queued"),
        ]
        mock_repo.recover_orphan_jobs.return_value = jobs

        args = argparse.Namespace()

        import asyncio
        asyncio.run(cmd_recover(args))

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["recovered_count"] == 3
        assert len(output["recovered_jobs"]) == 3


# ===========================================================================
# cmd_console
# ===========================================================================

class TestCmdConsole:
    """Tests for cmd_console."""

    @patch("weave_ui.server.run_server", new_callable=AsyncMock)
    def test_console_default_host_port(self, mock_run_server, capsys):
        """Console launches with default host and port."""
        from cli.jobs import cmd_console

        args = argparse.Namespace(host="0.0.0.0", port=8080)

        import asyncio
        asyncio.run(cmd_console(args))

        captured = capsys.readouterr()
        assert "http://0.0.0.0:8080/console" in captured.out
        assert "http://0.0.0.0:8080/" in captured.out
        mock_run_server.assert_awaited_once_with(host="0.0.0.0", port=8080)

    @patch("weave_ui.server.run_server", new_callable=AsyncMock)
    def test_console_custom_host_port(self, mock_run_server, capsys):
        """Console launches with custom host and port."""
        from cli.jobs import cmd_console

        args = argparse.Namespace(host="127.0.0.1", port=3000)

        import asyncio
        asyncio.run(cmd_console(args))

        captured = capsys.readouterr()
        assert "http://127.0.0.1:3000/console" in captured.out
        mock_run_server.assert_awaited_once_with(host="127.0.0.1", port=3000)


# ===========================================================================
# Status filter parsing edge cases
# ===========================================================================

class TestStatusFilterParsing:
    """Verify that JobStatus enum parsing works for all valid status values."""

    @patch("cli.jobs._make_repository")
    def test_all_valid_status_filters(self, mock_make_repo, capsys):
        """Every JobStatus enum value should be accepted as a filter."""
        from control_plane.models import JobStatus
        from cli.jobs import cmd_list_jobs

        for status in JobStatus:
            mock_repo = MagicMock()
            mock_make_repo.return_value = mock_repo
            mock_repo.list_jobs.return_value = []

            args = argparse.Namespace(status=status.value)

            import asyncio
            asyncio.run(cmd_list_jobs(args))

            mock_repo.list_jobs.assert_called_with(status=status)
