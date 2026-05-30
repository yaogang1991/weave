"""
LearningScheduler — Periodic background analysis trigger and on-demand analysis.

Manages when analysis runs (based on interval and minimum sample count),
stores analysis state, and provides status information.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config import LearningConfig
from learning.analyzer import LearningAnalyzer
from learning.optimizer import LearningOptimizer

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class LearningScheduler:
    """
    Manages learning analysis lifecycle: when to run, how to run,
    and tracking results.
    """

    def __init__(
        self,
        config: LearningConfig,
        analyzer: LearningAnalyzer,
        optimizer: LearningOptimizer,
    ) -> None:
        self.config = config
        self.analyzer = analyzer
        self.optimizer = optimizer
        self._state_path = Path(config.base_path) / ".last_analysis"
        self._lock = threading.Lock()

    def maybe_run_analysis(self) -> dict[str, Any] | None:
        """Run analysis if enough time has passed and enough samples exist.

        Returns analysis results dict if analysis was run, None if skipped.
        Thread-safe: concurrent calls will not trigger duplicate analyses.
        """
        if not self.config.enabled:
            return None

        if not self._lock.acquire(blocking=False):
            logger.debug("Skipping analysis: another analysis is already running")
            return None

        try:
            return self._maybe_run_analysis_inner()
        finally:
            self._lock.release()

    def _maybe_run_analysis_inner(self) -> dict[str, Any] | None:
        """Internal implementation of maybe_run_analysis (called under lock)."""
        # Check interval
        last = self._get_last_analysis_time()
        if last is not None:
            hours_since = (_utc_now() - last).total_seconds() / 3600
            if hours_since < self.config.analysis_interval_hours:
                return None

        # Check minimum sample count
        metrics = None
        if self.analyzer.metrics_collector is not None:
            try:
                metrics = self.analyzer.metrics_collector.collect()
            except Exception as e:
                logger.warning("Metrics collection failed: %s", e)
        total_jobs = (metrics or {}).get("summary", {}).get("total", 0)
        if total_jobs < self.config.min_samples:
            logger.debug(
                "Skipping analysis: %d jobs < min_samples %d",
                total_jobs, self.config.min_samples,
            )
            return None

        return self.run_analysis()

    def run_analysis(self) -> dict[str, Any]:
        """Run full analyze → optimize → store cycle."""
        logger.info("Starting learning analysis...")
        start_time = _utc_now()

        # 1. Analyze
        insights = self.analyzer.analyze()

        # 1b. Enforce max_insights cap
        if len(insights) > self.config.max_insights:
            logger.info(
                "Truncating %d insights to %d",
                len(insights), self.config.max_insights,
            )
            insights = insights[: self.config.max_insights]

        # 2. Optimize (convert insights to memories)
        memories = self.optimizer.optimize(
            insights,
            confidence_threshold=self.config.confidence_threshold,
        )

        # 3. Save state
        end_time = _utc_now()
        duration = (end_time - start_time).total_seconds()

        result = {
            "started_at": start_time.isoformat(),
            "completed_at": end_time.isoformat(),
            "duration_sec": round(duration, 2),
            "total_insights": len(insights),
            "stored_memories": len(memories),
            "insights": [
                {
                    "id": i.id,
                    "category": i.category.value,
                    "type": i.insight_type.value,
                    "description": i.description[:200],
                    "confidence": i.confidence,
                    "impact": i.impact,
                }
                for i in insights
            ],
        }

        self._save_state(result)
        logger.info(
            "Analysis complete: %d insights, %d memories stored",
            len(insights), len(memories),
        )
        return result

    def get_status(self) -> dict[str, Any]:
        """Return current learning system status."""
        state = self._load_state()

        status: dict[str, Any] = {
            "enabled": self.config.enabled,
            "analysis_interval_hours": self.config.analysis_interval_hours,
            "confidence_threshold": self.config.confidence_threshold,
            "last_analysis": None,
            "last_insight_count": 0,
        }

        if state:
            status["last_analysis"] = state.get("completed_at")
            status["last_insight_count"] = state.get("total_insights", 0)
            status["last_memory_count"] = state.get("stored_memories", 0)

        return status

    # -- State persistence --

    def _get_last_analysis_time(self) -> datetime | None:
        state = self._load_state()
        if not state:
            return None
        ts = state.get("completed_at")
        if ts:
            try:
                return datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                return None
        return None

    def _load_state(self) -> dict[str, Any] | None:
        if not self._state_path.exists():
            return None
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return data
        except Exception as e:
            logger.debug("Failed to load analysis state: %s", e)
            return None

    def _save_state(self, result: dict[str, Any]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.parent / ".last_analysis.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, default=str, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(self._state_path))
        except Exception:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            logger.error("Failed to save analysis state", exc_info=True)
            raise
