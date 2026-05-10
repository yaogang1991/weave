"""
Tests for M3.3 Self-Learning System.

Covers: LearningInsight model, LearningAnalyzer, LearningOptimizer,
LearningScheduler, and integration points.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from core.models import (
    LearningInsight, LearningCategory, InsightType,
    MemoryEntry, MemoryScope, MemoryType, EventType,
)
from core.config import LearningConfig, MemoryConfig
from memory.manager import MemoryManager
from learning.analyzer import LearningAnalyzer
from learning.optimizer import LearningOptimizer
from learning.scheduler import LearningScheduler


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def memory_config(tmp_path):
    return MemoryConfig(base_path=str(tmp_path / "memory"))


@pytest.fixture
def memory_manager(memory_config):
    return MemoryManager(memory_config)


@pytest.fixture
def learning_config(tmp_path):
    return LearningConfig(
        base_path=str(tmp_path / "learning"),
        analysis_interval_hours=0,  # Always due
        min_samples=1,
    )


@pytest.fixture
def mock_metrics():
    """Create a mock MetricsCollector with sample data."""
    mock = MagicMock()
    mock.collect.return_value = {
        "summary": {
            "total": 10,
            "succeeded": 6,
            "failed": 3,
            "canceled": 1,
            "dead_letter": 0,
            "success_rate": 60.0,
        },
        "duration": {
            "count": 8,
            "mean_sec": 30,
            "p95_sec": 120,
            "p99_sec": 180,
            "max_sec": 200,
        },
        "retries": {
            "total_attempts": 15,
            "avg_attempts": 1.5,
            "jobs_with_retries": 4,
            "retry_rate": 40.0,
        },
        "failures": {
            "total_failures": 3,
            "top_errors": [
                {"reason": "timeout: execution exceeded limit", "count": 3},
                {"reason": "evaluation_failed: tests did not pass", "count": 2},
            ],
        },
        "throughput": {
            "jobs_per_hour": 2.5,
            "peak_hour": "2026-05-10 14:00",
            "peak_count": 3,
        },
    }
    return mock


@pytest.fixture
def analyzer(mock_metrics, memory_manager):
    return LearningAnalyzer(
        metrics_collector=mock_metrics,
        memory_manager=memory_manager,
    )


@pytest.fixture
def optimizer(memory_manager):
    return LearningOptimizer(memory_manager)


@pytest.fixture
def scheduler(learning_config, analyzer, optimizer):
    return LearningScheduler(learning_config, analyzer, optimizer)


# =============================================================================
# TestLearningInsight
# =============================================================================


class TestLearningInsight:
    def test_default_fields(self):
        insight = LearningInsight(
            category=LearningCategory.EXECUTION,
            insight_type=InsightType.ANTI_PATTERN,
            description="High failure rate",
        )
        assert insight.id.startswith("ins_")
        assert insight.confidence == 1.0
        assert insight.impact == "medium"
        assert insight.created_at.tzinfo is not None

    def test_serialization_roundtrip(self):
        insight = LearningInsight(
            category=LearningCategory.PLANNING,
            insight_type=InsightType.RECOMMENDATION,
            description="Break complex tasks into subtasks",
            evidence={"p95_sec": 120},
            confidence=0.85,
            impact="high",
            applies_to=["planner"],
        )
        data = insight.model_dump(mode="json")
        restored = LearningInsight(**data)
        assert restored.id == insight.id
        assert restored.category == LearningCategory.PLANNING
        assert restored.confidence == 0.85
        assert restored.applies_to == ["planner"]

    def test_event_types_exist(self):
        assert EventType.LEARNING_ANALYSIS_START == "learning.analysis_start"
        assert EventType.LEARNING_INSIGHT_GENERATED == "learning.insight_generated"
        assert EventType.LEARNING_OPTIMIZATION_APPLIED == "learning.optimization_applied"


# =============================================================================
# TestLearningAnalyzer
# =============================================================================


class TestLearningAnalyzer:
    def test_analyze_returns_list(self, analyzer):
        insights = analyzer.analyze()
        assert isinstance(insights, list)

    def test_failure_patterns_detected(self, analyzer):
        insights = analyzer.analyze()
        # Our mock metrics have: 60% success rate (< 80%), 40% retry rate
        failure_insights = [
            i for i in insights
            if i.insight_type == InsightType.ANTI_PATTERN
        ]
        assert len(failure_insights) >= 1

    def test_recurring_errors_detected(self, analyzer):
        insights = analyzer.analyze()
        # Mock has timeout with count=3 and eval_failed with count=2
        error_insights = [
            i for i in insights
            if "Recurring failure" in i.description
        ]
        assert len(error_insights) >= 1

    def test_high_retry_rate_detected(self, analyzer):
        insights = analyzer.analyze()
        retry_insights = [
            i for i in insights
            if "retry rate" in i.description.lower()
        ]
        assert len(retry_insights) >= 1

    def test_no_metrics_returns_empty(self, memory_manager):
        analyzer = LearningAnalyzer(None, memory_manager)
        insights = analyzer.analyze()
        # Should return empty or only experience-based insights
        assert isinstance(insights, list)

    def test_high_success_rate_pattern(self, memory_manager):
        mock = MagicMock()
        mock.collect.return_value = {
            "summary": {"total": 10, "succeeded": 9, "failed": 1,
                        "canceled": 0, "dead_letter": 0, "success_rate": 90.0},
            "duration": {"count": 8, "mean_sec": 30, "p95_sec": 50,
                         "p99_sec": 60, "max_sec": 70},
            "retries": {"total_attempts": 11, "avg_attempts": 1.1,
                        "jobs_with_retries": 1, "retry_rate": 10.0},
            "failures": {"total_failures": 1, "top_errors": []},
            "throughput": {"jobs_per_hour": 2, "peak_hour": None, "peak_count": 0},
        }
        analyzer = LearningAnalyzer(mock, memory_manager)
        insights = analyzer.analyze()
        patterns = [
            i for i in insights
            if i.insight_type == InsightType.PATTERN
        ]
        assert len(patterns) >= 1

    def test_agent_performance_analysis(self, memory_manager):
        # Store experience memories for a low-performing agent
        for _ in range(4):
            memory_manager.store.store(MemoryEntry(
                agent_type="evaluator",
                memory_type=MemoryType.EXPERIENCE,
                content="Task 'run tests' failed. Timeout exceeded.",
            ))
        # Store one success
        memory_manager.store.store(MemoryEntry(
            agent_type="evaluator",
            memory_type=MemoryType.EXPERIENCE,
            content="Task 'run tests' succeeded. All tests pass.",
        ))

        mock = MagicMock()
        mock.collect.return_value = {
            "summary": {"total": 5, "succeeded": 4, "failed": 1,
                        "canceled": 0, "dead_letter": 0, "success_rate": 80.0},
            "duration": {"count": 5, "mean_sec": 20, "p95_sec": 40,
                         "p99_sec": 50, "max_sec": 60},
            "retries": {"total_attempts": 6, "avg_attempts": 1.2,
                        "jobs_with_retries": 1, "retry_rate": 20.0},
            "failures": {"total_failures": 1, "top_errors": []},
            "throughput": {"jobs_per_hour": 1, "peak_hour": None, "peak_count": 0},
        }
        analyzer = LearningAnalyzer(mock, memory_manager)
        insights = analyzer.analyze()

        agent_insights = [
            i for i in insights
            if i.category == LearningCategory.AGENT_SELECTION
        ]
        assert len(agent_insights) >= 1

    def test_planning_quality_timeout(self, memory_manager):
        mock = MagicMock()
        mock.collect.return_value = {
            "summary": {"total": 10, "succeeded": 5, "failed": 5,
                        "canceled": 0, "dead_letter": 0, "success_rate": 50.0},
            "duration": {"count": 10, "mean_sec": 20, "p95_sec": 120,
                         "p99_sec": 200, "max_sec": 250},
            "retries": {"total_attempts": 12, "avg_attempts": 1.2,
                        "jobs_with_retries": 2, "retry_rate": 20.0},
            "failures": {
                "total_failures": 5,
                "top_errors": [
                    {"reason": "timeout: execution exceeded 600s limit", "count": 3},
                ],
            },
            "throughput": {"jobs_per_hour": 1, "peak_hour": None, "peak_count": 0},
        }
        analyzer = LearningAnalyzer(mock, memory_manager)
        insights = analyzer.analyze()

        planning_insights = [
            i for i in insights
            if i.category == LearningCategory.PLANNING
        ]
        assert len(planning_insights) >= 1

    def test_metrics_exception_handled(self, memory_manager):
        mock = MagicMock()
        mock.collect.side_effect = Exception("DB error")
        analyzer = LearningAnalyzer(mock, memory_manager)
        insights = analyzer.analyze()
        assert isinstance(insights, list)  # Should not crash


# =============================================================================
# TestLearningOptimizer
# =============================================================================


class TestLearningOptimizer:
    def test_optimize_stores_high_confidence(self, optimizer):
        insights = [
            LearningInsight(
                category=LearningCategory.EXECUTION,
                insight_type=InsightType.ANTI_PATTERN,
                description="High failure rate detected",
                confidence=0.9,
                impact="high",
            ),
        ]
        entries = optimizer.optimize(insights, confidence_threshold=0.7)
        assert len(entries) == 1
        assert entries[0].scope == MemoryScope.GLOBAL

    def test_optimize_skips_low_confidence(self, optimizer):
        insights = [
            LearningInsight(
                category=LearningCategory.EXECUTION,
                insight_type=InsightType.PATTERN,
                description="Maybe a pattern",
                confidence=0.3,
            ),
        ]
        entries = optimizer.optimize(insights, confidence_threshold=0.7)
        assert len(entries) == 0

    def test_optimize_agent_specific_scope(self, optimizer):
        insights = [
            LearningInsight(
                category=LearningCategory.AGENT_SELECTION,
                insight_type=InsightType.ANTI_PATTERN,
                description="Evaluator has low success rate",
                confidence=0.9,
                applies_to=["evaluator"],
            ),
        ]
        entries = optimizer.optimize(insights, confidence_threshold=0.7)
        assert len(entries) == 1
        assert entries[0].scope == MemoryScope.PRIVATE
        assert entries[0].agent_type == "evaluator"

    def test_optimize_anti_pattern_as_experience(self, optimizer):
        insights = [
            LearningInsight(
                category=LearningCategory.EXECUTION,
                insight_type=InsightType.ANTI_PATTERN,
                description="Avoid something",
                confidence=0.9,
            ),
        ]
        entries = optimizer.optimize(insights, confidence_threshold=0.7)
        assert entries[0].memory_type == MemoryType.EXPERIENCE

    def test_optimize_pattern_as_fact(self, optimizer):
        insights = [
            LearningInsight(
                category=LearningCategory.EXECUTION,
                insight_type=InsightType.PATTERN,
                description="Good pattern found",
                confidence=0.9,
            ),
        ]
        entries = optimizer.optimize(insights, confidence_threshold=0.7)
        assert entries[0].memory_type == MemoryType.FACT

    def test_get_planning_hints_with_data(self, optimizer, memory_manager):
        # Store a learning-related memory
        memory_manager.store.store(MemoryEntry(
            agent_type="shared",
            scope=MemoryScope.GLOBAL,
            memory_type=MemoryType.FACT,
            content="Recommendation: Break complex tasks into subtasks",
            keywords=["recommendation", "planning"],
        ))
        hints = optimizer.get_planning_hints("build API")
        assert isinstance(hints, str)

    def test_get_planning_hints_empty(self, optimizer):
        hints = optimizer.get_planning_hints("build API")
        assert hints == ""

    def test_get_agent_hints_with_data(self, optimizer, memory_manager):
        memory_manager.store.store(MemoryEntry(
            agent_type="generator",
            scope=MemoryScope.PRIVATE,
            memory_type=MemoryType.FACT,
            content="Recommendation: Read files before editing",
            keywords=["recommendation", "generator"],
        ))
        hints = optimizer.get_agent_hints("generator", "edit files")
        assert isinstance(hints, str)

    def test_get_agent_hints_empty(self, optimizer):
        hints = optimizer.get_agent_hints("planner", "plan task")
        assert hints == ""

    def test_content_truncation(self, optimizer):
        insights = [
            LearningInsight(
                category=LearningCategory.EXECUTION,
                insight_type=InsightType.PATTERN,
                description="x" * 2000,  # Very long
                confidence=0.9,
            ),
        ]
        entries = optimizer.optimize(insights, confidence_threshold=0.7)
        assert len(entries) == 1
        assert len(entries[0].content) <= 1000


# =============================================================================
# TestLearningScheduler
# =============================================================================


class TestLearningScheduler:
    def test_run_analysis(self, scheduler):
        result = scheduler.run_analysis()
        assert "total_insights" in result
        assert "stored_memories" in result
        assert "started_at" in result
        assert "completed_at" in result

    def test_maybe_run_analysis_due(self, scheduler):
        # Interval=0 means always due
        result = scheduler.maybe_run_analysis()
        assert result is not None

    def test_maybe_run_analysis_not_due(self, tmp_path, analyzer, optimizer):
        config = LearningConfig(
            base_path=str(tmp_path / "learning"),
            analysis_interval_hours=999,  # Far future
        )
        scheduler = LearningScheduler(config, analyzer, optimizer)
        # Run once to set last analysis time
        scheduler.run_analysis()
        # Second call should be skipped
        result = scheduler.maybe_run_analysis()
        assert result is None

    def test_maybe_run_analysis_disabled(self, tmp_path, analyzer, optimizer):
        config = LearningConfig(
            base_path=str(tmp_path / "learning"),
            enabled=False,
        )
        scheduler = LearningScheduler(config, analyzer, optimizer)
        result = scheduler.maybe_run_analysis()
        assert result is None

    def test_get_status(self, scheduler):
        scheduler.run_analysis()
        status = scheduler.get_status()
        assert status["enabled"] is True
        assert status["last_analysis"] is not None
        assert status["last_insight_count"] >= 0

    def test_get_status_initial(self, scheduler):
        status = scheduler.get_status()
        assert status["enabled"] is True
        assert status["last_analysis"] is None

    def test_state_persistence(self, scheduler, tmp_path):
        scheduler.run_analysis()
        # State file should exist
        state_path = Path(scheduler.config.base_path) / ".last_analysis"
        assert state_path.exists()
        data = json.loads(state_path.read_text())
        assert "total_insights" in data


# =============================================================================
# TestLearningIntegration
# =============================================================================


class TestLearningIntegration:
    def test_full_analyze_optimize_cycle(self, scheduler, memory_manager):
        """End-to-end: analyze metrics → optimize → memories stored."""
        result = scheduler.run_analysis()
        assert result["stored_memories"] >= 0

        # If memories were stored, verify they're retrievable
        if result["stored_memories"] > 0:
            entries = memory_manager.store.list_entries(
                scope=MemoryScope.GLOBAL,
            )
            assert len(entries) >= 1

    def test_learning_config_defaults(self):
        config = LearningConfig()
        assert config.enabled is True
        assert config.analysis_interval_hours == 6.0
        assert config.min_samples == 5
        assert config.confidence_threshold == 0.7

    def test_insight_categories_complete(self):
        assert LearningCategory.PLANNING.value == "planning"
        assert LearningCategory.EXECUTION.value == "execution"
        assert LearningCategory.EVALUATION.value == "evaluation"
        assert LearningCategory.AGENT_SELECTION.value == "agent_selection"

    def test_insight_types_complete(self):
        assert InsightType.PATTERN.value == "pattern"
        assert InsightType.RECOMMENDATION.value == "recommendation"
        assert InsightType.ANTI_PATTERN.value == "anti_pattern"
