"""
CriterionChecker protocol: pluggable interface for evaluation checkers.

Part of #178 PR 1: modularize EvaluatorEngine and formalize evaluation contracts.
"""
from __future__ import annotations

from typing import Protocol

from core.models import SuccessCriterion
from evaluator.models import EvaluationContext, CheckResult


class CriterionChecker(Protocol):
    """Protocol for criterion checkers.

    Each checker handles one or more CriterionType values.
    The engine dispatches to the appropriate checker based on criterion type.
    """

    def check(
        self,
        criterion: SuccessCriterion,
        context: EvaluationContext,
    ) -> CheckResult: ...
