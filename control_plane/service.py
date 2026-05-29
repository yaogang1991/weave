"""
RunService — Reusable execution service shared by CLI and Worker.

Encapsulates the complete plan -> execute -> summary lifecycle:
1. Accepts a job submission (creates Job record)
2. Plans a DAG via IntelligentOrchestrator
3. Executes the DAG via DAGExecutionEngine
4. Updates Job/Run records with final state
5. Handles timeouts, retries, and error classification

Design decisions:
- All public methods are async for uniform interface
- Timeout is enforced at the *job* level via asyncio.wait_for
- Error categories are standardized: timeout / eval_failed / tool_blocked / unknown
- The service is stateless; all state lives in JobRepository
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any  # noqa: F401 — still used for dict[str, Any] return types

# Allow imports from project root (core/, orchestrator/, agent/, session/, ...)
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import LLMConfig, WatchdogConfig  # noqa: E402
from session.store import SessionStore  # noqa: E402
from guardrails.policy import GuardrailPolicy  # noqa: E402
from core.models import DAG, EventType, NodeStatus  # noqa: E402
from core.exceptions import PendingApprovalError  # noqa: E402

from control_plane.approval import ApprovalRepository  # noqa: E402
from control_plane.models import Job, Run, JobStatus, RetryPolicy  # noqa: E402
from control_plane.repository import JobRepository  # noqa: E402
from control_plane.run_lifecycle import RunLifecycleManager  # noqa: E402
from control_plane.errors import classify_error  # noqa: E402
from control_plane.execution_factory import ExecutionFactory  # noqa: E402
from backend.lifecycle import BackendManager  # noqa: E402
from control_plane.hooks import ExecutionHook, ExecutionContext  # noqa: E402
from control_plane.job_lifecycle import JobLifecycleManager  # noqa: E402

logger = logging.getLogger(__name__)


# ============================================================================
# Error classification helper
# ============================================================================


def _default_watchdog_config() -> WatchdogConfig:
    """Create a WatchdogConfig with sensible defaults."""
    return WatchdogConfig()


def _scan_existing_files(
    root: Path,
    max_files: int = 100,
) -> list[dict[str, str]]:
    """Scan the workspace for existing source/test files (#335).

    Returns a list of ``{"path": ..., "type": ...}`` dicts where *type*
    is "source" for ``*.py`` outside ``tests/``, "test" for files under
    ``tests/``, or "config" for setup/config files.  Skips hidden dirs,
    ``__pycache__``, ``data/``, ``.git``, and common non-source dirs.
    """
    _SKIP_DIRS = frozenset((
        "__pycache__", ".git", ".weave", "data", "node_modules",
        ".venv", "venv", ".mypy_cache", ".pytest_cache", ".tox",
    ))
    _CONFIG_NAMES = frozenset((
        "setup.py", "setup.cfg", "pyproject.toml", "requirements.txt",
        "Makefile", "tox.ini", ".flake8", "conftest.py",
    ))

    results: list[dict[str, str]] = []
    try:
        for path in sorted(root.rglob("*.py")):
            # Skip hidden and junk directories
            parts = path.relative_to(root).parts
            if any(p.startswith(".") or p in _SKIP_DIRS for p in parts):
                continue
            rel = path.relative_to(root).as_posix()
            if path.name in _CONFIG_NAMES:
                ftype = "config"
            elif any(p == "tests" or p == "test" for p in parts):
                ftype = "test"
            else:
                ftype = "source"
            results.append({"path": rel, "type": ftype})
            if len(results) >= max_files:
                break
    except OSError:
        pass
    return results


# ============================================================================
# UTC helper
# ============================================================================


def _utc_now() -> datetime:
    """Current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


def _compute_summary(dag: DAG | None) -> dict[str, Any]:
    """Compute DAG execution summary from node statuses.

    Replaces the redundant engine re-creation that was previously used
    solely to call ``engine.get_execution_summary()``.
    """
    if dag is None:
        return {}
    total = len(dag.nodes)
    success = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.SUCCESS)
    partial_pass = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.PARTIAL_PASS)
    warned = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.WARNED)
    failed = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.FAILED)
    skipped = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.SKIPPED)
    return {
        "total_nodes": total,
        "success": success,
        "partial_pass": partial_pass,
        "warned": warned,
        "failed": failed,
        "skipped": skipped,
        "all_succeeded": failed == 0 and skipped == 0 and partial_pass == 0,
        "node_details": {
            nid: {
                "status": n.status.value,
                "agent": n.agent_type,
                "duration_ms": (
                    (n.completed_at - n.started_at).total_seconds() * 1000
                    if n.completed_at and n.started_at else None
                ),
            }
            for nid, n in dag.nodes.items()
        },
    }


