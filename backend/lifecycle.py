"""
Backend lifecycle manager.

Manages creation, execution, cleanup, and preservation of execution backends.
Supports two orthogonal dimensions:
- Workspace isolation (LOCAL / WORKTREE) — how files are managed
- Execution sandbox (LOCAL / DOCKER) — where processes run

Hooks are executed as subprocesses on the host (not through SandboxProvider),
matching Symphony's design — hooks are operational scripts that need host access.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.base import ExecutionBackend, WorkspaceIsolation, ExecutionSandbox
from backend.local import LocalBackend
from backend.worktree import WorktreeBackend
from backend.sandbox import SandboxProvider, LocalSandbox, DockerSandbox, CommandResult


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
        cleanup_policy: str = "on_success",
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
        self.cleanup_policy = cleanup_policy
        _VALID_POLICIES = ("always", "on_success", "never")
        if self.cleanup_policy not in _VALID_POLICIES:
            raise ValueError(
                f"Invalid cleanup_policy '{self.cleanup_policy}', "
                f"must be one of {_VALID_POLICIES}"
            )
        self._backends: dict[str, ExecutionBackend] = {}
        self._sandbox_provider = self._create_sandbox()
        self._active_runs: dict[str, ExecutionBackend] = {}  # run_id -> backend
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
            return DockerSandbox()
        else:
            raise ValueError(f"Unknown sandbox type: {self.sandbox_type}")

    @property
    def sandbox(self) -> SandboxProvider:
        """Access the sandbox provider for agent execution."""
        return self._sandbox_provider

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
            risk_level: Risk level (used for auto-selecting workspace isolation)
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

        # Check sandbox availability (fail fast if user requested unavailable sandbox)
        if not self._sandbox_provider.is_available():
            raise RuntimeError(
                f"Execution sandbox {self.sandbox_type.value} is not available. "
                f"Please install the required dependencies or switch to 'local'."
            )

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
        backend = self._active_runs.pop(run_id, None)
        if backend:
            backend.cleanup(job_id, run_id)

    def preserve(
        self, job_id: str, run_id: str, reason: str = ""
    ) -> Path | None:
        """Preserve execution scene (on failure)."""
        if self.cleanup_policy == "always":
            backend = self._active_runs.pop(run_id, None)
            if backend:
                backend.cleanup(job_id, run_id)
            return None
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
                subprocess.run(
                    ["git", "worktree", "add", str(node_work_dir), "HEAD"],
                    capture_output=True, text=True,
                    cwd=self.repo_root, timeout=30,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
                # Git not available, fall back to SHARED
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
                pass

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
