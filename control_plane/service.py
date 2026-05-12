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

from core.config import LLMConfig, WatchdogConfig
from core.dag_engine import DAGExecutionEngine
from core.agent_registry import AgentRegistry
from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
from agent.agent_pool import AgentPool
from session.store import SessionStore
from tools.registry import ToolRegistry
from guardrails.policy import Guardrails, GuardrailPolicy, PermissionMode, PersonalGuardrails
from core.models import EventType, PersonalGuardrailPolicy
from core.exceptions import PendingApprovalError
from evaluator.engine import EvaluatorEngine

from control_plane.models import Job, Run, JobStatus, RunStatus, RetryPolicy
from control_plane.repository import JobRepository

logger = logging.getLogger(__name__)


# ============================================================================
# Error classification helper
# ============================================================================


def _classify_error(error: str) -> str:
    """Classify an error string into a canonical category."""
    lowered = error.lower()
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    if "evaluation failed" in lowered or "eval_" in lowered:
        return "eval_failed"
    if "guardrail" in lowered or "blocked" in lowered or "permission" in lowered:
        return "tool_blocked"
    if "watchdog" in lowered or "killed by watchdog" in lowered:
        return "watchdog"
    return "unknown"


def _default_watchdog_config() -> WatchdogConfig:
    """Create a WatchdogConfig with sensible defaults."""
    return WatchdogConfig()


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

        # M2.1/M2.2: Backend manager for isolated execution
        from backend.lifecycle import BackendManager

        self.default_backend = default_backend
        self.backend_base_path = backend_base_path

    # ------------------------------------------------------------------
    # Public API — Job lifecycle
    # ------------------------------------------------------------------

    async def submit_job(
        self,
        requirement: str,
        project_path: str | None = None,
        timeout: int = 600,
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
        store.create_session(session_id, "harness_run")

        # Create Run record
        run = self.repository.create_run(job_id, session_id)
        work_dir: Path | None = None

        # Resolve timeout
        timeout: int = job.metadata.get("run_timeout_sec", 600)

        try:
            # M2.2: Build BackendManager per-job so repo_root matches project_path
            project_root = Path(job.project_path).resolve() if job.project_path else Path.cwd().resolve()
            from backend.lifecycle import BackendManager
            from backend.base import WorkspaceIsolation, ExecutionSandbox
            from core.config import HarnessConfig
            harness_config = HarnessConfig.from_env()
            # Select sandbox: use config value, but fall back to LOCAL if unavailable
            sandbox_type = ExecutionSandbox(
                harness_config.sandbox.runtime
                if harness_config.sandbox.runtime in ("local", "docker")
                else "local"
            )
            backend_manager = BackendManager(
                workspace=WorkspaceIsolation(harness_config.default_backend),
                sandbox=sandbox_type,
                repo_root=str(project_root),
                base_path=self.backend_base_path,
                workspace_by_risk=harness_config.risk_backend_map,
                cleanup_policy=harness_config.cleanup_policy,
            )
            work_dir = backend_manager.setup(
                job_id=job.id,
                run_id=run.id,
                risk_level=job.metadata.get("risk_level"),
            )

            # --- Non-interactive: expire old approval tickets ---
            if self.non_interactive and self.approval_repo is not None:
                self.approval_repo.expire_tickets()

            # --- Lifecycle hook: after_create ---
            hooks = self._load_project_hooks(job.project_path)
            if hooks.get("after_create"):
                await backend_manager.execute_hook(
                    "after_create", hooks["after_create"], work_dir,
                )

            # --- Lifecycle hook: before_run ---
            if hooks.get("before_run"):
                await backend_manager.execute_hook(
                    "before_run", hooks["before_run"], work_dir,
                )

            # --- Core execution (with task-level timeout) ---
            result_dag = await asyncio.wait_for(
                self._execute_plan_and_run(job, session_id, store, work_dir, run.id),
                timeout=timeout,
            )

            # --- Lifecycle hook: after_run ---
            if hooks.get("after_run"):
                await backend_manager.execute_hook(
                    "after_run", hooks["after_run"], work_dir,
                )

            # --- Summarize ---
            orchestrator = self._create_orchestrator(store)
            engine = self._create_execution_engine(
                session_id, store,
                replan_handler=lambda dag, failed_id: orchestrator.replan(
                    dag, failed_id, job.requirement,
                ),
                work_dir=work_dir,
            )
            summary = engine.get_execution_summary(result_dag)

            # Determine final status
            if summary.get("all_succeeded", False):
                run.status = RunStatus.SUCCEEDED
                job_status = JobStatus.SUCCEEDED
            else:
                run.status = RunStatus.FAILED
                job_status = JobStatus.FAILED

            run.dag_result = summary
            run.completed_at = _utc_now()

            # Persist run
            self.repository.update_run(run)

            # Transition job to final state unless externally canceled/requeued.
            current_job = self.repository.get_job(job_id)
            if current_job and current_job.status != JobStatus.RUNNING:
                run.status = (
                    RunStatus.CANCELED
                    if current_job.status == JobStatus.CANCELED
                    else RunStatus.FAILED
                )
                run.completed_at = _utc_now()
                self.repository.update_run(run)
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
                    error_cat = _classify_error(error_msg)

                # Must transition RUNNING -> FAILED before handle_job_failure
                self.repository.transition_job_status(
                    job_id, JobStatus.FAILED, error=error_msg, error_category=error_cat,
                )
                if work_dir is not None:
                    try:
                        backend_manager.preserve(job.id, run.id, reason=error_cat or "failed")
                    except Exception:
                        pass  # Backend cleanup failure must not mask original error
                job = self.repository.get_job(job_id)
                assert job is not None
                # Apply retry policy: FAILED -> QUEUED (retry) or DEAD_LETTER
                job = await self.handle_job_failure(
                    job, error=error_msg, error_category=error_cat,
                )
            else:
                self.repository.transition_job_status(
                    job_id, job_status, error=error_msg, error_category=error_cat,
                )
                if work_dir is not None:
                    try:
                        backend_manager.cleanup(job.id, run.id)
                    except Exception:
                        pass  # Backend cleanup failure must not mask original error

        except asyncio.TimeoutError:
            # --- Timeout handling ---
            current_job = self.repository.get_job(job_id)
            if current_job and current_job.status == JobStatus.CANCELED:
                run.status = RunStatus.CANCELED
                run.completed_at = _utc_now()
                run.dag_result = {"error": "canceled", "reason": "Job canceled during execution"}
                self.repository.update_run(run)
                return self.repository.get_run(run.id) or run

            run.status = RunStatus.TIMED_OUT
            run.completed_at = _utc_now()
            run.dag_result = {"error": "timeout", "reason": f"Exceeded {timeout}s"}
            self.repository.update_run(run)

            # Must transition RUNNING -> FAILED before handle_job_failure
            # (FAILED -> QUEUED/DEAD_LETTER is legal)
            self.repository.transition_job_status(
                job_id, JobStatus.FAILED,
                error="Job execution timed out", error_category="timeout",
            )
            job = self.repository.get_job(job_id)
            assert job is not None
            job = await self.handle_job_failure(
                job, error="Job execution timed out", error_category="timeout",
            )
            if work_dir is not None:
                try:
                    backend_manager.preserve(job.id, run.id, reason="timeout")
                except Exception:
                    pass

        except asyncio.CancelledError:
            run.status = RunStatus.CANCELED
            run.completed_at = _utc_now()
            run.dag_result = {"error": "canceled", "reason": "Run coroutine canceled"}
            self.repository.update_run(run)
            current_job = self.repository.get_job(job_id)
            if current_job and current_job.status == JobStatus.RUNNING:
                self.repository.transition_job_status(
                    job_id, JobStatus.CANCELED, error="Run canceled", error_category="tool_blocked",
                )
            if work_dir is not None:
                try:
                    backend_manager.preserve(job.id, run.id, reason="canceled")
                except Exception:
                    pass
            return self.repository.get_run(run.id) or run

        except PendingApprovalError as exc:
            # --- Approval required: pause execution, do NOT cleanup/preserve ---
            run.status = RunStatus.PENDING_APPROVAL
            run.dag_result = {
                "status": "pending_approval",
                "ticket_id": exc.ticket_id,
            }
            self.repository.update_run(run)
            # Re-raise so Worker can enter PENDING_APPROVAL poll loop.
            raise

        except Exception as exc:
            # --- Unexpected error handling ---
            current_job = self.repository.get_job(job_id)
            if current_job and current_job.status == JobStatus.CANCELED:
                run.status = RunStatus.CANCELED
                run.completed_at = _utc_now()
                run.dag_result = {"error": "canceled", "reason": "Job canceled during execution"}
                self.repository.update_run(run)
                return self.repository.get_run(run.id) or run

            error_msg = f"{type(exc).__name__}: {str(exc)}\n{traceback.format_exc()}"
            error_cat = _classify_error(str(exc))

            run.status = RunStatus.FAILED
            run.completed_at = _utc_now()
            run.dag_result = {"error": "execution_error", "reason": str(exc)}
            self.repository.update_run(run)

            # Must transition RUNNING -> FAILED before handle_job_failure
            self.repository.transition_job_status(
                job_id, JobStatus.FAILED, error=error_msg, error_category=error_cat,
            )
            job = self.repository.get_job(job_id)
            assert job is not None
            job = await self.handle_job_failure(
                job, error=error_msg, error_category=error_cat,
            )
            if work_dir is not None:
                try:
                    backend_manager.preserve(job.id, run.id, reason=error_cat)
                except Exception:
                    pass

        finally:
            self._running_tasks.pop(job_id, None)
            # Generate standardized job result artifact
            try:
                final_job = self.repository.get_job(job_id)
                final_run = self.repository.get_run(run.id)
                if final_job and final_run:
                    summary = final_run.dag_result or {}
                    self._generate_job_result(final_job, final_run, summary)
            except Exception:
                pass  # Job result generation must not mask original error

        return self.repository.get_run(run.id) or run

    async def get_job_status(self, job_id: str) -> dict[str, Any]:
        """
        Return a comprehensive status dict for *job_id*.

        Includes the job fields plus a ``runs`` list with all
        execution attempts for this job.
        """
        job = self.repository.get_job(job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")

        runs = self.repository.list_runs_by_job(job_id)
        return {
            "job_id": job.id,
            "status": job.status.value,
            "attempt": job.attempt,
            "last_error": job.last_error,
            "error_category": job.error_category,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
            "requirement": job.requirement,
            "project_path": job.project_path,
            "runs": [
                {
                    "run_id": r.id,
                    "status": r.status.value,
                    "session_id": r.session_id,
                    "dag_result": r.dag_result,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                }
                for r in runs
            ],
        }

    async def list_jobs(self, status: JobStatus | None = None) -> list[Job]:
        """Return all jobs, optionally filtered by status."""
        return self.repository.list_jobs(status=status)

    async def cancel_job(self, job_id: str) -> Job:
        """
        Cancel a job if it is in a cancellable state.

        Only jobs in QUEUED, LEASED, or RUNNING can be cancelled.
        Raises ValueError if the transition is illegal (e.g. already terminal).
        """
        job = self.repository.get_job(job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")

        if not job.is_active():
            raise ValueError(
                f"Cannot cancel job {job_id}: already in terminal state {job.status.value}"
            )

        return self.repository.transition_job_status(job_id, JobStatus.CANCELED)

    # ------------------------------------------------------------------
    # Retry logic
    # ------------------------------------------------------------------

    async def handle_job_failure(
        self,
        job: Job,
        error: str,
        error_category: str = "unknown",
    ) -> Job:
        """
        Handle a failed job by either queuing for retry or sending to dead-letter.

        - If ``attempt < max_attempts``: transition to QUEUED, increment attempt,
          clear previous error state.
        - Otherwise: transition to DEAD_LETTER.

        Args:
            job: The failed Job instance.
            error: Error message to record.
            error_category: Canonical error category
                            (timeout / eval_failed / tool_blocked / unknown).
        """
        max_attempts = job.retry_policy.max_attempts

        if job.attempt < max_attempts:
            # Retry: FAILED -> QUEUED (bump_attempt happens in repository)
            return self.repository.transition_job_status(
                job.id, JobStatus.QUEUED, error=error, error_category=error_category
            )
        else:
            # Exhausted retries: FAILED -> DEAD_LETTER
            return self.repository.transition_job_status(
                job.id, JobStatus.DEAD_LETTER, error=error, error_category=error_category
            )

    # ------------------------------------------------------------------
    # Approval resume / abort
    # ------------------------------------------------------------------

    async def resume_after_approval(self, job_id: str, ticket_id: str) -> Run | None:
        """Resume a job after an approval decision.

        For PENDING_APPROVAL jobs: the Worker's poll loop detects the approval
        and resumes execution itself — no action needed here.

        For legacy RUNNING/LEASED jobs: re-queue for workers.
        """
        job = self.repository.get_job(job_id)
        if not job:
            return None

        # PENDING_APPROVAL jobs: the Worker's poll loop detects approved tickets
        # and resumes execution. Do NOT re-queue — that races with the poll loop.
        # If no worker is active, orphan recovery will handle it on next startup.
        if job.status == JobStatus.PENDING_APPROVAL:
            self._emit_event("approval_resumed_poll", job_id, {
                "ticket_id": ticket_id,
                "job_id": job_id,
                "message": "Worker poll loop will detect approval and resume",
            })
            runs = self.repository.list_runs_by_job(job_id)
            active_runs = [r for r in runs if r.status in {RunStatus.RUNNING, RunStatus.PENDING_APPROVAL}]
            return active_runs[-1] if active_runs else None

        # Legacy path for RUNNING/LEASED jobs
        runs = self.repository.list_runs_by_job(job_id)
        active_runs = [r for r in runs if r.status == RunStatus.RUNNING]
        if not active_runs:
            return None

        run = active_runs[-1]

        if job.status in {JobStatus.RUNNING, JobStatus.LEASED}:
            job.status = JobStatus.QUEUED
            job.lease_owner = None
            job.lease_expires_at = None
            job.last_error = ""
            job.error_category = ""
            job = self.repository.update_job(job)
        elif job.status != JobStatus.QUEUED:
            return None

        self._emit_event("approval_resumed", job_id, {
            "ticket_id": ticket_id,
            "run_id": run.id,
            "job_id": job_id,
            "job_status": job.status.value,
        })

        return run

    async def abort_after_rejection(self, job_id: str, ticket_id: str, reason: str = "") -> Job:
        """
        审批被拒绝后中止任务。

        将 job 状态推进到 failed 或 dead_letter（根据重试策略）。
        """
        job = self.repository.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        error_msg = f"Approval ticket {ticket_id} rejected"
        if reason:
            error_msg += f": {reason}"

        if job.status == JobStatus.RUNNING:
            # Stop in-flight execution first, then mark job canceled.
            running_task = self._running_tasks.get(job.id)
            if running_task and not running_task.done():
                running_task.cancel()
            job = self.repository.transition_job_status(
                job.id,
                JobStatus.CANCELED,
                error=error_msg,
                error_category="tool_blocked",
            )
        elif job.status == JobStatus.LEASED:
            # LEASED cannot transition directly to FAILED in repository rules.
            job = self.repository.transition_job_status(
                job.id,
                JobStatus.QUEUED,
                error=error_msg,
                error_category="tool_blocked",
            )
            # Clear lease metadata so workers can immediately acquire this queued job.
            job.lease_owner = None
            job.lease_expires_at = None
            job = self.repository.update_job(job)
        elif job.status == JobStatus.QUEUED:
            job = self.repository.transition_job_status(
                job.id,
                JobStatus.FAILED,
                error=error_msg,
                error_category="tool_blocked",
            )

        if job.status == JobStatus.FAILED:
            job = await self.handle_job_failure(job, error_msg, "tool_blocked")

        self._emit_event("approval_rejected_abort", job_id, {
            "ticket_id": ticket_id,
            "job_status": job.status.value,
            "reason": reason,
        })

        return job

    def _emit_event(self, event_type: str, job_id: str, details: dict[str, Any]) -> None:
        """发出结构化事件（用于日志和监控）。"""
        event: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "job_id": job_id,
            "details": details,
        }
        print(json.dumps(event), flush=True)

    def _generate_job_result(
        self,
        job: Job,
        run: Run,
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        """Generate a standardized job_result.json artifact."""
        result = {
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
        orchestrator = self._create_orchestrator(store)
        project_path = job.project_path or (str(work_dir) if work_dir else None)
        project_context = {"project_path": project_path} if project_path else None
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

        engine = self._create_execution_engine(
            session_id, store,
            replan_handler=lambda dag_ref, failed_id: orchestrator.replan(
                dag_ref, failed_id, job.requirement,
            ),
            work_dir=work_dir,
            memory_manager=ctx.memory_manager,
            job_id=job.id,
            approval_repo=self.approval_repo,
            run_id=run_id,
        )
        result_dag = await engine.execute(dag)

        # After-hooks: impact verification, memory storage
        await self._run_after_hooks(ctx, result_dag)

        # Merge hook metadata into job
        if ctx.metadata:
            job.metadata.update(ctx.metadata)
            self.repository.update_job(job)

        return result_dag

    def _load_project_hooks(self, project_path: str | None) -> dict[str, str]:
        """Load lifecycle hooks from .harness/config.yaml if present."""
        hooks: dict[str, str] = {}
        if not project_path:
            return hooks
        try:
            config_path = Path(project_path) / ".harness" / "config.yaml"
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

    def _load_project_guardrails(self, work_dir: Path | None) -> dict[str, Any]:
        """Load guardrail overrides from .harness/config.yaml if present."""
        result: dict[str, Any] = {}
        if work_dir is None:
            return result
        try:
            config_path = Path(work_dir) / ".harness" / "config.yaml"
            if config_path.exists():
                import yaml
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                gr = cfg.get("guardrails", {})
                if "permission_mode" in gr:
                    result["permission_mode"] = PermissionMode(gr["permission_mode"])
                if "auto_approve_read" in gr:
                    result["auto_approve_read"] = gr["auto_approve_read"]
                if "denied_commands" in gr:
                    result["denied_commands"] = gr["denied_commands"]
                if "allowed_tools" in gr:
                    result["allowed_tools"] = gr["allowed_tools"]
        except Exception:
            pass
        return result

    def _create_orchestrator(self, store: SessionStore) -> IntelligentOrchestrator:
        """Build an IntelligentOrchestrator with default registries."""
        registry = AgentRegistry()
        # Get learning optimizer from LearningHook (if registered)
        learning_optimizer = None
        for hook in self._hooks:
            if hasattr(hook, "optimizer"):
                learning_optimizer = hook.optimizer
                break
        return IntelligentOrchestrator(
            llm_config=self.llm_config,
            session_store=store,
            agent_registry=registry,
            llm_router=getattr(self, "llm_router", None),
            learning_optimizer=learning_optimizer,
        )

    def _create_execution_engine(
        self,
        session_id: str,
        store: SessionStore,
        replan_handler: Any | None = None,
        work_dir: Path | None = None,
        memory_manager: Any | None = None,
        job_id: str = "",
        approval_repo: Any | None = None,
        run_id: str | None = None,
    ) -> DAGExecutionEngine:
        """Build a DAGExecutionEngine with agent pool, failure handler, and optional replan handler."""
        registry = AgentRegistry()
        tool_registry = ToolRegistry(base_cwd=str(work_dir) if work_dir is not None else None)

        # Default guardrails: non-interactive → DONT_ASK + built-in tool whitelist
        if getattr(self, "policy", None) is not None:
            policy = self.policy
        else:
            project_guardrails = self._load_project_guardrails(work_dir)
            if self.non_interactive:
                default_mode = PermissionMode.DONT_ASK
                default_allowed = ["read", "write", "edit", "bash", "glob", "grep", "git"]
            else:
                default_mode = PermissionMode.ACCEPT_EDITS
                default_allowed = []
            policy = GuardrailPolicy(
                mode=project_guardrails.get("permission_mode", default_mode),
                auto_approve_read=project_guardrails.get("auto_approve_read", True),
                allowed_tools=project_guardrails.get("allowed_tools", default_allowed),
                denied_commands=project_guardrails.get("denied_commands", []),
                max_iterations=self.max_iterations,
            )

        # 如果 policy 是 PersonalGuardrailPolicy，使用 PersonalGuardrails
        if isinstance(policy, PersonalGuardrailPolicy):
            guardrails = PersonalGuardrails(
                policy,
                tool_registry,
                non_interactive=self.non_interactive,
                approval_repo=self.approval_repo,
            )
        else:
            guardrails = Guardrails(policy, tool_registry)

        pool = AgentPool(
            llm_config=self.llm_config,
            session_store=store,
            agent_registry=registry,
            tool_registry=tool_registry,
            guardrails=guardrails,
            max_iterations=self.max_iterations,
            timeout=self.agent_timeout,
            max_context_tokens=self.max_context_tokens,
            llm_router=getattr(self, "llm_router", None),
            memory_manager=memory_manager,
            job_id=job_id,
            approval_repo=approval_repo,
            run_id=run_id,
        )

        # Orchestrator for failure handling
        orchestrator = self._create_orchestrator(store)

        # Evaluator for quality gates
        evaluator = EvaluatorEngine(session_store=store)

        engine = DAGExecutionEngine(
            agent_executor=pool.get_executor(session_id),
            failure_handler=orchestrator.adapt_to_failure,
            replan_handler=replan_handler,
            max_parallel=self.max_parallel,
            evaluator=evaluator,
            artifact_path=self.artifact_path,
            work_dir=str(work_dir) if work_dir else None,
            memory_manager=memory_manager,
            session_id=session_id,
            heartbeat_interval_sec=self.watchdog_config.heartbeat_interval_sec,
            heartbeat_miss_threshold=self.watchdog_config.heartbeat_miss_threshold,
            enable_watchdog=self.watchdog_config.enabled,
            watchdog_overrides={
                agent_type: (ov.heartbeat_interval_sec, ov.heartbeat_miss_threshold)
                for agent_type, ov in self.watchdog_config.agent_overrides.items()
                if ov.heartbeat_interval_sec is not None
                and ov.heartbeat_miss_threshold is not None
            },
        )

        # Register event handler: forward DAG node events to session store
        async def _session_event_handler(event):
            event_type_map = {
                "started": EventType.WORKFLOW_STAGE_START,
                "completed": EventType.WORKFLOW_STAGE_END,
                "failed": EventType.WORKFLOW_STAGE_ERROR,
                "retrying": EventType.WORKFLOW_STAGE_START,
                "failure_decision": EventType.WORKFLOW_STAGE_ERROR,
            }
            mapped_type = event_type_map.get(event.event_type)
            if mapped_type:
                store.emit_event(
                    session_id, mapped_type,
                    {"node_id": event.node_id, **event.details},
                )

        engine.on_event(_session_event_handler)
        return engine
