"""
JobResultWriter — extracted from RunService (#177 PR 1).

Generates standardized job_result.json artifacts from Job/Run/summary data.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from control_plane.models import Job, Run

logger = logging.getLogger(__name__)


class JobResultWriter:
    """Generates and writes job_result.json artifacts."""

    def __init__(self, artifact_path: str = "./data/artifacts") -> None:
        self.artifact_path = artifact_path

    def generate(
        self,
        job: Job,
        run: Run,
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        """Generate a standardized job_result.json artifact.

        Returns the result dict (also writes to disk).
        """
        result: dict[str, Any] = {
            "job": {
                "id": job.id,
                "requirement": job.requirement,
                "project_path": job.project_path,
                "attempt": job.attempt,
            },
            "run": {
                "id": run.id,
                "session_id": run.session_id,
                "status": run.status.value,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            },
            "dag": summary,
            "approvals": [],
            "artifacts": [],
            "errors": [],
            "timestamps": {
                "created_at": job.created_at.isoformat(),
                "updated_at": job.updated_at.isoformat(),
            },
        }

        if job.last_error:
            result["errors"].append({
                "message": job.last_error,
                "category": job.error_category,
            })

        # Write to artifact path
        artifact_dir = Path(self.artifact_path) / job.id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        result_path = artifact_dir / "job_result.json"
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, default=str, ensure_ascii=False)

        return result
