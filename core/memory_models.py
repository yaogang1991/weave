"""Agent memory and self-learning models (M3.2, M3.3)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class MemoryScope(str, Enum):
    """Visibility scope of a memory entry."""
    PRIVATE = "private"      # Per-agent, only the owning agent sees it
    SESSION = "session"      # Shared among agents within one session
    GLOBAL = "global"        # Cross-session, cross-agent, persistent


class MemoryType(str, Enum):
    """Classification of memory content."""
    FACT = "fact"            # Learned knowledge (e.g., "project uses pytest")
    EXPERIENCE = "experience"  # Task outcome (e.g., "planner succeeded with linear DAG")
    PREFERENCE = "preference"  # User preference (e.g., "user prefers type hints")
    CONTEXT = "context"      # Project context (e.g., "entry point is main.py")


class MemoryEntry(BaseModel):
    """A single memory record persisted across agent executions."""
    id: str = Field(default_factory=lambda: f"mem_{uuid.uuid4().hex[:12]}")
    agent_type: str           # Owning agent (planner/generator/evaluator or "shared")
    scope: MemoryScope = MemoryScope.PRIVATE
    memory_type: MemoryType = MemoryType.FACT
    content: str
    keywords: list[str] = Field(default_factory=list)
    session_id: str | None = None
    source_node_id: str | None = None
    access_count: int = 0
    relevance_score: float = 1.0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_accessed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LearningCategory(str, Enum):
    """Category of a learning insight."""
    PLANNING = "planning"
    EXECUTION = "execution"
    EVALUATION = "evaluation"
    AGENT_SELECTION = "agent_selection"


class InsightType(str, Enum):
    """Type of learning insight."""
    PATTERN = "pattern"              # Positive pattern to replicate
    RECOMMENDATION = "recommendation"  # Suggested improvement
    ANTI_PATTERN = "anti_pattern"     # Negative pattern to avoid


class LearningInsight(BaseModel):
    """A learning insight extracted from execution history analysis."""
    id: str = Field(default_factory=lambda: f"ins_{uuid.uuid4().hex[:12]}")
    category: LearningCategory
    insight_type: InsightType
    description: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 1.0          # 0.0 to 1.0
    impact: Literal["low", "medium", "high"] = "medium"
    applies_to: list[str] = Field(default_factory=list)  # Agent types
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)
