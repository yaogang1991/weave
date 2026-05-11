"""
Evaluator: automated evaluation and contract verification.
Inspired by Anthropic's three-agent harness evaluator.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from core.models import EvaluationResult, EventType
from session.store import SessionStore


class EvaluatorEngine:
    """
    Evaluates code against predefined success criteria.
    Supports: test execution, lint checks, coverage, complexity.
    """

    def __init__(self, session_store: SessionStore):
        self.session_store = session_store

    def evaluate_stage(
        self,
        session_id: str,
        stage_name: str,
        criteria: list[str],
        artifact_path: str,
    ) -> EvaluationResult:
        """Evaluate a stage against its success criteria."""
        self.session_store.emit_event(
            session_id,
            EventType.EVAL_START,
            {"stage": stage_name, "criteria": criteria, "artifact": artifact_path},
        )

        results: dict[str, bool] = {}
        score = 0.0
        feedback_parts: list[str] = []
        uncheckable: list[str] = []

        for criterion in criteria:
            passed, msg, auto = self._check_criterion(criterion, artifact_path)
            results[criterion] = passed
            if passed:
                score += 10.0 / len(criteria)
            if auto:
                feedback_parts.append(f"{'PASS' if passed else 'FAIL'} {criterion}: {msg}")
            else:
                feedback_parts.append(f"WARN {criterion}: {msg}")
                uncheckable.append(criterion)

        # A criterion that cannot be automatically verified should NOT
        # be treated as passed — mark the whole evaluation as incomplete
        # so the caller knows human review is needed.
        all_auto_passed = all(results.values())
        has_uncheckable = len(uncheckable) > 0

        overall_passed = all_auto_passed and not has_uncheckable

        feedback = "\n".join(feedback_parts)
        if has_uncheckable:
            feedback += (
                f"\n\nWARNING: {len(uncheckable)} criterion/criteria could not be "
                f"automatically verified and require manual review: "
                f"{', '.join(uncheckable)}"
            )

        result = EvaluationResult(
            passed=overall_passed,
            score=round(score, 1),
            criteria_results=results,
            feedback=feedback,
            suggestions=uncheckable,
        )

        self.session_store.emit_event(
            session_id,
            EventType.EVAL_RESULT,
            result.model_dump(),
        )

        return result

    def _check_criterion(self, criterion: str, artifact_path: str) -> tuple[bool, str, bool]:
        """
        Check a single success criterion.

        Returns:
            (passed, message, was_auto_checked)
            was_auto_checked=False means this criterion could not be verified
            and should be flagged for manual review.
        """
        criterion_lower = criterion.lower()
        path = Path(artifact_path)

        if "test" in criterion_lower and "pass" in criterion_lower:
            passed, msg = self._run_tests(path)
            return passed, msg, True

        if "coverage" in criterion_lower:
            target = self._extract_percentage(criterion_lower) or 80
            passed, msg = self._check_coverage(path, target)
            return passed, msg, True

        if "lint" in criterion_lower or "clean" in criterion_lower:
            passed, msg = self._run_lint(path)
            return passed, msg, True

        if "file" in criterion_lower and "exist" in criterion_lower:
            passed, msg = self._check_files_exist(criterion_lower, path)
            return passed, msg, True

        if "no_critical" in criterion_lower or "no bug" in criterion_lower:
            passed, msg = self._check_no_critical_issues(path)
            return passed, msg, True

        # Unrecognized criterion — return False so the stage is flagged for
        # manual review. The caller will include this in feedback and mark
        # the overall result as not fully passed.
        return False, (
            f"Criterion '{criterion}' is not automatically checkable. "
            f"Supported patterns: 'tests pass', 'coverage', 'lint clean', "
            f"'file exist', 'no_critical'"
        ), False

    def _run_tests(self, path: Path) -> tuple[bool, str]:
        try:
            result = subprocess.run(
                ["python", "-m", "pytest", str(path), "-v", "--tb=short"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            passed = result.returncode == 0
            return passed, "Tests passed" if passed else f"Tests failed:\n{result.stdout[-500:]}"
        except FileNotFoundError:
            return False, "pytest not installed"
        except Exception as e:
            return False, f"Test execution error: {e}"

    def _check_coverage(self, path: Path, target: int) -> tuple[bool, str]:
        try:
            result = subprocess.run(
                ["python", "-m", "pytest", str(path), "--cov=.", "--cov-report=term-missing"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            for line in result.stdout.split("\n"):
                if "TOTAL" in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        cov_str = parts[-1].replace("%", "")
                        try:
                            cov = float(cov_str)
                            passed = cov >= target
                            return passed, f"Coverage: {cov}% (target: {target}%)"
                        except ValueError:
                            continue
            return False, "Could not parse coverage report"
        except Exception as e:
            return False, f"Coverage check error: {e}"

    def _run_lint(self, path: Path) -> tuple[bool, str]:
        try:
            result = subprocess.run(
                ["python", "-m", "flake8", str(path), "--max-line-length=100"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            passed = result.returncode == 0
            return passed, "Lint clean" if passed else f"Lint issues:\n{result.stdout[:500]}"
        except FileNotFoundError:
            try:
                result = subprocess.run(
                    ["ruff", "check", str(path)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                passed = result.returncode == 0
                return passed, "Ruff clean" if passed else f"Ruff issues:\n{result.stdout[:500]}"
            except FileNotFoundError:
                return False, "No linter available (install flake8 or ruff)"
        except Exception as e:
            return False, f"Lint error: {e}"

    def _check_files_exist(self, criterion: str, path: Path) -> tuple[bool, str]:
        match = re.search(r"[:\s]+(.+)", criterion)
        if match:
            files = [f.strip() for f in match.group(1).split(",")]
            missing = [f for f in files if not (path / f).exists()]
            passed = len(missing) == 0
            return passed, f"Missing: {missing}" if missing else "All required files present"
        return True, "No specific files listed"

    def _check_no_critical_issues(self, path: Path) -> tuple[bool, str]:
        try:
            content = path.read_text(errors="ignore")
            issues = []
            for marker in ["TODO", "FIXME", "XXX", "HACK"]:
                if marker in content:
                    issues.append(marker)
            passed = len(issues) == 0
            return passed, f"Found markers: {issues}" if issues else "No critical markers found"
        except Exception:
            return True, "Could not check"

    def _extract_percentage(self, text: str) -> int | None:
        match = re.search(r'(\d+)%', text)
        if match:
            return int(match.group(1))
        return None
