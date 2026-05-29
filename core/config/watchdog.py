"""Watchdog configuration for DAG node heartbeat monitoring."""
from __future__ import annotations

import os

from pydantic import BaseModel, Field


class AgentWatchdogOverride(BaseModel):
    """Per-agent-type heartbeat override for the watchdog."""
    heartbeat_interval_sec: float | None = None
    heartbeat_miss_threshold: int | None = None


_DEFAULT_AGENT_WATCHDOG_OVERRIDES: dict[str, AgentWatchdogOverride] = {
    "generator": AgentWatchdogOverride(
        heartbeat_interval_sec=90.0,
        heartbeat_miss_threshold=20,
    ),
}


class WatchdogConfig(BaseModel):
    """Configuration for the DAG node watchdog."""

    enabled: bool = True
    heartbeat_interval_sec: float = 30.0
    heartbeat_miss_threshold: int = 12
    alert_threshold_fraction: float = Field(default=0.5, ge=0.0, le=1.0)
    agent_overrides: dict[str, AgentWatchdogOverride] = Field(
        default_factory=lambda: dict(_DEFAULT_AGENT_WATCHDOG_OVERRIDES),
    )

    def settings_for(self, agent_type: str) -> tuple[float, int]:
        override = self.agent_overrides.get(agent_type)
        interval = (
            override.heartbeat_interval_sec
            if override and override.heartbeat_interval_sec is not None
            else self.heartbeat_interval_sec
        )
        threshold = (
            override.heartbeat_miss_threshold
            if override and override.heartbeat_miss_threshold is not None
            else self.heartbeat_miss_threshold
        )
        return interval, threshold

    def alert_threshold_for(self, agent_type: str) -> int:
        _, threshold = self.settings_for(agent_type)
        return max(2, int(threshold * self.alert_threshold_fraction))

    @classmethod
    def from_env(cls) -> WatchdogConfig:
        overrides: dict[str, AgentWatchdogOverride] = dict(
            _DEFAULT_AGENT_WATCHDOG_OVERRIDES,
        )
        for agent_type in ("planner", "generator", "evaluator"):
            iv = os.getenv(f"WEAVE_WATCHDOG_{agent_type.upper()}_INTERVAL")
            tv = os.getenv(f"WEAVE_WATCHDOG_{agent_type.upper()}_THRESHOLD")
            if iv or tv:
                overrides[agent_type] = AgentWatchdogOverride(
                    heartbeat_interval_sec=float(iv) if iv else None,
                    heartbeat_miss_threshold=int(tv) if tv else None,
                )
        return cls(
            enabled=os.getenv("WEAVE_WATCHDOG_ENABLED", "true").lower()
            not in ("false", "0", "no"),
            heartbeat_interval_sec=float(
                os.getenv("WEAVE_WATCHDOG_INTERVAL", "30.0")
            ),
            heartbeat_miss_threshold=int(
                os.getenv("WEAVE_WATCHDOG_THRESHOLD", "8")
            ),
            alert_threshold_fraction=float(
                os.getenv("WEAVE_WATCHDOG_ALERT_FRACTION", "0.5")
            ),
            agent_overrides=overrides,
        )
