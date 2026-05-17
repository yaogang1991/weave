"""CLI learning commands — analyze, insights, status (M3.3)."""

from __future__ import annotations

import json

from cli.memory import _make_memory_manager


def _make_learning_scheduler():
    """Create a LearningScheduler from config."""
    from learning.analyzer import LearningAnalyzer
    from learning.optimizer import LearningOptimizer
    from learning.scheduler import LearningScheduler
    from control_plane.repository import JobRepository
    from monitoring.metrics import MetricsCollector
    from core.config import HarnessConfig

    config = HarnessConfig.from_env()
    memory_manager = _make_memory_manager()
    job_repo = JobRepository()
    metrics_collector = MetricsCollector(job_repo)

    analyzer = LearningAnalyzer(metrics_collector, memory_manager)
    optimizer = LearningOptimizer(memory_manager)
    return LearningScheduler(config.learning, analyzer, optimizer)


async def cmd_learning_analyze(args):
    """Trigger a learning analysis run."""
    scheduler = _make_learning_scheduler()
    result = scheduler.run_analysis()
    print(json.dumps(result, indent=2, default=str))


async def cmd_learning_insights(args):
    """List stored learning insights."""
    manager = _make_memory_manager()
    entries = manager.store.search(
        query="recommendation pattern anti_pattern",
        limit=args.limit,
    )
    result = [
        {
            "id": e.id,
            "agent_type": e.agent_type,
            "scope": e.scope.value,
            "type": e.memory_type.value,
            "content": e.content,
            "keywords": e.keywords,
            "relevance_score": e.relevance_score,
            "created_at": e.created_at.isoformat(),
        }
        for e in entries
    ]
    print(json.dumps(result, indent=2, default=str))


async def cmd_learning_status(args):
    """Show learning system status."""
    scheduler = _make_learning_scheduler()
    status = scheduler.get_status()
    print(json.dumps(status, indent=2, default=str))
