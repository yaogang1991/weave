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
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow imports from project root (core/, orchestrator/, agent/, session/, ...)
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import LLMConfig
from core.dag_engine import DAGExecutionEngine
from core.agent_registry import AgentRegistry
from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
from agent.agent_pool import AgentPool
from session.store import SessionStore
from tools.registry import ToolRegistry
from guardrails.policy import Guardrails, GuardrailPolicy, PermissionMode, PersonalGuardrails
from core.models import PersonalGuardrailPolicy
from evaluator.engine import EvaluatorEngine

from control_plane.models import Job, Run, JobStatus, RunStatus, RetryPolicy
from control_plane.repository import JobRepository


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
    return "unknown"


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
        agent_timeout: int = 120,
        max_context_tokens: int = 100000,
        artifact_path: str = "./data/artifacts",
        event_store_path: str = "./data/events",
        max_iterations: int = 50,
        policy: GuardrailPolicy | None = None,
        default_backend: str = "local",
        backend_base_path: str = "./data/backends",
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

        # M2.1/M2.2: Backend manager for isolated execution
        from backend.lifecycle import BackendManager

        self.backend_manager = BackendManager(
            default_backend=default_backend,
            base_path=backend_base_path,
        )

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

        # Acquire lease then transition to RUNNING
        # Required path: QUEUED -> LEASED -> RUNNING
        self.repository.acquire_lease(job_id, "run_service")
        self.repository.transition_job_status(job_id, JobStatus.RUNNING)
        job = self.repository.get_job(job_id)  # refresh

        # Create session
        session_id = str(uuid.uuid4())
        store = SessionStore(self.event_store_path)
        store.create_session(session_id, "harness_run")

        # Create Run record
        run = self.repository.create_run(job_id, session_id)

        # Resolve timeout
        timeout: int = job.metadata.get("run_timeout_sec", 600)

        try:
            # --- Core execution (with task-level timeout) ---
            result_dag = await asyncio.wait_for(
                self._execute_plan_and_run(job, session_id, store),
                timeout=timeout,
            )

            # --- Summarize ---
            orchestrator = self._create_orchestrator(store)
            engine = self._create_execution_engine(
                session_id, store,
                replan_handler=lambda dag, failed_id: orchestrator.replan(
                    dag, failed_id, job.requirement,
                ),
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

            # Transition job to final state
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

        except asyncio.TimeoutError:
            # --- Timeout handling ---
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

        except Exception as exc:
            # --- Unexpected error handling ---
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
    # Internal helpers
    # ------------------------------------------------------------------

    async def _execute_plan_and_run(
        self,
        job: Job,
        session_id: str,
        store: SessionStore,
    ) -> Any:
        """
        Plan a DAG and execute it.

        Returns the executed DAG object.
        """
        # 1. Create orchestrator and plan DAG
        orchestrator = self._create_orchestrator(store)
        project_context = {"project_path": job.project_path} if job.project_path else None
        dag = await orchestrator.plan(
            requirement=job.requirement,
            project_context=project_context,
        )

        # 2. Create execution engine (with replan handler) and execute
        engine = self._create_execution_engine(
            session_id, store,
            replan_handler=lambda dag_ref, failed_id: orchestrator.replan(
                dag_ref, failed_id, job.requirement,
            ),
        )
        result_dag = await engine.execute(dag)

        return result_dag

    def _create_orchestrator(self, store: SessionStore) -> IntelligentOrchestrator:
        """Build an IntelligentOrchestrator with default registries."""
        registry = AgentRegistry()
        return IntelligentOrchestrator(
            llm_config=self.llm_config,
            session_store=store,
            agent_registry=registry,
        )

    def _create_execution_engine(
        self,
        session_id: str,
        store: SessionStore,
        replan_handler: Any | None = None,
    ) -> DAGExecutionEngine:
        """Build a DAGExecutionEngine with agent pool, failure handler, and optional replan handler."""
        registry = AgentRegistry()
        tool_registry = ToolRegistry()

        # Default guardrails: accept edits, auto-approve reads
        if getattr(self, "policy", None) is not None:
            policy = self.policy
        else:
            policy = GuardrailPolicy(
                mode=PermissionMode.ACCEPT_EDITS,
                auto_approve_read=True,
                max_iterations=self.max_iterations,
            )

        # 如果 policy 是 PersonalGuardrailPolicy，使用 PersonalGuardrails
        if isinstance(policy, PersonalGuardrailPolicy):
            guardrails = PersonalGuardrails(policy, tool_registry)
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
        )

        # Orchestrator for failure handling
        orchestrator = self._create_orchestrator(store)

        # Evaluator for quality gates
        evaluator = EvaluatorEngine(session_store=store)

        return DAGExecutionEngine(
            agent_executor=pool.get_executor(session_id),
            failure_handler=orchestrator.adapt_to_failure,
            replan_handler=replan_handler,
            max_parallel=self.max_parallel,
            evaluator=evaluator,
            artifact_path=self.artifact_path,
        )