def _extract_dag_errors(dag: DAG | None) -> tuple[str, str]:
    """Extract error message and category from failed DAG nodes."""
    if dag is None:
        return "", "unknown"
    failed_nodes = [nid for nid, n in dag.nodes.items() if n.status == NodeStatus.FAILED]
    if not failed_nodes:
        return "", "unknown"
    errors = [f"{nid}: {dag.nodes[nid].error}" for nid in failed_nodes]
    error_msg = "; ".join(errors)
    return error_msg, classify_error(error_msg)


def _write_job_result(
    artifact_path: str,
    job: Job,
    run: Run,
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Generate and write a standardized job_result.json artifact.

    Inlined from JobResultWriter — the serialization is simple enough
    to live as a module-level function.
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
    artifact_dir = Path(artifact_path) / job.id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    with open(artifact_dir / "job_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, default=str, ensure_ascii=False)
    return result


@dataclass
class _RunContext:
    """Execution context bundled for outcome resolution."""

    job: Job                          # Original job (always non-None)
    run: Run
    outcome: str                      # "succeeded" | "failed" | "timed_out" | "canceled"
    result_dag: DAG | None = None
    error_msg: str = ""
    error_cat: str = ""
    timeout: int = 1800
    backend_manager: Any = None       # BackendManager | None
    bls: Any = None                   # BackendLifecycleService | None
    work_dir: str | None = None


# ============================================================================
# RunService
# ============================================================================


class RunService:
    """
    High-level service that orchestrates job execution.

    Usage::

        service = RunService(repository=repo, llm_config=config)
        job = await service.submit_job("Build a REST API")
        run = await service.run_job(job.id)
    """

    def __init__(
        self,
        repository: JobRepository,
        llm_config: LLMConfig,
        max_parallel: int = 3,
        agent_timeout: int = 300,
        max_context_tokens: int = 100000,
        artifact_path: str = "./data/artifacts",
        event_store_path: str = "./data/events",
        max_iterations: int = 50,
        policy: GuardrailPolicy | None = None,
        default_backend: str = "local",
        backend_base_path: str = "./data/backends",
        non_interactive: bool = False,
        approval_repo: ApprovalRepository | None = None,
        approval_timeout_sec: int = 300,
        watchdog_config: WatchdogConfig | None = None,
    ) -> None:
        self.repository = repository
        self.llm_config = llm_config
        self.max_parallel = max_parallel
        self.agent_timeout = agent_timeout
        self.max_context_tokens = max_context_tokens
        self.artifact_path = artifact_path
        self.event_store_path = event_store_path
        self.max_iterations = max_iterations
        self.policy = policy
        self.non_interactive = non_interactive
        self.approval_repo = approval_repo
        self.approval_timeout_sec = approval_timeout_sec
        self.watchdog_config = watchdog_config or _default_watchdog_config()
        self._running_tasks: dict[str, asyncio.Task] = {}

        # Execution hooks — subsystems register lifecycle callbacks
        self._hooks: list[ExecutionHook] = []
        self._register_hooks()

        self.default_backend = default_backend
        self.backend_base_path = backend_base_path

        # Extracted collaborators (#177 PR 1)
        self._lifecycle = RunLifecycleManager(repository)
        # Extracted lifecycle manager (#177 PR6)
        self._job_lifecycle = JobLifecycleManager(
            repository=repository,
            emit_event=self._emit_event,
            running_tasks=self._running_tasks,
        )

        # Extracted factory (#177 PR3)
        self._execution_factory = ExecutionFactory(
            llm_config=llm_config,
            max_parallel=max_parallel,
            agent_timeout=agent_timeout,
            max_context_tokens=max_context_tokens,
            max_iterations=max_iterations,
            artifact_path=artifact_path,
            non_interactive=non_interactive,
            watchdog_config=self.watchdog_config,
            hooks=self._hooks,
            approval_repo=approval_repo,
            policy=policy,
            budget_manager=None,  # TODO: propagate from WeaveConfig.budget (#595)

        )

    # ------------------------------------------------------------------
    # Public API — Job lifecycle
    # ------------------------------------------------------------------

    async def submit_job(
        self,
        requirement: str,
        project_path: str | None = None,
        timeout: int = 1800,
        max_attempts: int = 3,
    ) -> Job:
        """
        Create and persist a new job, returning it immediately.

        Args:
            requirement: Natural-language description of the work.
            project_path: Optional path to the project directory.
            timeout: Maximum wall-clock seconds for a single *run* attempt.
            max_attempts: Maximum retry attempts (embedded in RetryPolicy).
        """
        retry_policy = RetryPolicy(max_attempts=max_attempts, backoff_sec=5)
        job = self.repository.create_job(
            requirement=requirement,
            project_path=project_path,
            retry_policy=retry_policy,
        )
        # Store the per-run timeout in job metadata so run_job can read it
        job.metadata["run_timeout_sec"] = timeout
        self.repository.update_job(job)
        return job

    async def run_job(self, job_id: str) -> Run:
        """
        Execute the full plan -> execute -> summary lifecycle for *job_id*.

        Flow:
            1. Fetch job and validate status.
            2. Setup backend workspace and lifecycle hooks.
            3. Plan + execute DAG (with timeout).
            4. Resolve outcome via ``_resolve_run_outcome``.
            5. Cleanup and write result artifact.

        Raises:
            ValueError: If the job does not exist.
            PendingApprovalError: If approval is required mid-execution.
        """
        job = self.repository.get_job(job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")

        if job.status != JobStatus.RUNNING:
            raise ValueError(
                f"Expected job {job_id} to be RUNNING, got {job.status.value}"
            )

        current_task = asyncio.current_task()
        if current_task is not None:
            self._running_tasks[job_id] = current_task

        session_id = str(uuid.uuid4())
        store = SessionStore(self.event_store_path)
        store.create_session(session_id, "weave_run")
        run = self.repository.create_run(job_id, session_id)
        work_dir: str | None = None
        timeout: int = job.metadata.get("run_timeout_sec", 1800)

        outcome = "failed"
        result_dag: DAG | None = None
        error_msg = ""
        error_cat = ""
        backend_manager: BackendManager | None = None
        bls: Any = None  # BackendLifecycleService

        try:
            # --- Determine outcome ---
            try:
                if not job.project_path:
                    raise ValueError(
                        "project_path is required for job execution. "
                        "Refusing to use cwd as target — agents may modify Weave itself. "
                        "Submit jobs with --project /path/to/target."
                    )
                project_root = Path(job.project_path).resolve()
                from control_plane.backend_lifecycle import BackendLifecycleService as _BLS
                bls = _BLS(backend_base_path=self.backend_base_path)
                backend_manager = bls.create_backend_manager(str(project_root))
                work_dir = bls.setup_workspace(
                    backend_manager, job_id=job.id, run_id=run.id,
                    risk_level=job.metadata.get("risk_level"),
                )

                if self.non_interactive and self.approval_repo is not None:
                    self.approval_repo.expire_tickets()

                hooks = _BLS.load_project_hooks(job.project_path)
                await bls.run_hook(backend_manager, "after_create", hooks, work_dir)
                await bls.run_hook(backend_manager, "before_run", hooks, work_dir)

                result_dag = await asyncio.wait_for(
                    self._execute_plan_and_run(
                        job, session_id, store, Path(work_dir), run.id, backend_manager,
                    ),
                    timeout=timeout,
                )

                await bls.run_hook(backend_manager, "after_run", hooks, work_dir)

                summary = _compute_summary(result_dag)
                if summary.get("all_succeeded", False):
                    outcome = "succeeded"
                else:
                    error_msg, error_cat = _extract_dag_errors(result_dag)

            except asyncio.TimeoutError:
                outcome = "timed_out"
                error_msg = "Job execution timed out"
                error_cat = "timeout"

            except asyncio.CancelledError:
                outcome = "canceled"

            except PendingApprovalError as exc:
                self._lifecycle.mark_pending_approval(run, exc.ticket_id)
                raise

            except Exception as exc:
                error_msg = f"{type(exc).__name__}: {str(exc)}\n{traceback.format_exc()}"
                error_cat = classify_error(str(exc))

            # --- Resolve outcome (unified for all paths) ---
            ctx = _RunContext(
                job=job, run=run, outcome=outcome, result_dag=result_dag,
                error_msg=error_msg, error_cat=error_cat, timeout=timeout,
                backend_manager=backend_manager, bls=bls, work_dir=work_dir,
            )
            run = await self._resolve_run_outcome(ctx)

        finally:
            self._running_tasks.pop(job_id, None)
            try:
                final_job = self.repository.get_job(job_id)
                final_run = self.repository.get_run(run.id)
                if final_job and final_run:
                    _write_job_result(
                        self.artifact_path, final_job, final_run,
                        final_run.dag_result or {},
                    )
            except Exception as exc:
                logger.warning(
                    "Failed to write job result artifact for %s: %s",
                    job_id, exc,
                )

        return self.repository.get_run(run.id) or run

    # ------------------------------------------------------------------
    # Job lifecycle operations (delegated to JobLifecycleManager)
    # ------------------------------------------------------------------

    async def handle_job_failure(
        self,
        job: Job,
        error: str,
        error_category: str = "unknown",
    ) -> Job:
        """Apply retry policy or dead-letter a failed job."""
        return await self._job_lifecycle.handle_job_failure(job, error, error_category)

    async def resume_after_approval(self, job_id: str, ticket_id: str) -> Run | None:
        """Resume a job after an approval ticket is approved."""
        return await self._job_lifecycle.resume_after_approval(job_id, ticket_id)

    async def abort_after_rejection(self, job_id: str, ticket_id: str, reason: str = "") -> Job:
        """Abort a job after an approval ticket is rejected."""
        return await self._job_lifecycle.abort_after_rejection(job_id, ticket_id, reason)

    def _emit_event(self, event_type: str, job_id: str, details: dict[str, Any]) -> None:
        """发出结构化事件（用于日志和监控）。"""
        event: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "job_id": job_id,
            "details": details,
        }
        logger.info("Execution event: %s", json.dumps(event))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _register_hooks(self) -> None:
        """Register execution lifecycle hooks with dependency injection."""
        from control_plane.hooks import MemoryHook, LearningHook, ImpactHook
        self._hooks = [
            MemoryHook(),
            LearningHook(repository=self.repository),
            ImpactHook(llm_config=self.llm_config),
        ]

    async def _run_before_hooks(self, ctx: ExecutionContext) -> None:
        """Run all before_execution hooks. Errors are logged, never raised."""
        for hook in self._hooks:
            try:
                await hook.before_execution(ctx)
            except Exception as exc:
                logger.debug("Hook %s.before_execution failed: %s", type(hook).__name__, exc)

    async def _run_after_hooks(self, ctx: ExecutionContext, result_dag: DAG) -> None:
        """Run all after_execution hooks. Errors are logged, never raised."""
        for hook in self._hooks:
            try:
                await hook.after_execution(ctx, result_dag)
            except Exception as exc:
                logger.debug("Hook %s.after_execution failed: %s", type(hook).__name__, exc)

    async def _execute_plan_and_run(
        self,
        job: Job,
        session_id: str,
        store: SessionStore,
        work_dir: Path,
        run_id: str | None = None,
        backend_manager: BackendManager | None = None,
    ) -> DAG:
        """Plan a DAG and execute it. Subsystems run via hooks."""
        from control_plane.hooks import ExecutionContext

        ctx = ExecutionContext(
            job=job,
            session_id=session_id,
            store=store,
            work_dir=work_dir,
            run_id=run_id,
            llm_config=self.llm_config,
            repository=self.repository,
        )

        # Before-hooks: memory, learning, impact prediction
        await self._run_before_hooks(ctx)

        # Persist prediction metadata immediately (before execution may fail/cancel)
        if ctx.metadata:
            ctx.job.metadata.update(ctx.metadata)
            self.repository.update_job(ctx.job)

        # Core: plan + execute
        orchestrator = self._execution_factory.create_orchestrator(store)
        project_path = job.project_path or (str(work_dir) if work_dir else None)
        project_context = {"project_path": project_path} if project_path else None

        # Scan existing workspace files so the planner can reconcile with
        # files created by previous runs (#335).
        scan_root = work_dir if work_dir else (
            Path(project_path) if project_path else None
        )
        if scan_root:
            existing = _scan_existing_files(scan_root)
            if existing:
                if project_context is None:
                    project_context = {}
                project_context["existing_files"] = existing

        # Inject retry context so the planner builds on existing work
        # instead of starting from scratch (#328).
        if job.attempt > 0:
            if project_context is None:
                project_context = {}
            project_context["retry_attempt"] = job.attempt
            project_context["max_attempts"] = job.retry_policy.max_attempts
            if scan_root:
                project_context["existing_file_count"] = sum(
                    1 for p in scan_root.rglob("*.py")
                    if not any(s in str(p) for s in
                               ["__pycache__", ".git", "data/"])
                )

        dag = await orchestrator.plan(
            requirement=job.requirement,
            project_context=project_context,
        )

        # Emit DAG structure to session store (same as main.py:197)
        store.emit_event(
            session_id,
            EventType.SESSION_DAG,
            {
                "nodes": {
                    nid: {
                        "task": n.task_description,
                        "agent_type": n.agent_type,
                        "dependencies": [e.from_node for e in dag.edges if e.to_node == nid],
                    }
                    for nid, n in dag.nodes.items()
                },
                "edges": [{"from": e.from_node, "to": e.to_node} for e in dag.edges],
                "requirement": job.requirement,
            },
        )

        engine = self._execution_factory.create_execution_engine(
            session_id, store,
            replan_handler=lambda dag_ref, failed_id: orchestrator.replan(
                dag_ref, failed_id, job.requirement,
            ),
            work_dir=work_dir,
            memory_manager=ctx.memory_manager,
            job_id=job.id,
            approval_repo=self.approval_repo,
            run_id=run_id,
            backend_manager=backend_manager,
            project_dir=job.project_path,
        )
        result_dag = await engine.execute(dag)

        # After-hooks: impact verification, memory storage
        await self._run_after_hooks(ctx, result_dag)

        # Merge hook metadata into job
        if ctx.metadata:
            job.metadata.update(ctx.metadata)
            self.repository.update_job(job)

        return result_dag

    # ------------------------------------------------------------------
    # Outcome resolution
    # ------------------------------------------------------------------

    async def _resolve_run_outcome(self, ctx: _RunContext) -> Run:
        """Unified outcome resolution for all execution paths.

        Handles run/job state transitions, retry policy, and workspace
        cleanup/preserve in one place instead of duplicated per-exception blocks.
        """
        job_id = ctx.job.id
        current_job = self.repository.get_job(job_id)

        # External cancel takes priority over local outcome
        if current_job and current_job.status == JobStatus.CANCELED and ctx.outcome != "canceled":
            run = self._lifecycle.mark_canceled(ctx.run, "Job canceled during execution")
            self._preserve_workspace(ctx, reason="canceled")
            return run

        # --- Succeeded ---
        if ctx.outcome == "succeeded":
            summary = _compute_summary(ctx.result_dag)
            run = self._lifecycle.mark_succeeded(ctx.run, summary)
            if current_job:
                resolved = self._lifecycle.resolve_external_status(run, current_job)
                if resolved:
                    self._preserve_workspace(ctx, run=run, reason="external_status_change")
                    return run
            self.repository.transition_job_status(job_id, JobStatus.SUCCEEDED)
            self._cleanup_workspace(ctx, run=run)
            if ctx.work_dir:
                run.dag_result = {**run.dag_result, "work_dir": ctx.work_dir}
            return run

        # --- Canceled ---
        if ctx.outcome == "canceled":
            run = self._lifecycle.mark_canceled(ctx.run, "Run coroutine canceled")
            if current_job and current_job.status == JobStatus.RUNNING:
                self.repository.transition_job_status(
                    job_id, JobStatus.CANCELED,
                    error="Run canceled", error_category="tool_blocked",
                )
            self._preserve_workspace(ctx, run=run, reason="canceled")
            if ctx.work_dir:
                run.dag_result = {**run.dag_result, "work_dir": ctx.work_dir}
            return run

        # --- Failed or Timed Out ---
        run = ctx.run
        if ctx.outcome == "timed_out":
            run = self._lifecycle.mark_timed_out(run, ctx.timeout)
            ctx.error_msg = ctx.error_msg or "Job execution timed out"
            ctx.error_cat = ctx.error_cat or "timeout"
        else:
            summary = _compute_summary(ctx.result_dag) if ctx.result_dag else {}
            run = self._lifecycle.mark_failed(run, summary or {"error": "execution_error"})

        self.repository.transition_job_status(
            job_id, JobStatus.FAILED,
            error=ctx.error_msg, error_category=ctx.error_cat,
        )

        # Node-level timeout: upgrade run status to TIMED_OUT
        if ctx.error_cat == "timeout" and ctx.result_dag:
            failed_nodes = [
                nid for nid, n in ctx.result_dag.nodes.items()
                if n.status == NodeStatus.FAILED
            ]
            if failed_nodes:
                run = self._lifecycle.mark_timed_out(run, ctx.timeout)

        # Re-fetch job for retry policy (state may have changed)
        job = self.repository.get_job(job_id) or ctx.job
        await self._job_lifecycle.handle_job_failure(
            job, error=ctx.error_msg, error_category=ctx.error_cat,
        )
        self._preserve_workspace(ctx, run=run, reason=ctx.error_cat or "failed")
        if ctx.work_dir:
            run.dag_result = {**run.dag_result, "work_dir": ctx.work_dir}
        return run

    def _preserve_workspace(
        self,
        ctx: _RunContext,
        run: Run | None = None,
        reason: str = "unknown",
    ) -> None:
        if ctx.work_dir is not None and ctx.backend_manager and ctx.bls:
            ctx.bls.preserve(
                ctx.backend_manager, ctx.job.id, (run or ctx.run).id, reason=reason,
            )

    def _cleanup_workspace(
        self,
        ctx: _RunContext,
        run: Run | None = None,
    ) -> None:
        if ctx.work_dir is not None and ctx.backend_manager and ctx.bls:
            ctx.bls.cleanup(ctx.backend_manager, ctx.job.id, (run or ctx.run).id)
