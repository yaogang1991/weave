"""Self-learning system configuration."""
from __future__ import annotations

import os

from pydantic import BaseModel, Field


class LearningConfig(BaseModel):
    """Configuration for the M3.3 Self-Learning system."""
    enabled: bool = True
    analysis_interval_hours: float = Field(default=6.0, ge=0.0)
    min_samples: int = Field(default=5, ge=1)
    max_insights: int = Field(default=100, ge=1)
    confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    base_path: str = Field(
        default_factory=lambda: os.getenv("WEAVE_LEARNING_PATH", "./data/learning")
    )
    failure_rate_threshold: float = Field(default=50.0, ge=0.0, le=100.0)
    success_rate_threshold: float = Field(default=80.0, ge=0.0, le=100.0)
    low_agent_rate_threshold: float = Field(default=40.0, ge=0.0, le=100.0)
    min_error_samples: int = Field(default=3, ge=1)
    min_trend_samples: int = Field(default=5, ge=1)
    retry_rate_threshold: float = Field(default=30.0, ge=0.0, le=100.0)
    duration_variance_ratio: float = Field(default=3.0, ge=1.0)
