"""Timeout, stall, and watchdog configuration."""

from __future__ import annotations

import os

from pydantic import BaseModel, Field


class EvalTimeoutScaleConfig(BaseModel):
    """Dynamic evaluator timeout scaling based on project size (#621).

    When enabled, the evaluator timeout is calculated as:
        base_timeout + (file_count * per_file_seconds)
    This prevents timeout failures on large generated projects.
    """

    enabled: bool = Field(
        default=os.getenv(
            "WEAVE_EVAL_TIMEOUT_SCALE", "true",
        ).lower() not in ("false", "0", "no"),
        description="Enable dynamic evaluator timeout scaling",
    )
    per_file_seconds: int = Field(
        default=int(os.getenv(
            "WEAVE_EVAL_TIMEOUT_PER_FILE", "5",
        )),
        ge=1,
        description="Additional seconds per output file for evaluator timeout",
    )
    max_timeout: int = Field(
        default=int(os.getenv(
            "WEAVE_EVAL_TIMEOUT_MAX", "1200",
        )),
        ge=1,
        description="Upper cap for dynamically scaled evaluator timeout",
    )


class EvaluatorStallScaleConfig(BaseModel):
    """Dynamic evaluator stall timeout scaling based on workspace size.

    Note: os.getenv() in Field(default=...) is evaluated once at import
    time. Runtime changes to these env vars require a process restart.
    """

    base: int = Field(
        default=int(os.getenv("WEAVE_EVAL_STALL_BASE", "300")),
        ge=1,
    )
    per_file: int = Field(
        default=int(os.getenv("WEAVE_EVAL_STALL_PER_FILE", "4")),
        ge=0,
    )
    per_test: int = Field(
        default=int(os.getenv("WEAVE_EVAL_STALL_PER_TEST", "3")),
        ge=0,
    )
    cap: int = Field(
        default=int(os.getenv("WEAVE_EVAL_STALL_CAP", "900")),
        ge=1,
    )


class GeneratorStallScaleConfig(BaseModel):
    """Dynamic generator stall timeout scaling based on dependency/feature count.

    Note: os.getenv() in Field(default=...) is evaluated once at import
    time. Runtime changes to these env vars require a process restart.
    """

    base: int = Field(
        default=int(os.getenv("WEAVE_GEN_STALL_BASE", "300")),
        ge=1,
    )
    per_dep: int = Field(
        default=int(os.getenv("WEAVE_GEN_STALL_PER_DEP", "30")),
        ge=0,
    )
    per_feature: int = Field(
        default=int(os.getenv("WEAVE_GEN_STALL_PER_FEATURE", "40")),
        ge=0,
        description=(
            "Extra seconds per estimated feature in task description "
            "(#722). Nodes with many features need more time."
        ),
    )
    cap: int = Field(
        default=int(os.getenv("WEAVE_GEN_STALL_CAP", "900")),
        ge=1,
    )


