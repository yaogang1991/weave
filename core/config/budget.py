"""Token budget and estimation configuration."""
from __future__ import annotations

import os

from pydantic import BaseModel, Field


class BudgetConfig(BaseModel):
    """M4.2: Token budget configuration for cost control."""
    enabled: bool = True
    total_tokens: int = Field(default=0, ge=0)
    warning_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    per_node_token_limit: int = Field(default=0, ge=0)

    @property
    def is_unlimited(self) -> bool:
        return self.total_tokens == 0

    @classmethod
    def from_env(cls) -> BudgetConfig:
        return cls(
            enabled=os.getenv("WEAVE_BUDGET_ENABLED", "true").lower()
            not in ("false", "0", "no"),
            total_tokens=int(os.getenv("WEAVE_BUDGET_TOKENS", "0")),
            warning_threshold=float(os.getenv("WEAVE_BUDGET_WARNING_THRESHOLD", "0.8")),
            per_node_token_limit=int(os.getenv("WEAVE_BUDGET_PER_NODE_TOKENS", "0")),
        )


class TokenEstimationConfig(BaseModel):
    """M4.6: Token estimation configuration."""
    enabled: bool = Field(default=True)
    fallback_to_heuristic: bool = Field(default=True)
    target_budget: int = Field(default=8192)
    overhead_margins: dict[str, int] = Field(
        default_factory=lambda: {"generator": 2200, "evaluator": 900, "planner": 550},
    )
    max_estimation_concurrency: int = Field(default=10)
    cache_ttl_seconds: int = Field(default=300)
