"""Impact analysis and DAG template models (M3.4, M3.5)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class DAGTemplate(BaseModel):
    """A reusable DAG template with variable substitution."""
    name: str
    description: str
    version: str = "1.0"
    category: str = "general"
    variables: dict[str, str] = Field(default_factory=dict)
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]] = Field(default_factory=list)
    reasoning_template: str = ""


class ImpactRiskLevel(str, Enum):
    """Risk level of predicted impact scope."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ImpactScope(BaseModel):
    """Predicted impact of a requirement on a project's file structure."""
    id: str = Field(default_factory=lambda: f"imp_{uuid.uuid4().hex[:12]}")
    requirement: str
    predicted_files: list[str] = Field(default_factory=list)
    predicted_modules: list[str] = Field(default_factory=list)
    risk_level: ImpactRiskLevel = ImpactRiskLevel.MEDIUM
    confidence: float = 0.0
    reasoning: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class VerificationResult(BaseModel):
    """Result of comparing predicted impact to actual changes."""
    impact_scope_id: str
    expected_files: list[str] = Field(default_factory=list)
    actual_changed_files: list[str] = Field(default_factory=list)
    covered_files: list[str] = Field(default_factory=list)
    unexpected_files: list[str] = Field(default_factory=list)
    missed_files: list[str] = Field(default_factory=list)
    coverage: float = 0.0
    prediction_accuracy: float = 0.0
    passes: bool = False
    notes: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
