"""
Bug-fix verification checkers: FILE_CHANGED, PATTERN_ABSENT, PATTERN_PRESENT.

Extracted from EvaluatorEngine as part of #178 PR 2.
"""
from __future__ import annotations

import re
from pathlib import Path

from core.models import CriterionType, SuccessCriterion
from evaluator.models import CheckResult, EvaluationContext


class BugfixPatternChecker:
    """Handles FILE_CHANGED, PATTERN_ABSENT, and PATTERN_PRESENT criteria."""

    def check(
        self,
        criterion: SuccessCriterion,
        context: EvaluationContext,
    ) -> CheckResult:
        ct = criterion.type
        if ct == CriterionType.FILE_CHANGED:
            return self._check_file_changed(criterion, context)
        if ct == CriterionType.PATTERN_ABSENT:
            return self._check_pattern_absent(criterion, context)
        if ct == CriterionType.PATTERN_PRESENT:
            return self._check_pattern_present(criterion, context)
        return CheckResult(passed=False, message=f"Unhandled criterion type: {ct}")

    def _check_file_changed(
        self,
        crit: SuccessCriterion,
        context: EvaluationContext,
    ) -> CheckResult:
        """Verify that the agent actually modified the specified file(s)."""
        output_artifacts = context.artifacts
        if not crit.path:
            if output_artifacts:
                return CheckResult(
                    passed=True,
                    message=f"Files changed: {len(output_artifacts)} file(s)",
                )
            return CheckResult(
                passed=False,
                message="No files changed (path not specified, no output_artifacts)",
            )

        target_files = [f.strip() for f in crit.path.split(",")]
        if output_artifacts:
            artifact_names = {Path(a).name for a in output_artifacts}
            missing = [f for f in target_files if Path(f).name not in artifact_names]
            if missing:
                return CheckResult(
                    passed=False,
                    message=f"Files not changed by agent: {missing}",
                )
            return CheckResult(
                passed=True,
                message=f"All target files changed: {target_files}",
            )

        return CheckResult(
            passed=False,
            message=f"No files changed by agent (expected: {target_files})",
        )

    def _check_pattern_absent(
        self,
        crit: SuccessCriterion,
        context: EvaluationContext,
    ) -> CheckResult:
        """Verify that a pattern no longer exists in the specified file."""
        if not crit.path or not crit.pattern:
            return CheckResult(
                passed=True,
                message="pattern_absent: path or pattern not specified (skipped)",
            )

        fpath = context.work_dir / crit.path
        if not fpath.exists():
            return CheckResult(
                passed=True,
                message=f"File {crit.path} does not exist (pattern trivially absent)",
            )

        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
            matches = re.findall(crit.pattern, content)
            if matches:
                return CheckResult(
                    passed=False,
                    message=(
                        f"Pattern still present in {crit.path}: "
                        f"found {len(matches)} match(es) for '{crit.pattern}'"
                    ),
                )
            return CheckResult(
                passed=True,
                message=f"Pattern '{crit.pattern}' absent from {crit.path}",
            )
        except re.error as e:
            return CheckResult(
                passed=False,
                message=f"Invalid regex pattern '{crit.pattern}': {e}",
            )

    def _check_pattern_present(
        self,
        crit: SuccessCriterion,
        context: EvaluationContext,
    ) -> CheckResult:
        """Verify that a pattern exists in the specified file."""
        if not crit.path or not crit.pattern:
            return CheckResult(
                passed=True,
                message="pattern_present: path or pattern not specified (skipped)",
            )

        fpath = context.work_dir / crit.path
        if not fpath.exists():
            return CheckResult(passed=False, message=f"File {crit.path} does not exist")

        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
            matches = re.findall(crit.pattern, content)
            if not matches:
                return CheckResult(
                    passed=False,
                    message=(
                        f"Pattern not found in {crit.path}: "
                        f"expected '{crit.pattern}'"
                    ),
                )
            return CheckResult(
                passed=True,
                message=f"Pattern '{crit.pattern}' found in {crit.path} ({len(matches)} match(es))",
            )
        except re.error as e:
            return CheckResult(
                passed=False,
                message=f"Invalid regex pattern '{crit.pattern}': {e}",
            )
