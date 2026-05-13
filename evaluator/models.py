"""
Evaluation context and result models for pluggable criterion checkers.

Part of #178 PR 1: modularize EvaluatorEngine and formalize evaluation contracts.
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CheckSeverity(str, Enum):
    """Severity level for checker results."""

    NORMAL = "normal"       # Standard automated check result
    WARNING = "warning"     # Could not auto-verify; manual review recommended
    ERROR = "error"         # Check execution itself failed


class EvaluationContext(BaseModel):
    """Context passed to every criterion checker."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    work_dir: Path
    node_id: str | None = None
    artifacts: list[str] | None = None
    session_store: Any = None   # SessionStore — Any to avoid circular import


class CheckResult(BaseModel):
    """Result from a single criterion check."""

    passed: bool
    message: str
    severity: CheckSeverity = CheckSeverity.NORMAL
    metadata: dict[str, Any] = Field(default_factory=dict)
