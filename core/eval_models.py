"""Evaluation domain models -- success criteria and evaluation results."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EvalStatus(str, Enum):
    """Evaluation result status -- maps to NodeStatus in DAG engine."""
    CLEAN_PASS = "clean_pass"      # All criteria passed
    PARTIAL_PASS = "partial_pass"  # Passed via threshold (soft failures)
    WARNED = "warned"              # Passed with uncheckable/warned criteria
    FAILED = "failed"              # Did not pass


class EvaluationResult(BaseModel):
    """Result of an evaluation pass."""
    passed: bool
    score: float = 0.0
    criteria_results: dict[str, bool] = Field(default_factory=dict)
    feedback: str = ""
    suggestions: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    eval_status: EvalStatus = EvalStatus.CLEAN_PASS


class CriterionType(str, Enum):
    """Structured criterion types for evaluator dispatch."""
    TESTS_PASS = "tests_pass"
    LINT = "lint"
    FILE_EXISTS = "file_exists"
    FILE_PATTERN = "file_pattern"
    COVERAGE = "coverage"
    NO_CRITICAL = "no_critical"
    FILE_CHANGED = "file_changed"
    PATTERN_ABSENT = "pattern_absent"
    PATTERN_PRESENT = "pattern_present"
    TEST_FILE_EXISTS = "test_file_exists"
    CUSTOM = "custom"


class SuccessCriterion(BaseModel):
    """Structured success criterion for evaluation."""
    type: CriterionType = CriterionType.CUSTOM
    test_path: str = ""
    path: str = ""
    pattern: str = ""
    target: float | None = None
    description: str = ""
