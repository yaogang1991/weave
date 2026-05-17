"""
BackendLifecycleService: manages workspace setup, lifecycle hooks, and cleanup.

Extracted from RunService.run_job() as part of #177 PR 2.
Encapsulates BackendManager creation, project hook loading/execution,
and workspace preserve/cleanup behavior.
"""
from __future__ import annotations

import logging
from pathlib import Path

from backend.lifecycle import BackendManager
from backend.base import WorkspaceIsolation, ExecutionSandbox
from core.config import WeaveConfig

logger = logging.getLogger(__name__)


class BackendLifecycleService:
    """Manages the full backend lifecycle for a job run.

    Responsibilities:
    - resolve effective backend config
    - setup workspace via BackendManager
    - run after_create / before_run / after_run / before_remove hooks
    - cleanup / preserve workspace
    - ensure backend failures do not mask primary execution errors
    """

    def __init__(
        self,
        backend_base_path: str = "./data/backend",
    ):
        self._base_path = backend_base_path

    def create_backend_manager(
        self,
        project_path: str,
    ) -> BackendManager:
        """Create a BackendManager configured from environment."""
        weave_config = WeaveConfig.from_env()
        sandbox_type = ExecutionSandbox(
            weave_config.sandbox.runtime
            if weave_config.sandbox.runtime in ("local", "docker")
            else "local"
        )
        return BackendManager(
            workspace=WorkspaceIsolation(weave_config.default_backend),
            sandbox=sandbox_type,
            repo_root=str(Path(project_path).resolve()),
            base_path=self._base_path,
            workspace_by_risk=weave_config.risk_backend_map,
            cleanup_policy=weave_config.cleanup_policy,
        )

    def setup_workspace(
        self,
        backend_manager: BackendManager,
        job_id: str,
        run_id: str,
        risk_level: str | None = None,
    ) -> str:
        """Set up workspace and return work_dir path."""
        return backend_manager.setup(
            job_id=job_id,
            run_id=run_id,
            risk_level=risk_level,
        )

    async def run_hook(
        self,
        backend_manager: BackendManager,
        hook_name: str,
        hooks: dict[str, str],
        work_dir: str,
    ) -> None:
        """Execute a lifecycle hook if configured.

        Lifecycle hooks (after_create, before_run, after_run) propagate
        exceptions to the caller so that the job failure flow handles them
        consistently with the original RunService behavior.
        """
        command = hooks.get(hook_name)
        if not command:
            return
        await backend_manager.execute_hook(hook_name, command, work_dir)

    def preserve(
        self,
        backend_manager: BackendManager,
        job_id: str,
        run_id: str,
        reason: str = "",
    ) -> None:
        """Preserve workspace artifacts. Backend errors are swallowed."""
        try:
            backend_manager.preserve(job_id, run_id, reason=reason)
        except Exception:
            pass  # Backend cleanup failure must not mask original error

    def cleanup(
        self,
        backend_manager: BackendManager,
        job_id: str,
        run_id: str,
    ) -> None:
        """Clean up workspace. Backend errors are swallowed."""
        try:
            backend_manager.cleanup(job_id, run_id)
        except Exception:
            pass  # Backend cleanup failure must not mask original error

    @staticmethod
    def load_project_hooks(project_path: str | None) -> dict[str, str]:
        """Load lifecycle hooks from .weave/config.yaml if present."""
        hooks: dict[str, str] = {}
        if not project_path:
            return hooks
        try:
            config_path = Path(project_path) / ".weave" / "config.yaml"
            if config_path.exists():
                import yaml
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                hook_cfg = cfg.get("hooks", {})
                for key in ("after_create", "before_run", "after_run", "before_remove"):
                    if key in hook_cfg:
                        hooks[key] = hook_cfg[key]
        except Exception:
            pass
        return hooks
