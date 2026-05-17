"""CLI job management commands — submit, status, list, cancel, worker, recover, console."""

from __future__ import annotations

import json
import os
import sys

from core.config import HarnessConfig
from session.store import SessionStore
from control_plane.models import JobStatus
from control_plane.worker import WorkerConfig, run_worker

from cli.utils import (
    _resolve_project_path,
    _write_error,
    _make_repository,
    _make_run_service,
)


async def cmd_submit(args):
    """Submit a new job to the control plane."""
    project = _resolve_project_path(
        args.project,
        allow_self_modify=getattr(args, "allow_self_modify", False),
    )

    repository = _make_repository()
    service = _make_run_service(repository)

    try:
        job = await service.submit_job(
            requirement=args.requirement,
            project_path=project,
            timeout=args.timeout,
            max_attempts=args.max_attempts,
        )
    except Exception as exc:
        _write_error("E_SUBMIT_FAILED", f"Failed to submit job: {exc}")
        return

    print(json.dumps({
        "job_id": job.id,
        "status": job.status.value,
        "message": "Job submitted",
    }))


async def cmd_status(args):
    """Get the status of a job including its runs."""
    repository = _make_repository()

    try:
        job = repository.get_job(args.job_id)
        if job is not None:
            runs = repository.list_runs_by_job(args.job_id)
            result = {
                "job_id": job.id,
                "status": job.status.value,
                "requirement": job.requirement,
                "project_path": job.project_path,
                "attempt": job.attempt,
                "last_error": job.last_error,
                "error_category": job.error_category,
                "created_at": str(job.created_at),
                "updated_at": str(job.updated_at),
                "runs": [
                    {
                        "run_id": r.id,
                        "status": r.status.value,
                        "session_id": r.session_id,
                        "started_at": str(r.started_at),
                        "completed_at": str(r.completed_at) if r.completed_at else None,
                    }
                    for r in runs
                ],
            }
            print(json.dumps(result, default=str))
            return

        config = HarnessConfig.from_env()
        store = SessionStore(config.event_store_path)
        if store.exists(args.job_id):
            summary = store.get_summary(args.job_id)
            print(json.dumps(summary, default=str))
            return

        _write_error("E_JOB_NOT_FOUND", f"Job not found: {args.job_id}")

    except Exception as exc:
        _write_error("E_STATUS_FAILED", f"Failed to get job status: {exc}")


async def cmd_list_jobs(args):
    """List jobs, optionally filtered by status."""
    repository = _make_repository()

    try:
        jobs = repository.list_jobs(status=JobStatus(args.status) if args.status else None)
    except Exception as exc:
        _write_error("E_LIST_FAILED", f"Failed to list jobs: {exc}")
        return

    output = []
    for job in jobs:
        output.append({
            "job_id": job.id,
            "status": job.status.value,
            "requirement": job.requirement,
            "created_at": str(job.created_at),
            "updated_at": str(job.updated_at),
            "attempt": job.attempt,
            "last_error": job.last_error,
        })

    print(json.dumps(output, default=str))


async def cmd_cancel(args):
    """Cancel a job."""
    repository = _make_repository()

    try:
        job = repository.transition_job_status(args.job_id, JobStatus.CANCELED)
    except ValueError as exc:
        _write_error("E_CANCEL_FAILED", str(exc))
        return
    except Exception as exc:
        _write_error("E_CANCEL_FAILED", f"Failed to cancel job: {exc}")
        return

    print(json.dumps({
        "job_id": job.id,
        "status": job.status.value,
        "message": "Job canceled",
    }))


async def cmd_worker(args):
    """Start a background worker that polls for and executes jobs."""
    repository = _make_repository()
    non_interactive = args.non_interactive or os.getenv(
        "HARNESS_NON_INTERACTIVE", ""
    ).lower() in ("true", "1", "yes")
    service = _make_run_service(repository, non_interactive=non_interactive)
    config = WorkerConfig(
        concurrency=args.concurrency,
        poll_interval_sec=args.poll_interval,
        non_interactive=non_interactive,
    )

    await run_worker(repository, service, config)


async def cmd_recover(args):
    """Manually trigger recovery of orphaned jobs."""
    repository = _make_repository()
    recovered = repository.recover_orphan_jobs()

    result = {
        "recovered_count": len(recovered),
        "recovered_jobs": [
            {"job_id": j.id, "old_status": "leased|running", "new_status": j.status.value}
            for j in recovered
        ],
        "message": f"Recovered {len(recovered)} orphan jobs",
    }
    print(json.dumps(result, indent=2, default=str))


async def cmd_console(args):
    """Launch the Web Console (FastAPI server)."""
    from visualizer.server import run_server
    print(f"Harness Console: http://{args.host}:{args.port}/console")
    print(f"Visualizer: http://{args.host}:{args.port}/")
    await run_server(host=args.host, port=args.port)
