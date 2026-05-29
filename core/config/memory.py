"""Agent memory configuration."""
from __future__ import annotations

import os

from pydantic import BaseModel, Field, model_validator


class MemoryConfig(BaseModel):
    """Configuration for the M3.2 Agent Memory system."""
    enabled: bool = True
    base_path: str = Field(
        default_factory=lambda: os.getenv("WEAVE_MEMORY_PATH", "./data/memory")
    )
    max_entries_per_agent: int = Field(default=500, ge=1)
    max_content_length: int = Field(default=1000, ge=100)
    default_ttl_days: int = Field(default=90, ge=1)
    retrieval_limit: int = Field(default=10, ge=1)
    decay_half_life_days: float = Field(default=30.0, ge=1.0)
    auto_store: bool = True
    embedding_provider: str = Field(default="local")
    semantic_search_enabled: bool = Field(default=True)

    @model_validator(mode="after")
    def _validate_memory_config(self) -> "MemoryConfig":
        if self.retrieval_limit > self.max_entries_per_agent:
            raise ValueError("retrieval_limit cannot exceed max_entries_per_agent")
        return self
