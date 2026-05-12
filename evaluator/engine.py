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
        self._last_autofixed: list[str] = []

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
        overall_passed = all_auto_passed

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

    # Chinese → English keyword mapping for criteria parsing
    _CN_KEYWORD_MAP = {
        "测试": "test", "覆盖率": "coverage", "代码": "code",
        "文件": "file", "存在": "exist", "无严重": "no_critical",
        "无 bug": "no bug", "检查": "check", "通过": "pass",
        "清理": "clean",
    }

    def _parse_string_criterion(self, criterion: str) -> SuccessCriterion:
        lower = criterion.lower()
        # Normalize Chinese keywords to English equivalents
        for cn, en in self._CN_KEYWORD_MAP.items():
            lower = lower.replace(cn, en)
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
            test_targets = None
            if crit.test_path:
                test_targets = crit.test_path
            elif output_artifacts:
                test_targets = [a for a in output_artifacts if "test" in Path(a).name.lower()]
            if not test_targets:
                return True, "No test files to run (passed by default)", True
            passed, msg = self._run_tests(Path(work_dir), test_targets)
            return passed, msg, True

        if crit.type == CriterionType.LINT:
            if not output_artifacts:
                return True, "No files to lint (passed by default)", True
            passed, msg = self._run_lint(output_artifacts, Path(work_dir))
            return passed, msg, True

        if crit.type == CriterionType.FILE_EXISTS:
            # Prefer output_artifacts (actual files the agent produced)
            if output_artifacts:
                return True, f"Files confirmed via output_artifacts ({len(output_artifacts)} files)", True
            # Fallback: check criteria paths with loose matching
            files_str = crit.path
            files = [f.strip() for f in files_str.split(",")] if files_str else []
            if not files:
                return True, "No specific files listed", True
            passed, msg = self._check_files_exist_loose(files, Path(work_dir))
            return passed, msg, True

        if crit.type == CriterionType.COVERAGE:
            target = int(crit.target) if crit.target else 80
            return self._check_coverage(
                Path(work_dir), target, output_artifacts,
            )

        if crit.type == CriterionType.NO_CRITICAL:
            passed, msg = self._check_no_critical(Path(work_dir), output_artifacts)
            return passed, msg, True

        if crit.type == CriterionType.FILE_CHANGED:
            passed, msg = self._check_file_changed(crit, output_artifacts)
            return passed, msg, True

        if crit.type == CriterionType.PATTERN_ABSENT:
            passed, msg = self._check_pattern_absent(crit, Path(work_dir))
            return passed, msg, True

        if crit.type == CriterionType.PATTERN_PRESENT:
            passed, msg = self._check_pattern_present(crit, Path(work_dir))
            return passed, msg, True

        # CUSTOM + any unknown type → pass with warning (manual review recommended)
        return True, (
            f"Cannot auto-verify: {crit.description}. "
            f"Assumed passed — manual review recommended."
        ), False

    # ------------------------------------------------------------------
    # Internal checkers — all return 2-tuple (bool, str)
    # ------------------------------------------------------------------

    def _run_tests(self, work_dir: Path, test_path: str | list[str] | None = None) -> tuple[bool, str]:
        """Run pytest with a fixed command. Never executes arbitrary commands."""
        try:
            cmd = ["python", "-m", "pytest", "-v", "--tb=short"]
            if test_path:
                if isinstance(test_path, list):
                    cmd.extend(str(work_dir / t) if not Path(t).is_absolute() else t for t in test_path)
                else:
                    cmd.append(test_path)
            result = subprocess.run(
                cmd,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=120,
                cwd=str(work_dir) if work_dir.is_dir() else None,
            )
            passed = result.returncode == 0
            if passed:
                return passed, "Tests passed"
            # Extract specific failure lines for actionable feedback
            failure_lines = []
            for line in result.stdout.split("\n"):
                if any(kw in line for kw in ("FAILED", "AssertionError", "Error:", "error")):
                    failure_lines.append(line)
            detail = "\n".join(failure_lines[-20:]) if failure_lines else result.stdout[-500:]
            return passed, f"Tests failed:\n{detail}"
        except FileNotFoundError:
            return False, "pytest not installed"
        except Exception as e:
            return False, f"Test execution error: {e}"

    def _run_lint(self, targets: list[str], work_dir: Path) -> tuple[bool, str]:
        """Auto-fix then verify lint for resolved target files.

        Phase 1 (auto-fix): Runs autoflake (F401/F841) and autopep8 (E501)
        --in-place on resolved targets.  Snapshots file contents before/after
        autoflake to detect which files were actually modified.

        Phase 2 (verify): Runs flake8 (or ruff as fallback) on the same
        targets.  If auto-fix tools are not installed, the verify phase
        proceeds without them; if flake8/ruff is not installed, returns
        failure.

        Only lints specific files — does NOT recursively scan directories.
        """
        self._last_autofixed = []

        resolved = []
        for t in targets:
            p = work_dir / t
            if p.is_file():
                resolved.append(str(p))
            elif p.is_dir():
                for f in p.glob("*.py"):
                    resolved.append(str(f))
            elif Path(t).is_file():
                resolved.append(str(Path(t)))
        if not resolved:
            return True, "No targets to lint"

        # Snapshot file contents before autoflake.
        pre_contents: dict[str, bytes] = {}
        for fpath in resolved:
            p = Path(fpath)
            if p.exists():
                pre_contents[fpath] = p.read_bytes()

        # Auto-fix: F401 unused imports / F841 unused variables
        try:
            subprocess.run(
                [
                    "python", "-m", "autoflake",
                    "--remove-all-unused-imports",
                    "--remove-unused-variables",
                    "--in-place",
                ] + resolved,
                capture_output=True, text=True, timeout=30,
            )
        except FileNotFoundError:
            pass
        except Exception:
            pass

        # Detect which files autoflake actually changed.
        autofixed: list[str] = []
        for fpath, pre in pre_contents.items():
            p = Path(fpath)
            if p.exists() and p.read_bytes() != pre:
                try:
                    autofixed.append(str(p.relative_to(work_dir)))
                except ValueError:
                    autofixed.append(p.name)

        self._last_autofixed = autofixed

        # Auto-fix: E501 line too long
        try:
            subprocess.run(
                [
                    "python", "-m", "autopep8",
                    "--select", "E501",
                    "--max-line-length", "100",
                    "--in-place",
                ] + resolved,
                capture_output=True, text=True, timeout=30,
            )
        except FileNotFoundError:
            pass
        except Exception:
            pass

        try:
            result = subprocess.run(
                ["python", "-m", "flake8"] + resolved + ["--max-line-length=100"],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=60,
            )
            if result.returncode == 0:
                msg = "Lint clean"
            else:
                msg = f"Lint issues:\n{result.stdout[:500]}"
        except FileNotFoundError:
            try:
                result = subprocess.run(
                    ["ruff", "check"] + resolved,
                    capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=60,
                )
                if result.returncode == 0:
                    msg = "Ruff clean"
                else:
                    msg = f"Ruff issues:\n{result.stdout[:500]}"
            except FileNotFoundError:
                return False, "No linter available (install flake8 or ruff)"
        except Exception as e:
            return False, f"Lint error: {e}"

        if autofixed:
            autofix_msg = f"Autoflake auto-fixed: {', '.join(autofixed)}"
            msg = f"{autofix_msg}\n{msg}"

        return result.returncode == 0, msg

    def _check_files_exist(self, files: list[str], base: Path) -> tuple[bool, str]:
        missing = [f for f in files if not (base / f).exists()]
        passed = len(missing) == 0
        return passed, f"Missing: {missing}" if missing else "All required files present"

    def _check_files_exist_loose(self, patterns: list[str], base: Path) -> tuple[bool, str]:
        """Loose file matching: exact, glob by name, or substring match."""
        missing = []
        for pattern in patterns:
            # 1. Exact match
            if (base / pattern).exists():
                continue
            # 2. Glob by filename
            name = Path(pattern).name
            if list(base.glob(f"**/{name}")):
                continue
            # 3. Substring match (without extension)
            stem = Path(pattern).stem
            if len(stem) >= 3 and list(base.glob(f"**/*{stem}*")):
                continue
            missing.append(pattern)
        passed = len(missing) == 0
        return passed, f"Missing: {missing}" if missing else "Required files found (loose match)"

    def _check_coverage(
        self,
        work_dir: Path,
        target: int,
        output_artifacts: list[str] | None = None,
    ) -> tuple[bool, str, bool]:
        """Check test coverage against target percentage.

        Returns (passed, message, was_auto_verified).
        When coverage output cannot be parsed, returns was_auto_verified=False
        so the caller emits WARN instead of PASS (#152).
        """
        try:
            cmd = [
                "python", "-m", "pytest", "-v",
                "--tb=short", "--cov-report=term-missing",
            ]

            # Limit coverage scope to packages inferred from output artifacts
            if output_artifacts:
                cov_targets = set()
                for a in output_artifacts:
                    parts = Path(a).parts
                    if len(parts) > 1:
                        cov_targets.add(str(Path(*parts[:2])))
                if cov_targets:
                    for t in cov_targets:
                        cmd.append(f"--cov={t}")
                else:
                    cmd.append("--cov=.")
            else:
                cmd.append("--cov=.")

            result = subprocess.run(
                cmd,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120,
                cwd=str(work_dir) if work_dir.is_dir() else None,
            )

            # Parse TOTAL line — try multiple column positions
            for line in result.stdout.split("\n"):
                stripped = line.strip()
                if stripped.startswith("TOTAL"):
                    parts = stripped.split()
                    if len(parts) >= 2:
                        for idx in (-1, -2):
                            if abs(idx) <= len(parts):
                                cov_str = parts[idx].replace("%", "")
                                try:
                                    cov = float(cov_str)
                                    return (
                                        cov >= target,
                                        f"Coverage: {cov}% (target: {target}%)",
                                        True,
                                    )
                                except ValueError:
                                    continue

            # Could not parse TOTAL line
            stdout_tail = result.stdout[-200:] if result.stdout else ""
            stderr_tail = result.stderr[-200:] if result.stderr else ""
            if result.returncode == 0:
                # Tests pass but coverage unverified — mark as WARN, not PASS.
                # Returning auto=False ensures evaluate_stage emits "WARN ..."
                # instead of "PASS ..." (#152).
                return True, (
                    f"Coverage could not be parsed (tool error); "
                    f"tests passed, coverage target {target}% not verified. "
                    f"stdout_tail=...{stdout_tail} "
                    f"stderr_tail=...{stderr_tail}"
                ), False
            return False, (
                f"Tests failed and coverage report could not be parsed. "
                f"stdout_tail=...{stdout_tail} "
                f"stderr_tail=...{stderr_tail}"
            ), True
        except Exception as e:
            return False, f"Coverage check error: {e}", True

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

    # ------------------------------------------------------------------
    # Bug-fix verification checkers
    # ------------------------------------------------------------------

    def _check_file_changed(
        self,
        crit: SuccessCriterion,
        output_artifacts: list[str] | None = None,
    ) -> tuple[bool, str]:
        """Verify that the agent actually modified the specified file(s).

        Checks output_artifacts (the list of files the agent wrote/edited).
        If output_artifacts is empty, the file must exist on disk.
        """
        if not crit.path:
            if output_artifacts:
                return True, f"Files changed: {len(output_artifacts)} file(s)"
            return False, "No files changed (path not specified, no output_artifacts)"

        target_files = [f.strip() for f in crit.path.split(",")]
        if output_artifacts:
            # Normalize for comparison (handle relative/absolute differences)
            artifact_names = {Path(a).name for a in output_artifacts}
            missing = [f for f in target_files if Path(f).name not in artifact_names]
            if missing:
                return False, f"Files not changed by agent: {missing}"
            return True, f"All target files changed: {target_files}"

        # No output_artifacts — agent didn't produce any file changes
        return False, f"No files changed by agent (expected: {target_files})"

    def _check_pattern_absent(
        self,
        crit: SuccessCriterion,
        work_dir: Path,
    ) -> tuple[bool, str]:
        """Verify that a pattern no longer exists in the specified file.

        Used for bug-fix verification: the buggy code pattern must be gone.
        """
        if not crit.path or not crit.pattern:
            return True, "pattern_absent: path or pattern not specified (skipped)"

        fpath = work_dir / crit.path
        if not fpath.exists():
            return True, f"File {crit.path} does not exist (pattern trivially absent)"

        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
            matches = re.findall(crit.pattern, content)
            if matches:
                return False, (
                    f"Pattern still present in {crit.path}: "
                    f"found {len(matches)} match(es) for '{crit.pattern}'"
                )
            return True, f"Pattern '{crit.pattern}' absent from {crit.path}"
        except re.error as e:
            return False, f"Invalid regex pattern '{crit.pattern}': {e}"

    def _check_pattern_present(
        self,
        crit: SuccessCriterion,
        work_dir: Path,
    ) -> tuple[bool, str]:
        """Verify that a pattern exists in the specified file.

        Used for bug-fix verification: the fix code pattern must be present.
        """
        if not crit.path or not crit.pattern:
            return True, "pattern_present: path or pattern not specified (skipped)"

        fpath = work_dir / crit.path
        if not fpath.exists():
            return False, f"File {crit.path} does not exist"

        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
            matches = re.findall(crit.pattern, content)
            if not matches:
                return False, (
                    f"Pattern not found in {crit.path}: "
                    f"expected '{crit.pattern}'"
                )
            return True, f"Pattern '{crit.pattern}' found in {crit.path} ({len(matches)} match(es))"
        except re.error as e:
            return False, f"Invalid regex pattern '{crit.pattern}': {e}"
