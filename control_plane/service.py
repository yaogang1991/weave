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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow imports from project root (core/, orchestrator/, agent/, session/, ...)
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import LLMConfig, WatchdogConfig  # noqa: E402
from session.store import SessionStore  # noqa: E402
from guardrails.policy import GuardrailPolicy  # noqa: E402
from core.models import EventType  # noqa: E402
from core.exceptions import PendingApprovalError  # noqa: E402

from control_plane.models import Job, Run, JobStatus, RetryPolicy  # noqa: E402
from control_plane.repository import JobRepository  # noqa: E402
from control_plane.run_lifecycle import RunLifecycleManager  # noqa: E402
from control_plane.job_result import JobResultWriter  # noqa: E402
from control_plane.errors import classify_error  # noqa: E402
from control_plane.execution_factory import ExecutionFactory  # noqa: E402
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
            rel = str(path.relative_to(root))
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
        approval_repo: Any | None = None,
        approval_timeout_sec: int = 300,
        watchdog_config: Any | None = None,
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
        self._running_tasks: dict[str, asyncio.Task[Any]] = {}

        # Execution hooks — subsystems register lifecycle callbacks
        self._hooks: list[Any] = []
        self._register_hooks()

        self.default_backend = default_backend
        self.backend_base_path = backend_base_path

        # Extracted collaborators (#177 PR 1)
        self._lifecycle = RunLifecycleManager(repository)
        self._result_writer = JobResultWriter(artifact_path)
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
            1. Fetch job and transition status to RUNNING.
            2. Create a session + Run record.
            3. Plan DAG via IntelligentOrchestrator.
            4. Execute DAG via DAGExecutionEngine (with timeout).
            5. Update Run and Job records with final state.
            6. On failure: apply RetryPolicy (retry via QUEUED or dead-letter).
            7. Return the Run.

        Raises:
            ValueError: If the job does not exist.
        """
        job = self.repository.get_job(job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")

        # Worker is responsible for LEASED -> RUNNING transition.
        # run_job() only executes jobs that are already RUNNING.
        if job.status != JobStatus.RUNNING:
            raise ValueError(
                f"Expected job {job_id} to be RUNNING, got {job.status.value}"
            )

        current_task = asyncio.current_task()
        if current_task is not None:
            self._running_tasks[job_id] = current_task

        # Create session
        session_id = str(uuid.uuid4())
        store = SessionStore(self.event_store_path)
        store.create_session(session_id, "weave_run")

        # Create Run record
        run = self.repository.create_run(job_id, session_id)
        work_dir: Path | None = None

        # Resolve timeout
        timeout: int = job.metadata.get("run_timeout_sec", 1800)

        try:
            # M2.2: Build BackendManager per-job so repo_root matches project_path
            if not job.project_path:
                raise ValueError(
                    "project_path is required for job execution. "
                    "Refusing to use cwd as target — agents may modify Weave itself. "
                    "Submit jobs with --project /path/to/target."
                )
            project_root = Path(job.project_path).resolve()
            from control_plane.backend_lifecycle import BackendLifecycleService
            bls = BackendLifecycleService(backend_base_path=self.backend_base_path)
            backend_manager = bls.create_backend_manager(str(project_root))
            work_dir = bls.setup_workspace(
                backend_manager, job_id=job.id, run_id=run.id,
                risk_level=job.metadata.get("risk_level"),
            )

            # --- Non-interactive: expire old approval tickets ---
            if self.non_interactive and self.approval_repo is not None:
                self.approval_repo.expire_tickets()

            # --- Lifecycle hook: after_create ---
            hooks = BackendLifecycleService.load_project_hooks(job.project_path)
            await bls.run_hook(backend_manager, "after_create", hooks, work_dir)

            # --- Lifecycle hook: before_run ---
            await bls.run_hook(backend_manager, "before_run", hooks, work_dir)

            # --- Core execution (with task-level timeout) ---
            result_dag = await asyncio.wait_for(
                self._execute_plan_and_run(
                    job, session_id, store, work_dir, run.id, backend_manager,
                ),
                timeout=timeout,
            )

            # --- Lifecycle hook: after_run ---
            await bls.run_hook(backend_manager, "after_run", hooks, work_dir)

            # --- Summarize ---
            orchestrator = self._execution_factory.create_orchestrator(store)
            engine = self._execution_factory.create_execution_engine(
                session_id, store,
                replan_handler=lambda dag, failed_id: orchestrator.replan(
                    dag, failed_id, job.requirement,
                ),
                work_dir=work_dir,
                backend_manager=backend_manager,
                job_id=job.id,
                run_id=run.id,
            )
            summary = engine.get_execution_summary(result_dag)

            # Determine final status via lifecycle manager
            if summary.get("all_succeeded", False):
                run = self._lifecycle.mark_succeeded(run, summary)
                job_status = JobStatus.SUCCEEDED
            else:
                run = self._lifecycle.mark_failed(run, summary)
                job_status = JobStatus.FAILED

            # Transition job to final state unless externally canceled/requeued.
            current_job = self.repository.get_job(job_id)
            if current_job:
                resolved = self._lifecycle.resolve_external_status(run, current_job)
                if resolved:
                    if work_dir is not None:
                        backend_manager.preserve(job.id, run.id, reason="external_status_change")
                    return self.repository.get_run(run.id) or run

            error_msg = ""
            error_cat = ""
            if job_status == JobStatus.FAILED:
                # Collect errors from failed nodes
                failed_nodes = [
                    nid for nid, n in result_dag.nodes.items()
                    if n.status.value == "failed"
                ]
                if failed_nodes:
                    errors = [
                        f"{nid}: {result_dag.nodes[nid].error}"
                        for nid in failed_nodes
                    ]
                    error_msg = "; ".join(errors)
                    error_cat = classify_error(error_msg)

                # Must transition RUNNING -> FAILED before handle_job_failure
                self.repository.transition_job_status(
                    job_id, JobStatus.FAILED, error=error_msg, error_category=error_cat,
                )
                # NodeTimeoutError should mark run as TIMED_OUT rather than
                # merely FAILED (#360).  Note: `timeout` here is the run-level
                # timeout (default 1800s); the actual node timeout (e.g. 300s)
                # is recorded in the node error message.  Called after
                # transition to avoid state inconsistency if transition throws.
                if error_cat == "timeout" and failed_nodes:
                    run = self._lifecycle.mark_timed_out(run, timeout)
                if work_dir is not None:
                    bls.preserve(backend_manager, job.id, run.id, reason=error_cat or "failed")
                job = self.repository.get_job(job_id)
                assert job is not None
                # Apply retry policy: FAILED -> QUEUED (retry) or DEAD_LETTER
                job = await self._job_lifecycle.handle_job_failure(
                    job, error=error_msg, error_category=error_cat,
                )
            else:
                self.repository.transition_job_status(
                    job_id, job_status, error=error_msg, error_category=error_cat,
                )
                if work_dir is not None:
                    bls.cleanup(backend_manager, job.id, run.id)

        except asyncio.TimeoutError:
            # --- Timeout handling ---
            current_job = self.repository.get_job(job_id)
            if current_job and current_job.status == JobStatus.CANCELED:
                run = self._lifecycle.mark_canceled(run, "Job canceled during execution")
                return self.repository.get_run(run.id) or run

            run = self._lifecycle.mark_timed_out(run, timeout)

            # Must transition RUNNING -> FAILED before handle_job_failure
            # (FAILED -> QUEUED/DEAD_LETTER is legal)
            self.repository.transition_job_status(
                job_id, JobStatus.FAILED,
                error="Job execution timed out", error_category="timeout",
            )
            job = self.repository.get_job(job_id)
            assert job is not None
            job = await self._job_lifecycle.handle_job_failure(
                job, error="Job execution timed out", error_category="timeout",
            )
            if work_dir is not None:
                bls.preserve(backend_manager, job.id, run.id, reason="timeout")

        except asyncio.CancelledError:
            run = self._lifecycle.mark_canceled(run, "Run coroutine canceled")
            current_job = self.repository.get_job(job_id)
            if current_job and current_job.status == JobStatus.RUNNING:
                self.repository.transition_job_status(
                    job_id, JobStatus.CANCELED, error="Run canceled", error_category="tool_blocked",
                )
            if work_dir is not None:
                bls.preserve(backend_manager, job.id, run.id, reason="canceled")
            return self.repository.get_run(run.id) or run

        except PendingApprovalError as exc:
            # --- Approval required: pause execution, do NOT cleanup/preserve ---
            run = self._lifecycle.mark_pending_approval(run, exc.ticket_id)
            # Re-raise so Worker can enter PENDING_APPROVAL poll loop.
            raise

        except Exception as exc:
            # --- Unexpected error handling ---
            current_job = self.repository.get_job(job_id)
            if current_job and current_job.status == JobStatus.CANCELED:
                run = self._lifecycle.mark_canceled(run, "Job canceled during execution")
                return self.repository.get_run(run.id) or run

            error_msg = f"{type(exc).__name__}: {str(exc)}\n{traceback.format_exc()}"
            error_cat = classify_error(str(exc))

            run = self._lifecycle.mark_failed(run, {"error": "execution_error", "reason": str(exc)})

            # Must transition RUNNING -> FAILED before handle_job_failure
            self.repository.transition_job_status(
                job_id, JobStatus.FAILED, error=error_msg, error_category=error_cat,
            )
            job = self.repository.get_job(job_id)
            assert job is not None
            job = await self._job_lifecycle.handle_job_failure(
                job, error=error_msg, error_category=error_cat,
            )
            if work_dir is not None:
                bls.preserve(backend_manager, job.id, run.id, reason=error_cat)

        finally:
            self._running_tasks.pop(job_id, None)
            # Generate standardized job result artifact
            try:
                final_job = self.repository.get_job(job_id)
                final_run = self.repository.get_run(run.id)
                if final_job and final_run:
                    summary = final_run.dag_result or {}
                    self._result_writer.generate(final_job, final_run, summary)
            except Exception:
                pass  # Job result generation must not mask original error

        return self.repository.get_run(run.id) or run

    # -- Backward-compat proxies for JobLifecycleManager (#177 PR6) ----------

    async def get_job_status(self, job_id: str) -> dict[str, Any]:
        """Proxy to JobLifecycleManager.get_job_status."""
        return await self._job_lifecycle.get_job_status(job_id)

    async def list_jobs(self, status: JobStatus | None = None) -> list[Job]:
        """Proxy to JobLifecycleManager.list_jobs."""
        return await self._job_lifecycle.list_jobs(status)

    async def cancel_job(self, job_id: str) -> Job:
        """Proxy to JobLifecycleManager.cancel_job."""
        return await self._job_lifecycle.cancel_job(job_id)

    async def handle_job_failure(
        self,
        job: Job,
        error: str,
        error_category: str = "unknown",
    ) -> Job:
        """Proxy to JobLifecycleManager.handle_job_failure."""
        return await self._job_lifecycle.handle_job_failure(job, error, error_category)

    async def resume_after_approval(self, job_id: str, ticket_id: str) -> Run | None:
        """Proxy to JobLifecycleManager.resume_after_approval."""
        return await self._job_lifecycle.resume_after_approval(job_id, ticket_id)

    async def abort_after_rejection(self, job_id: str, ticket_id: str, reason: str = "") -> Job:
        """Proxy to JobLifecycleManager.abort_after_rejection."""
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

    async def _run_before_hooks(self, ctx: Any) -> None:
        """Run all before_execution hooks. Errors are logged, never raised."""
        for hook in self._hooks:
            try:
                await hook.before_execution(ctx)
            except Exception as exc:
                logger.debug("Hook %s.before_execution failed: %s", type(hook).__name__, exc)

    async def _run_after_hooks(self, ctx: Any, result_dag: Any) -> None:
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
        backend_manager: Any | None = None,
    ) -> Any:
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
        )
        result_dag = await engine.execute(dag)

        # After-hooks: impact verification, memory storage
        await self._run_after_hooks(ctx, result_dag)

        # Merge hook metadata into job
        if ctx.metadata:
            job.metadata.update(ctx.metadata)
            self.repository.update_job(job)

        return result_dag
