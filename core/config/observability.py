"""Observability configuration."""
from __future__ import annotations

import os

from pydantic import BaseModel


class ObservabilityConfig(BaseModel):
    """M5.1: Observability configuration."""
    enabled: bool = True
    otlp_endpoint: str | None = None

    @classmethod
    def from_env(cls) -> ObservabilityConfig:
        return cls(
            enabled=os.getenv("WEAVE_OBSERVABILITY_ENABLED", "true").lower()
            not in ("false", "0"),
            otlp_endpoint=os.getenv("WEAVE_OTLP_ENDPOINT") or None,
        )
