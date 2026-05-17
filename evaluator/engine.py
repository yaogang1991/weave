"""
Evaluator: automated evaluation and contract verification.

Supports both legacy list[str] criteria and structured SuccessCriterion.
All internal checkers return 2-tuples (bool, str) for consistency.
The public _check_criterion returns 3-tuples (bool, str, bool) for the
was_auto_checked protocol used by evaluate_stage.

Security: never executes arbitrary commands from LLM output. TESTS_PASS
runs a fixed ``python -m pytest`` via subprocess with shell=False.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from core.models import (
    CriterionType,
    EvalStatus,
    EvaluationResult,
    EventType,
    SuccessCriterion,
)
from evaluator.artifact import resolve_artifact_path, scope_artifacts_to_criteria
from evaluator.compat import normalize_criteria
from evaluator.lint.parser import LintIssue, parse_flake8_output  # noqa: F401 — backward compat
from evaluator.models import CheckResult, CheckSeverity, EvaluationContext
from evaluator.runner import (
    auto_fix_unused,
    auto_format_apply,
    check_coverage,
    check_files_exist,
    check_files_exist_loose,
    check_no_critical,
    detect_shadowing_test_inits,
    extract_percentage,
    find_test_files,
    import_smoke_test,
    isolated_env,
    run_lint,
    run_tests,
    safe_eval_id,
)
from session.store import SessionStore

logger = logging.getLogger(__name__)


class EvaluatorEngine:
    """
    Evaluates code against predefined success criteria.

    Supports: test execution, lint checks, coverage, file existence,
    no-critical-issues check. Accepts list[str] (legacy) and
    list[SuccessCriterion] (structured).
    """

    # Hard criteria that can never be downgraded from FAIL to WARN,
    # even when the overall score meets pass_threshold.
    HARD_CRITERIA: set[CriterionType] = {
        CriterionType.FILE_EXISTS,
        CriterionType.FILE_PATTERN,
        CriterionType.TESTS_PASS,
        CriterionType.PATTERN_PRESENT,
        CriterionType.PATTERN_ABSENT,
    }

    def __init__(
        self,
        session_store: SessionStore,
        pass_threshold: float | None = None,
        auto_format_before_eval: bool = False,
    ):
        self.session_store = session_store
        self.auto_format_before_eval = auto_format_before_eval
        if pass_threshold is not None:
            if pass_threshold <= 0:
                raise ValueError(
                    f"pass_threshold must be > 0, got {pass_threshold}. "
                    "A threshold of 0 would pass all criteria regardless."
                )
            if pass_threshold > 10:
                raise ValueError(
                    f"pass_threshold must be <= 10, got {pass_threshold}."
                )
        self.pass_threshold = pass_threshold
        self._last_autofixed: list[str] = []
        self._last_auto_formatted: list[str] = []
        self._last_lint_new_issues: list[str] = []
        self._last_lint_all_issues: list[str] = []
        self._checkers: dict[CriterionType, Any] = {}
        self._register_builtin_checkers()

    def _register_builtin_checkers(self) -> None:
        """Register extracted criterion checkers (#178 PR 2)."""
        from evaluator.checkers.file_exists import FileExistsChecker
        from evaluator.checkers.bugfix_patterns import BugfixPatternChecker

        file_checker = FileExistsChecker()
        self._checkers[CriterionType.FILE_EXISTS] = file_checker
        self._checkers[CriterionType.FILE_PATTERN] = file_checker
        self._checkers[CriterionType.TEST_FILE_EXISTS] = file_checker

        bugfix_checker = BugfixPatternChecker()
        self._checkers[CriterionType.FILE_CHANGED] = bugfix_checker
        self._checkers[CriterionType.PATTERN_ABSENT] = bugfix_checker
        self._checkers[CriterionType.PATTERN_PRESENT] = bugfix_checker

    def register_checker(
        self,
        criterion_type: CriterionType,
        checker: Any,
    ) -> None:
        """Register a pluggable criterion checker (#178)."""
        self._checkers[criterion_type] = checker

    def _try_registered_checker(
        self,
        crit: SuccessCriterion,
        context: EvaluationContext,
    ) -> tuple[bool, str, bool] | None:
        """Dispatch to a registered checker if one exists for this type."""
        checker = self._checkers.get(crit.type)
        if checker is None:
            return None
        result: CheckResult = checker.check(crit, context)
        was_auto = result.severity in (CheckSeverity.NORMAL, CheckSeverity.ERROR)
        return result.passed, result.message, was_auto

    def evaluate_stage(
        self,
        session_id: str,
        stage_name: str,
        criteria: list[str | SuccessCriterion],
        artifact_path: str,
        work_dir: str | None = None,
        output_artifacts: list[str] | None = None,
        owned_files: list[str] | None = None,
    ) -> EvaluationResult:
        """Evaluate a stage against its success criteria."""
        eval_dir = work_dir or artifact_path
        eval_id = f"{session_id}_{stage_name}"

        self.session_store.emit_event(
            session_id,
            EventType.EVAL_START,
            {"stage": stage_name, "criteria": [str(c) for c in criteria], "artifact": artifact_path},
        )

        structured = normalize_criteria(criteria)

        output_artifacts = scope_artifacts_to_criteria(
            output_artifacts, structured, Path(eval_dir) if eval_dir else None,
            owned_files=owned_files,
        )

        results: dict[str] = {}
        hard_labels: set[str] = set()
        score = 0.0
        feedback_parts: list[str] = []
        uncheckable: list[str] = []

        for crit in structured:
            passed, msg, auto = self._check_criterion(crit, eval_dir, output_artifacts, eval_id)
            label = crit.description or crit.path or crit.test_path or crit.type.value
            results[label] = passed
            if crit.type in self.HARD_CRITERIA:
                hard_labels.add(label)
            if passed:
                score += 10.0 / max(len(structured), 1)
            if auto:
                feedback_parts.append(f"{'PASS' if passed else 'FAIL'} {label}: {msg}")
            else:
                feedback_parts.append(f"WARN {label}: {msg}")
                uncheckable.append(label)

        all_auto_passed = all(results.values())
        has_uncheckable = len(uncheckable) > 0

        # Threshold-based passing (#194)
        failed_auto = [
            label for label, ok in results.items() if not ok
        ]
        failed_hard = [label for label in failed_auto if label in hard_labels]
        threshold_pass = False
        if self.pass_threshold is not None and not all_auto_passed:
            if failed_hard:
                overall_passed = False
            elif score >= self.pass_threshold:
                soft_failed = [l for l in failed_auto if l not in hard_labels]
                feedback_parts_new: list[str] = []
                for part in feedback_parts:
                    for label in soft_failed:
                        prefix = f"FAIL {label}:"
                        if part.startswith(prefix):
                            part = f"WARN {part[5:]}"
                            break
                    feedback_parts_new.append(part)
                feedback_parts = feedback_parts_new
                overall_passed = True
                threshold_pass = True
            else:
                overall_passed = False
        else:
            overall_passed = all_auto_passed

        # Mandatory artifact verification (#234)
        if output_artifacts and eval_dir:
            eval_root = Path(eval_dir)
            phantom = []
            resolved_artifacts = []
            for art in output_artifacts:
                resolved = resolve_artifact_path(art, eval_root)
                if resolved is None:
                    phantom.append(art)
                else:
                    resolved_artifacts.append(str(resolved))
            if phantom:
                overall_passed = False
                score = 0.0
                feedback_parts.append(
                    f"FAIL artifact_verification: {len(phantom)} reported "
                    f"artifact(s) not found on disk: {phantom}"
                )
            elif resolved_artifacts != list(output_artifacts):
                output_artifacts = resolved_artifacts

        # Zero-output guard (#372)
        if (output_artifacts is not None
                and len(output_artifacts) == 0
                and any(
                    c.type == CriterionType.FILE_EXISTS for c in structured
                )):
            overall_passed = False
            score = 0.0
            feedback_parts.append(
                "FAIL zero_output: generator produced no output files "
                "but FILE_EXISTS criteria present"
            )

        # Import smoke test (#344)
        if output_artifacts and eval_dir:
            import_errors = import_smoke_test(
                output_artifacts, Path(eval_dir),
            )
            if import_errors:
                overall_passed = False
                for err_file, err_msg in import_errors:
                    feedback_parts.append(
                        f"FAIL import_check: {err_file} — {err_msg}"
                    )

        feedback = "\n".join(feedback_parts)
        if has_uncheckable:
            feedback += (
                f"\n\nWARNING: {len(uncheckable)} criterion/criteria could not be "
                f"automatically verified and require manual review: "
                f"{', '.join(uncheckable)}"
            )

        # Build metadata with structured lint issue data (#151)
        eval_metadata: dict[str, Any] = {}
        if self._last_lint_all_issues:
            eval_metadata["lint_all_issues"] = self._last_lint_all_issues
        if self._last_lint_new_issues:
            eval_metadata["lint_new_issues"] = self._last_lint_new_issues

        # Determine eval_status for DAG engine node state mapping (#270).
        if not overall_passed:
            eval_status = EvalStatus.FAILED
        elif threshold_pass:
            eval_status = EvalStatus.PARTIAL_PASS
        elif has_uncheckable:
            eval_status = EvalStatus.WARNED
        else:
            eval_status = EvalStatus.CLEAN_PASS

        result = EvaluationResult(
            passed=overall_passed,
            score=round(score, 1),
            criteria_results=results,
            feedback=feedback,
            suggestions=uncheckable,
            metadata=eval_metadata,
            eval_status=eval_status,
        )

        self.session_store.emit_event(
            session_id,
            EventType.EVAL_RESULT,
            result.model_dump(),
        )

        if self._last_autofixed:
            self.session_store.emit_event(
                session_id,
                EventType.EVAL_AUTOFIX_APPLIED,
                {
                    "tool": "autoflake",
                    "files": self._last_autofixed,
                    "stage": stage_name,
                },
            )

        if self._last_auto_formatted:
            self.session_store.emit_event(
                session_id,
                EventType.EVAL_AUTOFIX_APPLIED,
                {
                    "tool": "autopep8",
                    "files": self._last_auto_formatted,
                    "stage": stage_name,
                },
            )

        return result

    # ------------------------------------------------------------------
    # Dispatch — returns 3-tuple (passed, msg, was_auto)
    # ------------------------------------------------------------------

    def _check_criterion(
        self,
        crit: SuccessCriterion,
        work_dir: str,
        output_artifacts: list[str] | None = None,
        eval_id: str = "",
    ) -> tuple[bool, str, bool]:
        # Try pluggable checker first (#178)
        context = EvaluationContext(
            work_dir=Path(work_dir),
            artifacts=output_artifacts,
            session_store=self.session_store,
        )
        registered = self._try_registered_checker(crit, context)
        if registered is not None:
            return registered

        if crit.type == CriterionType.TESTS_PASS:
            test_targets = None
            if crit.test_path:
                test_targets = crit.test_path
            elif output_artifacts:
                test_targets = find_test_files(output_artifacts, Path(work_dir))
            if not test_targets:
                return True, (
                    "No test files found to run — tests not verified. "
                    "Consider adding test files or adjusting criteria."
                ), False
            passed, msg = run_tests(Path(work_dir), test_targets, eval_id)
            return passed, msg, True

        if crit.type == CriterionType.LINT:
            if not output_artifacts:
                return True, "No files to lint (passed by default)", True
            passed, msg, autofixed, formatted, new_issues, all_issues = run_lint(
                output_artifacts, Path(work_dir), self.auto_format_before_eval,
            )
            self._last_autofixed = autofixed
            self._last_auto_formatted = formatted
            self._last_lint_new_issues = new_issues
            self._last_lint_all_issues = all_issues
            # When no linter is available, treat as uncheckable (WARN)
            if not passed and "No linter available" in msg:
                return True, (
                    f"Lint skipped: {msg}. "
                    f"Install flake8 or ruff for lint checking."
                ), False
            return passed, msg, True

        if crit.type == CriterionType.COVERAGE:
            target = int(crit.target) if crit.target else 80
            return check_coverage(
                Path(work_dir), target, output_artifacts, eval_id,
            )

        if crit.type == CriterionType.NO_CRITICAL:
            passed, msg = check_no_critical(Path(work_dir), output_artifacts)
            return passed, msg, True

        # CUSTOM + any unknown type -> pass with warning
        return True, (
            f"Cannot auto-verify: {crit.description}. "
            f"Assumed passed — manual review recommended."
        ), False

    # ------------------------------------------------------------------
    # Backward-compat static/instance methods that delegate to runner
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_eval_id(eval_id: str) -> str:
        return safe_eval_id(eval_id)

    def _isolated_env(self, eval_id: str = "", work_dir: Path | None = None) -> dict[str, str]:
        return isolated_env(eval_id, work_dir)

    def _find_test_files(self, output_artifacts: list[str], work_dir: Path) -> list[str]:
        return find_test_files(output_artifacts, work_dir)

    def _run_tests(self, work_dir: Path, test_path: str | list[str] | None = None, eval_id: str = "") -> tuple[bool, str]:
        return run_tests(work_dir, test_path, eval_id)

    @staticmethod
    def _detect_shadowing_test_inits(work_dir: Path) -> list[str]:
        return detect_shadowing_test_inits(work_dir)

    def _run_lint(self, targets: list[str], work_dir: Path) -> tuple[bool, str]:
        passed, msg, autofixed, formatted, new_issues, all_issues = run_lint(
            targets, work_dir, self.auto_format_before_eval,
        )
        self._last_autofixed = autofixed
        self._last_auto_formatted = formatted
        self._last_lint_new_issues = new_issues
        self._last_lint_all_issues = all_issues
        return passed, msg

    def _auto_fix_unused(self, resolved: list[str], work_dir: Path) -> list[str]:
        return auto_fix_unused(resolved, work_dir)

    def _auto_format_apply(self, resolved: list[str], work_dir: Path) -> list[str]:
        return auto_format_apply(resolved, work_dir, self.auto_format_before_eval)

    def _check_files_exist(self, files: list[str], base: Path) -> tuple[bool, str]:
        return check_files_exist(files, base)

    def _check_files_exist_loose(self, patterns: list[str], base: Path) -> tuple[bool, str]:
        return check_files_exist_loose(patterns, base)

    def _check_coverage(self, work_dir: Path, target: int, output_artifacts: list[str] | None = None, eval_id: str = "") -> tuple[bool, str, bool]:
        return check_coverage(work_dir, target, output_artifacts, eval_id)

    def _check_no_critical(self, path: Path, artifacts: list[str] | None = None) -> tuple[bool, str]:
        return check_no_critical(path, artifacts)

    def _extract_percentage(self, text: str) -> int | None:
        return extract_percentage(text)

    @staticmethod
    def _import_smoke_test(artifacts: list[str], eval_dir: Path) -> list[tuple[str, str]]:
        return import_smoke_test(artifacts, eval_dir)

    @staticmethod
    def _resolve_artifact_path(artifact: str, eval_root: Path) -> Path | None:
        return resolve_artifact_path(artifact, eval_root)

    @staticmethod
    def _scope_artifacts_to_criteria(
        output_artifacts: list[str] | None,
        criteria: list[SuccessCriterion],
        work_dir: Path | None,
        owned_files: list[str] | None = None,
    ) -> list[str] | None:
        return scope_artifacts_to_criteria(output_artifacts, criteria, work_dir, owned_files)