class NodeTimeoutConfig(BaseModel):
    """Per-agent-type node execution timeout (#360 PR2, M4.5).

    M4.5 progress-driven stall timeout with dynamic complexity scaling.
    stall_timeout is the sole kill mechanism (no max_total hard cap).
    """

    default_timeout: int = Field(
        default=int(os.getenv(
            "WEAVE_NODE_TIMEOUT",
            os.getenv("WEAVE_AGENT_TIMEOUT", "300"),
        )),
        description="Default node execution timeout in seconds",
    )
    overrides: dict[str, int] = Field(
        default_factory=lambda: {
            "generator": int(os.getenv(
                "WEAVE_NODE_TIMEOUT_GENERATOR", "600",
            )),
            "evaluator": int(os.getenv(
                "WEAVE_NODE_TIMEOUT_EVALUATOR", "480",
            )),
        },
        description="Per-agent-type timeout overrides (agent_type -> seconds)",
    )
    eval_scale: EvalTimeoutScaleConfig = Field(
        default_factory=EvalTimeoutScaleConfig,
        description="Dynamic evaluator timeout scaling (#621)",
    )

    # M4.5: Progress-driven stall timeout per agent type
    stall_timeout: int = Field(
        default=int(os.getenv("WEAVE_STALL_TIMEOUT", "120")),
        description="Kill node if no progress reported for this many seconds",
    )
    stall_overrides: dict[str, int] = Field(
        default_factory=dict,
        description="Per-agent-type stall timeout overrides",
    )
    eval_stall_scale: EvaluatorStallScaleConfig = Field(
        default_factory=EvaluatorStallScaleConfig,
    )
    gen_stall_scale: GeneratorStallScaleConfig = Field(
        default_factory=GeneratorStallScaleConfig,
    )

    def timeout_for(
        self, agent_type: str, artifact_count: int = 0,
    ) -> int:
        """Return timeout for the given agent type.

        For evaluator nodes with eval_scale enabled, dynamically adjusts
        the timeout based on the number of output artifacts from upstream
        nodes (proxy for project size).
        """
        base = self.overrides.get(agent_type, self.default_timeout)

        if (
            agent_type == "evaluator"
            and self.eval_scale.enabled
            and artifact_count > 0
        ):
            scaled = base + artifact_count * self.eval_scale.per_file_seconds
            return min(scaled, self.eval_scale.max_timeout)

        return base

    def stall_timeout_for(
        self,
        agent_type: str,
        file_count: int = 0,
        test_count: int = 0,
        dep_count: int = 0,
        feature_count: int = 0,
    ) -> int:
        """Return dynamic stall timeout: max(configured, complexity-based).

        Caller provides file/test/dependency/feature counts; no I/O
        performed here.  Configured value is always a floor.
        """
        configured = self.stall_overrides.get(agent_type, self.stall_timeout)

        dynamic = 0
        if agent_type == "evaluator" and (file_count or test_count):
            dynamic = min(
                self.eval_stall_scale.base
                + file_count * self.eval_stall_scale.per_file
                + test_count * self.eval_stall_scale.per_test,
                self.eval_stall_scale.cap,
            )
        elif agent_type == "generator" and (dep_count or feature_count):
            dynamic = min(
                self.gen_stall_scale.base
                + dep_count * self.gen_stall_scale.per_dep
                + feature_count * self.gen_stall_scale.per_feature,
                self.gen_stall_scale.cap,
            )

        return max(configured, dynamic) if dynamic else configured

    @property
    def min_timeout(self) -> int:
        values = [self.default_timeout, *self.overrides.values()]
        return min(values)

    @property
    def max_timeout(self) -> int:
        values = [self.default_timeout, *self.overrides.values()]
        if self.eval_scale.enabled:
            values.append(self.eval_scale.max_timeout)
        return max(values)


class AgentWatchdogOverride(BaseModel):
    """Per-agent-type heartbeat override for the watchdog."""

    heartbeat_interval_sec: float | None = None
    heartbeat_miss_threshold: int | None = None


# Sensible defaults: generator tasks (editing existing files) naturally take
# much longer than planner/evaluator tasks, especially when writing 6+ test
# files (#275).
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
    heartbeat_miss_threshold: int = 12  # was 5→8; raised to reduce false kills (#275)
    # Fraction of miss_threshold at which heartbeat_missed events are
    # emitted.  With threshold=8 and fraction=0.5, alerts fire at
    # missed_count >= 4 instead of the old hardcoded 2 — reducing noise
    # for slow but healthy LLM API responses.
    alert_threshold_fraction: float = Field(default=0.5, ge=0.0, le=1.0)
    agent_overrides: dict[str, AgentWatchdogOverride] = Field(
        default_factory=lambda: dict(_DEFAULT_AGENT_WATCHDOG_OVERRIDES),
    )

    def settings_for(self, agent_type: str) -> tuple[float, int]:
        """Return (interval_sec, miss_threshold) for *agent_type*."""
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
        """Minimum missed_count to emit heartbeat_missed event."""
        _, threshold = self.settings_for(agent_type)
        return max(2, int(threshold * self.alert_threshold_fraction))

    @classmethod
    def from_env(cls) -> WatchdogConfig:
        """Create from WEAVE_WATCHDOG_* environment variables."""
        overrides: dict[str, AgentWatchdogOverride] = dict(
            _DEFAULT_AGENT_WATCHDOG_OVERRIDES,
        )
        # Allow per-agent override via WEAVE_WATCHDOG_<TYPE>_INTERVAL /
        # WEAVE_WATCHDOG_<TYPE>_THRESHOLD (e.g. WEAVE_WATCHDOG_GENERATOR_INTERVAL)
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
