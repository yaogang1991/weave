"""Impact analysis configuration."""
from __future__ import annotations

import os

from pydantic import BaseModel, Field


class ImpactConfig(BaseModel):
    """Configuration for the M3.5 Impact Analysis system."""
    enabled: bool = True
    coverage_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    max_predicted_files: int = Field(default=50, ge=1)
    confidence_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    base_path: str = Field(
        default_factory=lambda: os.getenv("WEAVE_IMPACT_PATH", "./data/impact")
    )
