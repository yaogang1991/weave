"""
Backend lifecycle manager.

Manages creation, execution, cleanup, and preservation of execution backends.
Supports two orthogonal dimensions:
- Workspace isolation (LOCAL / WORKTREE) — how files are managed
- Execution sandbox (LOCAL / DOCKER) — where processes run

Risk-based sandbox selection (#484): when risk_level is provided during
setup(), the manager can override the default sandbox. High/critical risk
operations use DockerSandbox (if available), falling back to LocalSandbox
with resource limits when Docker is not installed.

Hooks are executed as subprocesses on the host (not through SandboxProvider),
matching Symphony's design — hooks are operational scripts that need host access.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from backend.base import ExecutionBackend, WorkspaceIsolation, ExecutionSandbox
from backend.local import LocalBackend
from backend.worktree import WorktreeBackend
from backend.sandbox import SandboxProvider, LocalSandbox, DockerSandbox
from core.models import NodeWorkspace

logger = logging.getLogger(__name__)


@dataclass
class HookResult:
    """Result of a hook execution."""
    success: bool
    output: str = ""
    error: str = ""
    duration_ms: int = 0


class HookError(Exception):
    """Raised when a critical hook (after_create, before_run) fails."""

    def __init__(self, hook_name: str, output: str = "", error: str = ""):
        self.hook_name = hook_name
        self.output = output
        self.error = error
        super().__init__(f"Hook '{hook_name}' failed: {error or output}")


class BackendManager:
    """
    Backend lifecycle manager — composes workspace isolation + execution sandbox.

    Usage:
        manager = BackendManager(
            workspace=WorkspaceIsolation.WORKTREE,
            sandbox=ExecutionSandbox.LOCAL,
            repo_root="/path/to/repo",
        )
        work_dir = manager.setup(job_id, run_id)
        # ... execute in work_dir ...
        manager.cleanup(job_id, run_id)  # on success
        # or
        manager.preserve(job_id, run_id, reason="failed")  # on failure
    """

    def __init__(
        self,
        workspace: WorkspaceIsolation | str = WorkspaceIsolation.LOCAL,
        sandbox: ExecutionSandbox | str = ExecutionSandbox.LOCAL,
        repo_root: str | None = None,
        base_path: str = "./data/backends",
        workspace_by_risk: dict[str, str] | None = None,
        sandbox_by_risk: dict[str, str] | None = None,
        cleanup_policy: str = "on_success",
        sandbox_config: dict | None = None,
    ):
        self.workspace_type = WorkspaceIsolation(workspace)
        self.sandbox_type = ExecutionSandbox(sandbox)
        self.repo_root = repo_root
        self.base_path = base_path
        self.workspace_by_risk = workspace_by_risk or {
            "low": "local",
            "medium": "local",
            "high": "worktree",
            "critical": "worktree",
        }
        self.sandbox_by_risk = sandbox_by_risk or {
            "low": "local",
            "medium": "local",
            "high": "docker",
            "critical": "docker",
        }
        self.cleanup_policy = cleanup_policy
        self._sandbox_config = sandbox_config or {}
        _VALID_POLICIES = ("always", "on_success", "never")
        if self.cleanup_policy not in _VALID_POLICIES:
            raise ValueError(
                f"Invalid cleanup_policy '{self.cleanup_policy}', "
                f"must be one of {_VALID_POLICIES}"
            )
        self._backends: dict[str, ExecutionBackend] = {}
        self._sandbox_provider = self._create_sandbox()
        self._active_runs: dict[str, ExecutionBackend] = {}  # run_id -> backend
        self._active_sandboxes: dict[str, SandboxProvider] = {}  # run_id -> sandbox (#484)
        self._node_workspaces: dict[str, "NodeWorkspace"] = {}  # "run_id:node_id" -> NodeWorkspace

    def _get_workspace_backend(
        self, ws_type: WorkspaceIsolation
    ) -> ExecutionBackend:
        """Get or create workspace backend instance."""
        key = ws_type.value
        if key not in self._backends:
            if ws_type == WorkspaceIsolation.LOCAL:
                self._backends[key] = LocalBackend(
                    self.repo_root, self.base_path
                )
            elif ws_type == WorkspaceIsolation.WORKTREE:
                self._backends[key] = WorktreeBackend(
                    self.repo_root, self.base_path
                )
            else:
                raise ValueError(f"Unknown workspace type: {ws_type}")
        return self._backends[key]

    def _create_sandbox(self) -> SandboxProvider:
        """Create sandbox provider based on configuration."""
        if self.sandbox_type == ExecutionSandbox.LOCAL:
            return LocalSandbox()
        elif self.sandbox_type == ExecutionSandbox.DOCKER:
            return DockerSandbox(
                image=self._sandbox_config.get("image", "python:3.12-slim"),
                network_mode=self._sandbox_config.get("network_mode", "none"),
                memory_limit=self._sandbox_config.get("memory_limit", "512m"),
                cpu_limit=self._sandbox_config.get("cpu_limit", 1.0),
            )
        else:
            raise ValueError(f"Unknown sandbox type: {self.sandbox_type}")

    def _select_sandbox(self, risk_level: str | None = None) -> SandboxProvider:
        """Select sandbox provider based on risk level (#484).

        High/critical risk → DockerSandbox (if available).
        Low/medium risk → LocalSandbox (default).
        Falls back gracefully to LocalSandbox when Docker is unavailable.
        """
        if risk_level is None:
            return self._sandbox_provider

        target = self.sandbox_by_risk.get(risk_level.lower(), "local")

        if target == "docker":
            docker = DockerSandbox()
            if docker.is_available():
                logger.info(
                    "Risk-based sandbox selection: risk=%s → docker (#484)",
                    risk_level,
                )
                return docker
            logger.warning(
                "Docker requested for risk=%s but unavailable, "
                "falling back to LocalSandbox (#484)",
                risk_level,
            )

        return LocalSandbox()

    @property
    def sandbox(self) -> SandboxProvider:
        """Access the default sandbox provider for agent execution."""
        return self._sandbox_provider

    def get_sandbox(self, run_id: str) -> SandboxProvider:
        """Get the risk-selected sandbox for a specific run (#484).

        Falls back to the default sandbox if no risk-based selection was made.
        """
        return self._active_sandboxes.get(run_id, self._sandbox_provider)

    def setup(
        self,
        job_id: str,
        run_id: str,
        workspace_type: str | None = None,
        risk_level: str | None = None,
    ) -> Path:
        """
        Prepare execution environment.

        Args:
            job_id: Task ID
            run_id: Run ID
            workspace_type: Explicitly override workspace isolation (overrides default)
            risk_level: Risk level (used for auto-selecting workspace AND sandbox)

        Returns:
            Path to the workspace directory.
        """
        ws_type = self._resolve_workspace_type(workspace_type, risk_level)
        backend = self._get_workspace_backend(ws_type)

        # Check workspace availability
        if not backend.is_available():
            if ws_type == WorkspaceIsolation.WORKTREE:
                # Worktree not available, fall back to local
                backend = self._get_workspace_backend(WorkspaceIsolation.LOCAL)
            else:
                raise RuntimeError(
                    f"Workspace backend {ws_type.value} is not available"
                )

        # Select sandbox based on risk level (#484).
        # Graceful degradation: if Docker is unavailable, falls back to LocalSandbox.
        selected_sandbox = self._select_sandbox(risk_level)
        self._active_sandboxes[run_id] = selected_sandbox

        work_dir = backend.setup(job_id, run_id)
        self._active_runs[run_id] = backend
        return work_dir

    def get_work_dir(self, job_id: str, run_id: str) -> Path | None:
        """Get working directory."""
        backend = self._active_runs.get(run_id)
        if backend:
            return backend.get_work_dir(job_id, run_id)
        return None

    def cleanup(self, job_id: str, run_id: str) -> None:
        """Clean up execution environment (on success)."""
        if self.cleanup_policy == "never":
            self.preserve(job_id, run_id, reason="cleanup_policy=never")
            return
        self._active_sandboxes.pop(run_id, None)
        backend = self._active_runs.pop(run_id, None)
        if backend:
            backend.cleanup(job_id, run_id)

    def preserve(
        self, job_id: str, run_id: str, reason: str = ""
    ) -> Path | None:
        """Preserve execution scene (on failure)."""
        if self.cleanup_policy == "always":
            self._active_sandboxes.pop(run_id, None)
            backend = self._active_runs.pop(run_id, None)
            if backend:
                backend.cleanup(job_id, run_id)
            return None
        self._active_sandboxes.pop(run_id, None)
        backend = self._active_runs.pop(run_id, None)
        if backend:
            return backend.preserve(job_id, run_id, reason)
        return None

    def finalize(
        self, job_id: str, run_id: str, success: bool, reason: str = ""
    ) -> Path | None:
        """
        Finalize a run based on outcome and cleanup_policy.

        Args:
            job_id: Task ID
            run_id: Run ID
            success: Whether the run succeeded
            reason: Reason if failed

        Returns:
            Preserved path if preserved, None if cleaned up.
        """
        if success:
            if self.cleanup_policy == "never":
                return self.preserve(job_id, run_id, reason="success_but_never_policy")
            self.cleanup(job_id, run_id)
            return None
        else:
            if self.cleanup_policy == "always":
                self.cleanup(job_id, run_id)
                return None
            return self.preserve(job_id, run_id, reason=reason)

    # ------------------------------------------------------------------
    # Node-level workspace isolation (#176)
    # ------------------------------------------------------------------

    def setup_node(
        self,
        job_id: str,
        run_id: str,
        node_id: str,
        strategy: str = "shared",
    ) -> "NodeWorkspace":
        """
        Prepare an isolated workspace for a DAG node.

        For SHARED strategy, returns the run's existing work_dir.
        For WORKTREE/COPY, creates an isolated workspace.

        Returns a NodeWorkspace with the node's workspace_path set.
        """
        from core.models import NodeWorkspace, NodeWorkspaceStrategy

        ws_strategy = NodeWorkspaceStrategy(strategy)
        run_backend = self._active_runs.get(run_id)
        run_work_dir = run_backend.get_work_dir(job_id, run_id) if run_backend else None

        if ws_strategy == NodeWorkspaceStrategy.SHARED or not run_work_dir:
            return NodeWorkspace(
                node_id=node_id,
                strategy=NodeWorkspaceStrategy.SHARED,
                base_path=str(run_work_dir) if run_work_dir else "",
                workspace_path=str(run_work_dir) if run_work_dir else "",
            )

        # WORKTREE requires repo_root; fall back to COPY if missing
        if ws_strategy == NodeWorkspaceStrategy.WORKTREE and not self.repo_root:
            ws_strategy = NodeWorkspaceStrategy.COPY

        # WORKTREE / COPY: create isolated workspace
        node_work_dir = Path(self.base_path) / "nodes" / run_id / node_id
        node_work_dir.mkdir(parents=True, exist_ok=True)

        baseline_commit = ""
        if ws_strategy == NodeWorkspaceStrategy.WORKTREE and self.repo_root:
            # Create git worktree for the node
            try:
                import subprocess
                result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    capture_output=True, text=True,
                    cwd=self.repo_root, timeout=10,
                )
                if result.returncode == 0:
                    baseline_commit = result.stdout.strip()
                wt_result = subprocess.run(
                    ["git", "worktree", "add", str(node_work_dir), "HEAD"],
                    capture_output=True, text=True,
                    cwd=self.repo_root, timeout=30,
                )
                if wt_result.returncode != 0:
                    raise RuntimeError(
                        f"git worktree add failed (rc={wt_result.returncode}): "
                        f"{wt_result.stderr.strip()}"
                    )
            except (FileNotFoundError, subprocess.TimeoutExpired, RuntimeError, Exception):
                # Git not available or worktree failed, fall back to SHARED
                import shutil
                shutil.rmtree(node_work_dir, ignore_errors=True)
                return NodeWorkspace(
                    node_id=node_id,
                    strategy=NodeWorkspaceStrategy.SHARED,
                    base_path=str(run_work_dir),
                    workspace_path=str(run_work_dir),
                )
        elif ws_strategy == NodeWorkspaceStrategy.COPY and run_work_dir:
            # Copy workspace files for the node
            import shutil
            try:
                shutil.copytree(
                    str(run_work_dir), str(node_work_dir),
                    dirs_exist_ok=True,
                )
            except OSError:
                shutil.rmtree(node_work_dir, ignore_errors=True)
                return NodeWorkspace(
                    node_id=node_id,
                    strategy=NodeWorkspaceStrategy.SHARED,
                    base_path=str(run_work_dir),
                    workspace_path=str(run_work_dir),
                )

        # Track node workspace for cleanup
        key = f"{run_id}:{node_id}"
        self._node_workspaces[key] = NodeWorkspace(
            node_id=node_id,
            strategy=ws_strategy,
            base_path=str(run_work_dir),
            workspace_path=str(node_work_dir),
            baseline_commit=baseline_commit,
        )
        return self._node_workspaces[key]

    def cleanup_node(self, job_id: str, run_id: str, node_id: str) -> None:
        """Clean up a node's isolated workspace after execution."""
        key = f"{run_id}:{node_id}"
        ws = self._node_workspaces.pop(key, None)
        if not ws:
            return

        ws_path = Path(ws.workspace_path)
        if ws.strategy.value == "worktree" and self.repo_root:
            # Remove git worktree
            try:
                import subprocess
                subprocess.run(
                    ["git", "worktree", "remove", str(ws_path), "--force"],
                    capture_output=True, text=True,
                    cwd=self.repo_root, timeout=30,
                )
            except (FileNotFoundError, Exception):
                pass
        elif ws.strategy.value == "copy" and ws_path.exists():
            import shutil
            shutil.rmtree(ws_path, ignore_errors=True)

    def cleanup_node_artifacts(
        self,
        job_id: str,
        run_id: str,
        node_id: str,
        expected_artifacts: list[str],
        started_at: float | None = None,
    ) -> list[str]:
        """Remove or quarantine files generated by a node that are not in expected_artifacts (#240).

        Scans the workspace for .py files created during node execution
        (by modification time) that are not declared in owned_files.

        Args:
            job_id: Task ID
            run_id: Run ID
            node_id: Node ID
            expected_artifacts: Files this node declared ownership of
            started_at: Epoch timestamp when node started (for filtering)

        Returns:
            List of cleaned up file paths.
        """
        backend = self._active_runs.get(run_id)
        if not backend:
            return []

        work_dir = backend.get_work_dir(job_id, run_id)
        if not work_dir:
            return []

        work_path = Path(work_dir)
        if not work_path.exists():
            return []

        # When no owned_files declared, skip cleanup (nothing to validate against)
        if not expected_artifacts:
            return []

        expected_set = set(expected_artifacts)
        cleaned: list[str] = []

        for py_file in work_path.rglob("*.py"):
            # Skip files in ignored directories
            if any(part in {".git", ".venv", "venv", "node_modules", "__pycache__", ".leftovers"}
                   for part in py_file.parts):
                continue

            rel_path = str(py_file.relative_to(work_path))

            # Skip if this file is in expected artifacts
            if rel_path in expected_set:
                continue

            # Check modification time if started_at is provided
            if started_at:
                try:
                    mtime = py_file.stat().st_mtime
                    if mtime < started_at:
                        continue  # File existed before node started
                except OSError:
                    continue

            # This is a leftover file — quarantine it
            quarantine_dir = work_path / ".leftovers"
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            target = quarantine_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)

            import shutil
            try:
                shutil.move(str(py_file), str(target))
                cleaned.append(rel_path)
            except (OSError, shutil.Error):
                pass  # Best-effort cleanup

        return cleaned

    async def execute_hook(
        self,
        hook_name: str,
        command: str,
        work_dir: Path,
        timeout: int = 60,
    ) -> HookResult:
        """
        Execute a lifecycle hook as a subprocess on the host.

        Hooks run on the host, not through SandboxProvider. This matches
        Symphony's design — hooks are operational scripts (npm install, pytest)
        that need access to the real host environment.

        Failure semantics:
        - after_create / before_run: raises HookError (fatal)
        - after_run / before_remove: logs error, returns HookResult(success=False)
        """
        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(work_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            elapsed = int((time.monotonic() - start) * 1000)
            success = proc.returncode == 0
            out = stdout.decode(errors="replace")
            err = stderr.decode(errors="replace")

            if not success and hook_name in ("after_create", "before_run"):
                raise HookError(hook_name, output=out, error=err)

            return HookResult(
                success=success, output=out, error=err, duration_ms=elapsed,
            )

        except asyncio.TimeoutError:
            elapsed = int((time.monotonic() - start) * 1000)
            msg = f"Hook '{hook_name}' timed out after {timeout}s"
            # Kill the subprocess to prevent resource leaks
            try:
                proc.kill()
                await proc.communicate()  # Reap the process
            except (ProcessLookupError, OSError):
                pass
            if hook_name in ("after_create", "before_run"):
                raise HookError(hook_name, error=msg)
            return HookResult(success=False, error=msg, duration_ms=elapsed)

    def _resolve_workspace_type(
        self,
        explicit: str | None,
        risk_level: str | None,
    ) -> WorkspaceIsolation:
        """Resolve final workspace isolation type."""
        if explicit:
            return WorkspaceIsolation(explicit)
        if risk_level and risk_level.lower() in self.workspace_by_risk:
            mapped = self.workspace_by_risk[risk_level.lower()]
            return WorkspaceIsolation(mapped)
        return self.workspace_type

    def get_active_runs(self) -> dict[str, str]:
        """Get currently active runs (run_id -> workspace_type)."""
        return {
            rid: b.workspace_type.value
            for rid, b in self._active_runs.items()
        }
