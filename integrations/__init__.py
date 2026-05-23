"""Integration layer -- IssueTracker, CodeHost adapters for external systems."""
from integrations.base import CodeHost, IssueTracker
from integrations.models import LabelConfig, NormalizedIssue, RawIssue
from integrations.registry import IntegrationRegistry

__all__ = [
    "CodeHost",
    "IssueTracker",
    "IntegrationRegistry",
    "LabelConfig",
    "NormalizedIssue",
    "RawIssue",
]
