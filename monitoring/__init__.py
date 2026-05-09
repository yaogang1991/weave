"""
Monitoring — local metrics aggregation and minimal alerting system.

Exports:
    MetricsCollector: Collect metrics from JobRepository
    MetricsReporter: Generate JSON / Markdown reports
    AlertManager: Multi-rule alerting with webhook + console fallback
    AlertRule: Alert rule definition
    AlertEvent: Alert event dataclass
    create_default_alerts: Factory for default alert configuration
"""

from __future__ import annotations

from monitoring.alerts import (
    AlertEvent,
    AlertManager,
    AlertRule,
    create_default_alerts,
)
from monitoring.metrics import MetricsCollector, MetricsReporter

__all__ = [
    "AlertEvent",
    "AlertManager",
    "AlertRule",
    "MetricsCollector",
    "MetricsReporter",
    "create_default_alerts",
]
