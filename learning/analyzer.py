"""
LearningAnalyzer — Analyze execution history to extract patterns and insights.

Uses MetricsCollector for aggregate statistics and MemoryManager for
per-task experience data. Produces LearningInsight objects that can be
optimized into actionable memories by LearningOptimizer.
"""

from __future__ import annotations

import logging
from typing import Any

from core.models import (
    LearningInsight,
    LearningCategory,
    InsightType,
    MemoryType,
)
from memory.manager import MemoryManager

logger = logging.getLogger(__name__)


class LearningAnalyzer:
    """
    Analyze execution patterns from metrics and memory to generate insights.

    Each analysis examines:
    - Failure patterns (recurring error categories, high-failure agents)
    - Success patterns (what works well)
    - Agent performance (per-agent success rates, durations)
    - Planning quality (DAG structure vs outcomes)
    """

    def __init__(
        self,
        metrics_collector: Any | None = None,
        memory_manager: MemoryManager | None = None,
    ) -> None:
        self.metrics_collector = metrics_collector
        self.memory_manager = memory_manager

    def analyze(self) -> list[LearningInsight]:
        """Run all analyses and return combined insights."""
        insights: list[LearningInsight] = []

        # Snapshot metrics once for consistency across all analyzers
        metrics = self._get_metrics()
        experiences = self._get_experiences()

        insights.extend(self._analyze_failure_patterns(metrics))
        insights.extend(self._analyze_success_patterns(metrics, experiences))
        insights.extend(self._analyze_agent_performance(experiences))
        insights.extend(self._analyze_planning_quality(metrics))

        logger.info("Analysis produced %d insights", len(insights))
        return insights

    def _get_metrics(self) -> dict[str, Any]:
        """Safely collect metrics."""
        if self.metrics_collector is None:
            return {}
        try:
            return self.metrics_collector.collect()
        except Exception as e:
            logger.warning("Failed to collect metrics: %s", e)
            return {}

    def _get_experiences(self) -> list[Any]:
        """Get EXPERIENCE-type memories for analysis."""
        if self.memory_manager is None:
            return []
        try:
            return self.memory_manager.store.list_entries(
                memory_type=MemoryType.EXPERIENCE,
            )
        except Exception as e:
            logger.warning("Failed to get experiences: %s", e)
            return []

    # -- Analysis methods --

    def _analyze_failure_patterns(self, metrics: dict[str, Any]) -> list[LearningInsight]:
        """Find recurring failure categories and high-failure agents."""
        insights: list[LearningInsight] = []
        if not metrics:
            return insights

        failures = metrics.get("failures", {})
        summary = metrics.get("summary", {})

        # Check for high failure rate
        success_rate = summary.get("success_rate", 100)
        total = summary.get("total", 0)
        if total >= 3 and success_rate < 50:
            insights.append(LearningInsight(
                category=LearningCategory.EXECUTION,
                insight_type=InsightType.ANTI_PATTERN,
                description=(
                    f"Low success rate ({success_rate}%). "
                    f"System may need configuration adjustments."
                ),
                evidence={
                    "success_rate": success_rate,
                    "total_jobs": total,
                    "failed": summary.get("failed", 0),
                },
                confidence=min(total / 10, 1.0),
                impact="high",
            ))

        # Check for recurring error categories
        top_errors = failures.get("top_errors", [])
        for error_entry in top_errors:
            if error_entry.get("count", 0) >= 3:
                insights.append(LearningInsight(
                    category=LearningCategory.EXECUTION,
                    insight_type=InsightType.ANTI_PATTERN,
                    description=(
                        f"Recurring failure: "
                        f"'{error_entry['reason'][:60]}' "
                        f"({error_entry['count']} occurrences)"
                    ),
                    evidence=error_entry,
                    confidence=min(error_entry["count"] / 5, 1.0),
                    impact="high" if error_entry["count"] >= 5 else "medium",
                ))

        # Check for high retry rate
        retries = metrics.get("retries", {})
        retry_rate = retries.get("retry_rate", 0)
        if retry_rate > 30 and total >= 3:
            insights.append(LearningInsight(
                category=LearningCategory.EXECUTION,
                insight_type=InsightType.ANTI_PATTERN,
                description=(
                    f"High retry rate ({retry_rate}%). "
                    f"Consider increasing agent timeout or reducing task complexity."
                ),
                evidence={"retry_rate": retry_rate, "total_jobs": total},
                confidence=min(total / 5, 1.0),
                impact="medium",
            ))

        return insights

    def _analyze_success_patterns(
        self, metrics: dict[str, Any], experiences: list[Any],
    ) -> list[LearningInsight]:
        """Find what works well — patterns worth replicating."""
        insights: list[LearningInsight] = []
        if not metrics:
            return insights

        summary = metrics.get("summary", {})
        total = summary.get("total", 0)
        success_rate = summary.get("success_rate", 0)

        if total >= 5 and success_rate >= 80:
            insights.append(LearningInsight(
                category=LearningCategory.EXECUTION,
                insight_type=InsightType.PATTERN,
                description=(
                    f"High success rate ({success_rate}%) over {total} jobs. "
                    f"Current configuration and planning strategy is effective."
                ),
                evidence={"success_rate": success_rate, "total_jobs": total},
                confidence=min(total / 10, 1.0),
                impact="low",
            ))

        # Analyze experiences for success patterns
        success_tasks: dict[str, int] = {}
        for exp in experiences:
            if "succeeded" in exp.content.lower():
                # Extract agent type
                success_tasks[exp.agent_type] = success_tasks.get(exp.agent_type, 0) + 1

        for agent_type, count in success_tasks.items():
            if count >= 3:
                insights.append(LearningInsight(
                    category=LearningCategory.EXECUTION,
                    insight_type=InsightType.PATTERN,
                    description=(
                        f"Agent '{agent_type}' has {count} successful task completions. "
                        f"Current prompting and tool access is effective."
                    ),
                    evidence={"agent_type": agent_type, "success_count": count},
                    confidence=min(count / 5, 1.0),
                    impact="low",
                    applies_to=[agent_type],
                ))

        return insights

    def _analyze_agent_performance(self, experiences: list[Any]) -> list[LearningInsight]:
        """Analyze per-agent performance from experience memories."""
        insights: list[LearningInsight] = []
        if not experiences:
            return insights

        # Group experiences by agent type
        agent_stats: dict[str, dict[str, int]] = {}
        for exp in experiences:
            stats = agent_stats.setdefault(exp.agent_type, {"success": 0, "failed": 0})
            if "succeeded" in exp.content.lower():
                stats["success"] += 1
            elif "failed" in exp.content.lower():
                stats["failed"] += 1

        for agent_type, stats in agent_stats.items():
            total = stats["success"] + stats["failed"]
            if total < 3:
                continue

            success_rate = stats["success"] / total * 100
            if success_rate < 40:
                insights.append(LearningInsight(
                    category=LearningCategory.AGENT_SELECTION,
                    insight_type=InsightType.ANTI_PATTERN,
                    description=(
                        f"Agent '{agent_type}' has low success rate "
                        f"({success_rate:.0f}%, {stats['success']}/{total}). "
                        f"Consider adjusting its system prompt or tool access."
                    ),
                    evidence={
                        "agent_type": agent_type,
                        "success_rate": success_rate,
                        "total_tasks": total,
                    },
                    confidence=min(total / 5, 1.0),
                    impact="high",
                    applies_to=[agent_type],
                ))
            elif success_rate >= 80:
                insights.append(LearningInsight(
                    category=LearningCategory.AGENT_SELECTION,
                    insight_type=InsightType.PATTERN,
                    description=(
                        f"Agent '{agent_type}' performs well "
                        f"({success_rate:.0f}% success over {total} tasks)."
                    ),
                    evidence={
                        "agent_type": agent_type,
                        "success_rate": success_rate,
                    },
                    confidence=min(total / 5, 1.0),
                    impact="low",
                    applies_to=[agent_type],
                ))

        return insights

    def _analyze_planning_quality(self, metrics: dict[str, Any]) -> list[LearningInsight]:
        """Analyze DAG planning quality from experiences and metrics."""
        insights: list[LearningInsight] = []
        if not metrics:
            return insights

        # Check duration stats for timeout-related patterns
        duration = metrics.get("duration", {})
        p95 = duration.get("p95_sec", 0)
        mean = duration.get("mean_sec", 0)
        count = duration.get("count", 0)

        if count >= 5 and p95 > 0 and mean > 0:
            ratio = p95 / max(mean, 0.001)
            if ratio > 3:
                insights.append(LearningInsight(
                    category=LearningCategory.PLANNING,
                    insight_type=InsightType.RECOMMENDATION,
                    description=(
                        f"High duration variance (P95={p95}s, mean={mean}s). "
                        f"Consider breaking complex tasks into smaller subtasks."
                    ),
                    evidence={
                        "p95_sec": p95,
                        "mean_sec": mean,
                        "variance_ratio": round(ratio, 2),
                        "sample_count": count,
                    },
                    confidence=min(count / 10, 1.0),
                    impact="medium",
                ))

        # Check for timeout-heavy failures
        failures = metrics.get("failures", {})
        for error_entry in failures.get("top_errors", []):
            reason = error_entry.get("reason", "").lower()
            count = error_entry.get("count", 0)
            if "timeout" in reason and count >= 2:
                insights.append(LearningInsight(
                    category=LearningCategory.PLANNING,
                    insight_type=InsightType.RECOMMENDATION,
                    description=(
                        "Frequent timeout failures detected. "
                        "Consider increasing agent_timeout or reducing task scope."
                    ),
                    evidence=error_entry,
                    confidence=min(error_entry["count"] / 3, 1.0),
                    impact="high",
                ))

        return insights
