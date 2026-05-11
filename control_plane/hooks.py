"""
Execution Hooks — Lifecycle callbacks for RunService.

Decouples subsystems (memory, learning, impact analysis) from the core
execution flow. Each subsystem registers hooks that run before/after
DAG execution without modifying the main _execute_plan_and_run method.

Hooks receive dependencies via constructor injection (repository, llm_config)
and per-job context via ExecutionContext.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ExecutionContext:
    """Mutable context passed through execution hooks."""

    job: Any  # Job
    session_id: str
    store: Any  # SessionStore
    work_dir: Path
    run_id: str | None = None
    memory_manager: Any | None = None
    llm_config: Any | None = None
    repository: Any | None = None
    # Hooks write arbitrary data here; merged into job.metadata after execution.
    metadata: dict[str, Any] = field(default_factory=dict)
    # Internal: hooks can stash private state for after_execution to read.
    _state: dict[str, Any] = field(default_factory=dict)


class ExecutionHook(ABC):
    """Base class for execution lifecycle hooks."""

    async def before_execution(self, ctx: ExecutionContext) -> None:
        """Called before DAG planning. Errors are logged, not raised."""

    async def after_execution(self, ctx: ExecutionContext, result_dag: Any) -> None:
        """Called after DAG execution. Errors are logged, not raised."""


# ============================================================================
# MemoryHook
# ============================================================================


class MemoryHook(ExecutionHook):
    """Create a per-job MemoryManager and attach it to the context.

    Service-level maintenance runs once; per-job creation is lightweight.
    """

    def __init__(self) -> None:
        self._service_memory: Any | None = None
        self._maintenance_done = False
        self._maintenance_lock = threading.Lock()

    def _run_maintenance_once(self, mm: Any) -> None:
        if self._maintenance_done:
            return
        with self._maintenance_lock:
            if self._maintenance_done:
                return
            try:
                mm.run_maintenance()
            except Exception as exc:
                logger.debug("Memory maintenance skipped: %s", exc)
            self._maintenance_done = True

    async def before_execution(self, ctx: ExecutionContext) -> None:
        try:
            from core.config import HarnessConfig
            from memory.manager import MemoryManager
            config = HarnessConfig.from_env()
            if config.memory.enabled:
                ctx.memory_manager = MemoryManager(
                    config=config.memory, session_store=ctx.store,
                )
                self._run_maintenance_once(ctx.memory_manager)
        except Exception as exc:
            logger.debug("MemoryHook: per-job manager creation skipped: %s", exc)


# ============================================================================
# LearningHook
# ============================================================================


class LearningHook(ExecutionHook):
    """Trigger learning analysis if due.

    Exposes `optimizer` for RunService to inject into Orchestrator
    so planning hints are informed by past learnings.
    """

    def __init__(self, repository: Any | None = None) -> None:
        self.optimizer: Any | None = None
        self._scheduler: Any | None = None
        self._repository = repository
        self._init()

    def _init(self) -> None:
        try:
            from core.config import HarnessConfig
            from memory.manager import MemoryManager
            from monitoring.metrics import MetricsCollector
            from learning.analyzer import LearningAnalyzer
            from learning.optimizer import LearningOptimizer
            from learning.scheduler import LearningScheduler
            from control_plane.repository import JobRepository

            config = HarnessConfig.from_env()
            if not config.learning.enabled:
                return
            mm = MemoryManager(config.memory) if config.memory.enabled else None
            # Use injected repository (from RunService), not a new default one
            repo = self._repository or JobRepository()
            metrics_collector = MetricsCollector(repo)
            analyzer = LearningAnalyzer(metrics_collector, mm)
            self.optimizer = LearningOptimizer(mm)
            self._scheduler = LearningScheduler(config.learning, analyzer, self.optimizer)
        except Exception as exc:
            logger.debug("LearningHook init skipped: %s", exc)

    async def before_execution(self, ctx: ExecutionContext) -> None:
        if self._scheduler is None:
            return
        try:
            self._scheduler.maybe_run_analysis()
        except Exception:
            pass


# ============================================================================
# ImpactHook
# ============================================================================


class ImpactHook(ExecutionHook):
    """Predict impact before execution; verify changes after.

    Binds memory_manager from per-job context so historical impact
    predictions can be reused.
    """

    def __init__(self, llm_config: Any | None = None) -> None:
        self._llm_config = llm_config
        self._coverage_threshold: float = 0.7
        self._max_predicted_files: int = 50
        self._confidence_threshold: float = 0.5
        self._init()

    def _init(self) -> None:
        try:
            from core.config import HarnessConfig
            config = HarnessConfig.from_env()
            if not config.impact.enabled:
                return
            self._coverage_threshold = config.impact.coverage_threshold
            self._max_predicted_files = config.impact.max_predicted_files
            self._confidence_threshold = config.impact.confidence_threshold
        except Exception as exc:
            logger.debug("ImpactHook init skipped: %s", exc)

    def _make_predictor(self, memory_manager: Any | None) -> Any:
        from analysis.impact_predictor import ImpactPredictor
        return ImpactPredictor(
            llm_config=self._llm_config,
            memory_manager=memory_manager,
            max_predicted_files=self._max_predicted_files,
            confidence_threshold=self._confidence_threshold,
        )

    async def before_execution(self, ctx: ExecutionContext) -> None:
        if not ctx.work_dir:
            return
        try:
            predictor = self._make_predictor(ctx.memory_manager)
            impact_project_path = (
                ctx.job.project_path
                if ctx.job.project_path
                else str(ctx.work_dir)
            )
            impact_scope = await predictor.predict(
                requirement=ctx.job.requirement,
                project_path=impact_project_path,
            )
            from analysis.change_verifier import ChangeVerifier
            verifier = ChangeVerifier(project_path=str(ctx.work_dir))
            before_snapshot = verifier.capture_snapshot()

            ctx._state["impact_scope"] = impact_scope
            ctx._state["before_snapshot"] = before_snapshot
            ctx._state["impact_project_path"] = impact_project_path

            ctx.metadata["impact_scope_id"] = impact_scope.id
            ctx.metadata["predicted_files"] = impact_scope.predicted_files
        except Exception:
            pass

    async def after_execution(self, ctx: ExecutionContext, result_dag: Any) -> None:
        impact_scope = ctx._state.get("impact_scope")
        before_snapshot = ctx._state.get("before_snapshot")
        if not impact_scope or not ctx.work_dir:
            return
        if before_snapshot is None:
            logger.info("Skipping impact verification: no baseline snapshot")
            return
        try:
            from analysis.change_verifier import ChangeVerifier
            from core.models import MemoryType, MemoryScope

            impact_project_path = ctx._state["impact_project_path"]
            verifier = ChangeVerifier(
                project_path=str(ctx.work_dir),
                coverage_threshold=self._coverage_threshold,
            )
            verification = verifier.verify(impact_scope, before_snapshot)

            if ctx.memory_manager:
                ctx.memory_manager.store_learning(
                    agent_type="impact_analyzer",
                    content=(
                        f"Impact prediction for "
                        f"'{ctx.job.requirement[:100]}': "
                        f"coverage={verification.coverage:.2f}, "
                        f"accuracy={verification.prediction_accuracy:.2f}, "
                        f"unexpected={len(verification.unexpected_files)}"
                    ),
                    memory_type=MemoryType.EXPERIENCE,
                    scope=MemoryScope.GLOBAL,
                    keywords=[
                        "impact_analysis", "prediction",
                        impact_scope.risk_level.value,
                    ],
                    metadata={
                        "predicted_files": impact_scope.predicted_files[:20],
                        "project_path": impact_project_path,
                        "confidence": verification.prediction_accuracy,
                    },
                )

            ctx.metadata["verification_coverage"] = verification.coverage
            ctx.metadata["verification_passes"] = verification.passes

            self._persist_record(ctx, impact_scope, verification)
        except Exception:
            pass

    def _persist_record(
        self, ctx: ExecutionContext, impact_scope: Any, verification: Any,
    ) -> None:
        try:
            from core.config import HarnessConfig
            cfg = HarnessConfig.from_env().impact
            record_dir = Path(cfg.base_path)
            record_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "job_id": ctx.job.id,
                "requirement": ctx.job.requirement[:200],
                "project_path": ctx.job.project_path,
                "predicted_files": impact_scope.predicted_files[:20],
                "risk_level": impact_scope.risk_level.value,
                "verification_coverage": verification.coverage,
                "verification_accuracy": verification.prediction_accuracy,
                "verification_passes": verification.passes,
                "unexpected_files": verification.unexpected_files[:10],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            uid = ctx.run_id[:8] if ctx.run_id else uuid.uuid4().hex[:8]
            path = record_dir / f"impact_{ts}_{uid}.json"
            path.write_text(
                json.dumps(record, indent=2, default=str, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass
