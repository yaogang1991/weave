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

import json
import re
import subprocess
from pathlib import Path

from core.models import (
    CriterionType,
    EvaluationResult,
    EventType,
    SuccessCriterion,
)
from session.store import SessionStore


class EvaluatorEngine:
    """
    Evaluates code against predefined success criteria.

    Supports: test execution, lint checks, coverage, file existence,
    no-critical-issues check. Accepts list[str] (legacy) and
    list[SuccessCriterion] (structured).
    """

    def __init__(self, session_store: SessionStore):
        self.session_store = session_store

    def evaluate_stage(
        self,
        session_id: str,
        stage_name: str,
        criteria: list[str | SuccessCriterion],
        artifact_path: str,
        work_dir: str | None = None,
        output_artifacts: list[str] | None = None,
    ) -> EvaluationResult:
        """Evaluate a stage against its success criteria."""
        eval_dir = work_dir or artifact_path

        self.session_store.emit_event(
            session_id,
            EventType.EVAL_START,
            {"stage": stage_name, "criteria": [str(c) for c in criteria], "artifact": artifact_path},
        )

        structured = self._normalize_criteria(criteria)

        results: dict[str, bool] = {}
        score = 0.0
        feedback_parts: list[str] = []
        uncheckable: list[str] = []

        for crit in structured:
            passed, msg, auto = self._check_criterion(crit, eval_dir, output_artifacts)
            label = crit.description or crit.path or crit.test_path or crit.type.value
            results[label] = passed
            if passed:
                score += 10.0 / max(len(structured), 1)
            if auto:
                feedback_parts.append(f"{'PASS' if passed else 'FAIL'} {label}: {msg}")
            else:
                feedback_parts.append(f"WARN {label}: {msg}")
                uncheckable.append(label)

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

    # ------------------------------------------------------------------
    # Criteria normalization
    # ------------------------------------------------------------------

    def _normalize_criteria(self, criteria: list[str | SuccessCriterion]) -> list[SuccessCriterion]:
        """Parse list[str | SuccessCriterion] into list[SuccessCriterion].

        SuccessCriterion instances are preserved as-is.
        Strings that are valid JSON with a 'type' key are deserialized as
        structured criteria (backward compatibility with serialized data).
        Plain strings go through legacy keyword matching.
        """
        result: list[SuccessCriterion] = []
        for c in criteria:
            if isinstance(c, SuccessCriterion):
                result.append(c)
                continue
            if isinstance(c, str) and c.startswith("{"):
                try:
                    data = json.loads(c)
                    if isinstance(data, dict) and "type" in data:
                        result.append(SuccessCriterion(**data))
                        continue
                except (json.JSONDecodeError, Exception):
                    pass
            result.append(self._parse_string_criterion(c))
        return result

    def _parse_string_criterion(self, criterion: str) -> SuccessCriterion:
        lower = criterion.lower()
        if "test" in lower and "pass" in lower:
            return SuccessCriterion(type=CriterionType.TESTS_PASS, description=criterion)
        if "coverage" in lower:
            return SuccessCriterion(type=CriterionType.COVERAGE, target=float(self._extract_percentage(lower) or 80), description=criterion)
        if "lint" in lower or "clean" in lower:
            return SuccessCriterion(type=CriterionType.LINT, description=criterion)
        if "file" in lower and "exist" in lower:
            match = re.search(r"[:\s]+(.+)", lower)
            return SuccessCriterion(type=CriterionType.FILE_EXISTS, path=match.group(1) if match else "", description=criterion)
        if "no_critical" in lower or "no bug" in lower:
            return SuccessCriterion(type=CriterionType.NO_CRITICAL, description=criterion)
        return SuccessCriterion(type=CriterionType.CUSTOM, description=criterion)

    # ------------------------------------------------------------------
    # Dispatch — returns 3-tuple (passed, msg, was_auto)
    # ------------------------------------------------------------------

    def _check_criterion(
        self,
        crit: SuccessCriterion,
        work_dir: str,
        output_artifacts: list[str] | None = None,
    ) -> tuple[bool, str, bool]:
        if crit.type == CriterionType.TESTS_PASS:
            passed, msg = self._run_tests(Path(work_dir), crit.test_path or None)
            return passed, msg, True

        if crit.type == CriterionType.LINT:
            targets = output_artifacts or [work_dir]
            passed, msg = self._run_lint(targets, Path(work_dir))
            return passed, msg, True

        if crit.type == CriterionType.FILE_EXISTS:
            files_str = crit.path
            files = [f.strip() for f in files_str.split(",")] if files_str else (output_artifacts or [])
            if not files:
                return True, "No specific files listed", True
            passed, msg = self._check_files_exist(files, Path(work_dir))
            return passed, msg, True

        if crit.type == CriterionType.COVERAGE:
            target = int(crit.target) if crit.target else 80
            passed, msg = self._check_coverage(Path(work_dir), target)
            return passed, msg, True

        if crit.type == CriterionType.NO_CRITICAL:
            passed, msg = self._check_no_critical(Path(work_dir), output_artifacts)
            return passed, msg, True

        # CUSTOM + any unknown type → uncheckable
        return False, (
            f"Criterion '{crit.description}' is not automatically checkable. "
            f"Supported types: tests_pass, lint, file_exists, coverage, no_critical"
        ), False

    # ------------------------------------------------------------------
    # Internal checkers — all return 2-tuple (bool, str)
    # ------------------------------------------------------------------

    def _run_tests(self, work_dir: Path, test_path: str | None = None) -> tuple[bool, str]:
        """Run pytest with a fixed command. Never executes arbitrary commands."""
        try:
            cmd = ["python", "-m", "pytest", "-v", "--tb=short"]
            if test_path:
                cmd.append(test_path)
            result = subprocess.run(
                cmd,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=120,
                cwd=str(work_dir) if work_dir.is_dir() else None,
            )
            passed = result.returncode == 0
            return passed, "Tests passed" if passed else f"Tests failed:\n{result.stdout[-500:]}"
        except FileNotFoundError:
            return False, "pytest not installed"
        except Exception as e:
            return False, f"Test execution error: {e}"

    def _run_lint(self, targets: list[str], work_dir: Path) -> tuple[bool, str]:
        """Run flake8/ruff on all targets in a single batch call."""
        resolved = []
        for t in targets:
            p = work_dir / t if (work_dir / t).exists() else Path(t)
            resolved.append(str(p))
        if not resolved:
            return True, "No targets to lint"
        try:
            result = subprocess.run(
                ["python", "-m", "flake8"] + resolved + ["--max-line-length=100"],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=60,
            )
            if result.returncode == 0:
                return True, "Lint clean"
            return False, f"Lint issues:\n{result.stdout[:500]}"
        except FileNotFoundError:
            try:
                result = subprocess.run(
                    ["ruff", "check"] + resolved,
                    capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=60,
                )
                if result.returncode == 0:
                    return True, "Ruff clean"
                return False, f"Ruff issues:\n{result.stdout[:500]}"
            except FileNotFoundError:
                return False, "No linter available (install flake8 or ruff)"
        except Exception as e:
            return False, f"Lint error: {e}"

    def _check_files_exist(self, files: list[str], base: Path) -> tuple[bool, str]:
        missing = [f for f in files if not (base / f).exists()]
        passed = len(missing) == 0
        return passed, f"Missing: {missing}" if missing else "All required files present"

    def _check_coverage(self, path: Path, target: int) -> tuple[bool, str]:
        try:
            result = subprocess.run(
                ["python", "-m", "pytest", str(path), "--cov=.", "--cov-report=term-missing"],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120,
            )
            for line in result.stdout.split("\n"):
                if "TOTAL" in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        cov_str = parts[-1].replace("%", "")
                        try:
                            cov = float(cov_str)
                            return cov >= target, f"Coverage: {cov}% (target: {target}%)"
                        except ValueError:
                            continue
            return False, "Could not parse coverage report"
        except Exception as e:
            return False, f"Coverage check error: {e}"

    def _check_no_critical(self, path: Path, artifacts: list[str] | None = None) -> tuple[bool, str]:
        targets = artifacts or []
        if not targets:
            return True, "No artifacts to check"
        issues = []
        for fname in targets:
            fpath = path / fname
            if not fpath.exists():
                continue
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
                for marker in ["TODO", "FIXME", "XXX", "HACK"]:
                    if marker in content:
                        issues.append(f"{fname}: {marker}")
            except Exception:
                pass
        passed = len(issues) == 0
        return passed, f"Found markers: {issues}" if issues else "No critical markers found"

    def _extract_percentage(self, text: str) -> int | None:
        match = re.search(r'(\d+)%', text)
        return int(match.group(1)) if match else None
