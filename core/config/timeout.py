"""Node timeout and stall configuration."""
from __future__ import annotations

import os

from pydantic import BaseModel, Field


class EvalTimeoutScaleConfig(BaseModel):
    """Dynamic evaluator timeout scaling based on project size (#621)."""
    enabled: bool = Field(
        default=os.getenv("WEAVE_EVAL_TIMEOUT_SCALE", "true").lower()
        not in ("false", "0", "no"),
    )
    per_file_seconds: int = Field(
        default=int(os.getenv("WEAVE_EVAL_TIMEOUT_PER_FILE", "5")), ge=1,
    )
    max_timeout: int = Field(
        default=int(os.getenv("WEAVE_EVAL_TIMEOUT_MAX", "1200")), ge=1,
    )


class EvaluatorStallScaleConfig(BaseModel):
    """Dynamic evaluator stall timeout scaling."""
    base: int = Field(default=int(os.getenv("WEAVE_EVAL_STALL_BASE", "120")), ge=1)
    per_file: int = Field(default=int(os.getenv("WEAVE_EVAL_STALL_PER_FILE", "4")), ge=0)
    per_test: int = Field(default=int(os.getenv("WEAVE_EVAL_STALL_PER_TEST", "3")), ge=0)
    cap: int = Field(default=int(os.getenv("WEAVE_EVAL_STALL_CAP", "600")), ge=1)


class GeneratorStallScaleConfig(BaseModel):
    """Dynamic generator stall timeout scaling."""
    base: int = Field(default=int(os.getenv("WEAVE_GEN_STALL_BASE", "120")), ge=1)
    per_dep: int = Field(default=int(os.getenv("WEAVE_GEN_STALL_PER_DEP", "30")), ge=0)
    per_feature: int = Field(
        default=int(os.getenv("WEAVE_GEN_STALL_PER_FEATURE", "40")), ge=0,
        description="Extra seconds per estimated feature (#722).",
    )
    cap: int = Field(default=int(os.getenv("WEAVE_GEN_STALL_CAP", "600")), ge=1)


class NodeTimeoutConfig(BaseModel):
    """Per-agent-type node execution timeout (#360, M4.5)."""

    default_timeout: int = Field(
        default=int(os.getenv("WEAVE_NODE_TIMEOUT", os.getenv("WEAVE_AGENT_TIMEOUT", "300"))),
    )
    overrides: dict[str, int] = Field(
        default_factory=lambda: {
            "generator": int(os.getenv("WEAVE_NODE_TIMEOUT_GENERATOR", "600")),
            "evaluator": int(os.getenv("WEAVE_NODE_TIMEOUT_EVALUATOR", "480")),
        },
    )
    eval_scale: EvalTimeoutScaleConfig = Field(default_factory=EvalTimeoutScaleConfig)
    stall_timeout: int = Field(
        default=int(os.getenv("WEAVE_STALL_TIMEOUT", "120")),
    )
    stall_overrides: dict[str, int] = Field(default_factory=dict)
    eval_stall_scale: EvaluatorStallScaleConfig = Field(default_factory=EvaluatorStallScaleConfig)
    gen_stall_scale: GeneratorStallScaleConfig = Field(default_factory=GeneratorStallScaleConfig)

    def timeout_for(self, agent_type: str, artifact_count: int = 0) -> int:
        base = self.overrides.get(agent_type, self.default_timeout)
        if agent_type == "evaluator" and self.eval_scale.enabled and artifact_count > 0:
            scaled = base + artifact_count * self.eval_scale.per_file_seconds
            return min(scaled, self.eval_scale.max_timeout)
        return base

    def stall_timeout_for(
        self, agent_type: str, file_count: int = 0, test_count: int = 0,
        dep_count: int = 0, feature_count: int = 0,
    ) -> int:
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
