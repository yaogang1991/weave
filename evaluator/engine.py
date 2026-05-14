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
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.models import (
    CriterionType,
    EvalStatus,
    EvaluationResult,
    EventType,
    SuccessCriterion,
)
from session.store import SessionStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LintIssue:
    """A single lint issue parsed from flake8 output."""

    path: str  # relative to work_dir
    line: int
    col: int
    code: str  # e.g. "E501", "E402"
    message: str


def parse_flake8_output(output: str) -> list[LintIssue]:
    """Parse flake8 stdout into structured LintIssue list."""
    issues: list[LintIssue] = []
    for line in output.splitlines():
        m = re.match(
            r"^(.+?):(\d+):(\d+):\s+([A-Z]\d+)\s+(.+)$", line,
        )
        if m:
            issues.append(LintIssue(
                path=m.group(1),
                line=int(m.group(2)),
                col=int(m.group(3)),
                code=m.group(4),
                message=m.group(5),
            ))
    return issues


def get_changed_lines(
    file_paths: list[str],
    work_dir: Path,
) -> dict[str, set[int]]:
    """Return {relative_path: set_of_changed_line_numbers} via git diff.

    Uses ``git diff --unified=0`` against HEAD (or index for uncommitted).
    Returns empty dict if git is not available or the directory is not a repo.
    """
    result: dict[str, set[int]] = {}
    for fp in file_paths:
        abs_path = work_dir / fp
        try:
            p = abs_path if abs_path.exists() else Path(fp)
            diff_out = subprocess.run(
                ["git", "diff", "--unified=0", "--", str(p)],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=10,
                cwd=str(work_dir),
            )
            if diff_out.returncode != 0:
                continue
            lines: set[int] = set()
            for hunk in re.finditer(
                r"@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@",
                diff_out.stdout,
            ):
                start = int(hunk.group(1))
                count = int(hunk.group(2) or "1")
                for n in range(start, start + count):
                    lines.add(n)
            if lines:
                try:
                    rel = str(abs_path.relative_to(work_dir))
                except ValueError:
                    rel = fp
                result[rel] = lines
        except (FileNotFoundError, OSError):
            continue
    return result


class EvaluatorEngine:
    """
    Evaluates code against predefined success criteria.

    Supports: test execution, lint checks, coverage, file existence,
    no-critical-issues check. Accepts list[str] (legacy) and
    list[SuccessCriterion] (structured).
    """

    # Hard criteria that can never be downgraded from FAIL to WARN,
    # even when the overall score meets pass_threshold.  These represent
    # fundamental correctness checks that must always pass (#194 review).
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
        # When set, score >= threshold means overall pass even if some
        # criteria fail (reported as WARNING instead of FAIL).  None = strict
        # mode (all criteria must pass, same as before #194).
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
        eval_id = f"{session_id}_{stage_name}"

        self.session_store.emit_event(
            session_id,
            EventType.EVAL_START,
            {"stage": stage_name, "criteria": [str(c) for c in criteria], "artifact": artifact_path},
        )

        structured = self._normalize_criteria(criteria)

        results: dict[str, bool] = {}
        hard_labels: set[str] = set()  # labels for hard (non-overrideable) criteria
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

        # Threshold-based passing (#194):
        # When pass_threshold is set, score >= threshold allows overall pass
        # even if some auto-checked criteria fail.  Failed criteria are
        # reported as WARNING instead of FAIL.
        # Hard criteria (FILE_EXISTS, TESTS_PASS, PATTERN_PRESENT) can never
        # be overridden — if any hard criterion fails, overall result is FAIL.
        failed_auto = [
            label for label, ok in results.items() if not ok
        ]
        failed_hard = [label for label in failed_auto if label in hard_labels]
        threshold_pass = False  # Track whether pass is via threshold override
        if self.pass_threshold is not None and not all_auto_passed:
            # Hard criteria veto threshold override
            if failed_hard:
                overall_passed = False
            elif score >= self.pass_threshold:
                # Downgrade SOFT failed criteria from FAIL to WARN in feedback.
                # Hard criteria failures are left as FAIL (should not happen here
                # since failed_hard check above would have caught them).
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

        # Mandatory artifact verification (#234): verify output_artifacts
        # actually exist on disk. Prevents false positives when a generator
        # reports creating files that were never actually written.
        if output_artifacts and eval_dir:
            eval_root = Path(eval_dir)
            phantom = []
            for art in output_artifacts:
                p = Path(art)
                full = p if p.is_absolute() else eval_root / p
                if not full.is_file():
                    phantom.append(art)
            if phantom:
                overall_passed = False
                score = 0.0
                feedback_parts.append(
                    f"FAIL artifact_verification: {len(phantom)} reported "
                    f"artifact(s) not found on disk: {phantom}"
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
        if "test_file_exist" in lower or "test file exist" in lower:
            return SuccessCriterion(type=CriterionType.TEST_FILE_EXISTS, description=criterion)
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
        eval_id: str = "",
    ) -> tuple[bool, str, bool]:
        if crit.type == CriterionType.TESTS_PASS:
            test_targets = None
            if crit.test_path:
                test_targets = crit.test_path
            elif output_artifacts:
                test_targets = self._find_test_files(output_artifacts, Path(work_dir))
            if not test_targets:
                return True, (
                    "No test files found to run — tests not verified. "
                    "Consider adding test files or adjusting criteria."
                ), False
            passed, msg = self._run_tests(Path(work_dir), test_targets, eval_id)
            return passed, msg, True

        if crit.type == CriterionType.LINT:
            if not output_artifacts:
                return True, "No files to lint (passed by default)", True
            passed, msg = self._run_lint(output_artifacts, Path(work_dir))
            # When no linter is available, treat as uncheckable (WARN)
            # rather than hard FAIL — missing tool != bad code (#200).
            if not passed and "No linter available" in msg:
                return True, (
                    f"Lint skipped: {msg}. "
                    f"Install flake8 or ruff for lint checking."
                ), False
            return passed, msg, True

        if crit.type == CriterionType.FILE_EXISTS:
            # Collect candidate paths: planner-specified + agent-reported.
            candidates: list[str] = []
            if crit.path:
                candidates.extend(f.strip() for f in crit.path.split(","))
            if output_artifacts:
                candidates.extend(output_artifacts)

            if not candidates:
                return True, "No specific files listed", True

            # Verify each candidate ON DISK (#158).
            eval_root = Path(work_dir)
            verified, missing = [], []
            for cand in candidates:
                p = Path(cand)
                full = p if p.is_absolute() else eval_root / p
                if full.is_file():
                    verified.append(str(full))
                    continue
                # Fallback: loose glob match by stem.
                stem = Path(cand).stem
                if stem and len(stem) >= 3:
                    matches = list(eval_root.glob(f"**/*{stem}*"))
                    matches = [
                        m for m in matches
                        if m.is_file()
                    ]
                    if matches:
                        verified.append(str(matches[0]))
                        continue
                missing.append(cand)

            if missing:
                # Fallback: try alternative test file paths (#218).
                # Different models place tests in different conventions:
                #   module/test_x.py  vs  tests/test_module_x.py
                still_missing = []
                for m in missing:
                    alt = self._find_test_file_alternative(m, eval_root)
                    if alt:
                        verified.append(alt)
                        logger.info(
                            "FILE_EXISTS fallback: %s → %s", m, alt,
                        )
                    else:
                        # Fallback: try stdlib-prefixed rename (#285)
                        renamed = self._try_stdlib_rename(m, eval_root)
                        if renamed:
                            renamed_files = [
                                f for f in eval_root.glob(renamed) if f.is_file()
                            ]
                            if renamed_files:
                                verified.append(str(renamed_files[0]))
                                logger.info(
                                    "FILE_EXISTS stdlib rename fallback: "
                                    "%s → %s", m, renamed,
                                )
                                continue
                        still_missing.append(m)
                missing = still_missing

            if missing:
                # Actionable feedback: show expected vs actual files (#160)
                msg = (
                    f"Required file(s) missing: {missing}. "
                    f"Found on disk: {verified or 'none'}. "
                    f"To pass, create the required file(s) at the exact path(s), "
                    f"or adjust the plan criterion to file_pattern if "
                    f"alternative filenames are acceptable."
                )
                return False, msg, True
            return (
                True,
                f"All {len(verified)} file(s) verified on disk",
                True,
            )

        if crit.type == CriterionType.FILE_PATTERN:
            return self._check_file_pattern(crit, Path(work_dir))

        if crit.type == CriterionType.COVERAGE:
            target = int(crit.target) if crit.target else 80
            return self._check_coverage(
                Path(work_dir), target, output_artifacts, eval_id,
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

        if crit.type == CriterionType.TEST_FILE_EXISTS:
            passed, msg = self._check_test_file_exists(output_artifacts)
            return passed, msg, True

        # CUSTOM + any unknown type → pass with warning (manual review recommended)
        return True, (
            f"Cannot auto-verify: {crit.description}. "
            f"Assumed passed — manual review recommended."
        ), False

    # ------------------------------------------------------------------
    # Internal checkers — all return 2-tuple (bool, str)
    # ------------------------------------------------------------------
    @staticmethod
    def _safe_eval_id(eval_id: str) -> str:
        """Sanitize eval_id for use as a filename component."""
        import re as _re
        return _re.sub(r"[^a-zA-Z0-9_\-]", "_", eval_id)

    def _isolated_env(self, eval_id: str = "", work_dir: Path | None = None) -> dict[str, str]:
        """Build env with a unique COVERAGE_FILE to prevent parallel node contention (#260).

        Uses an absolute path so that coverage data is written to the work
        directory regardless of the subprocess cwd.
        """
        import os
        env = os.environ.copy()
        if eval_id:
            safe_id = self._safe_eval_id(eval_id)
            if work_dir:
                env["COVERAGE_FILE"] = str(work_dir / f".coverage.{safe_id}")
            else:
                env["COVERAGE_FILE"] = f".coverage.{safe_id}"
        return env

    def _find_test_files(
        self,
        output_artifacts: list[str],
        work_dir: Path,
    ) -> list[str]:
        """Find test files relevant to the current artifacts.

        1. Test files already in output_artifacts.
        2. Test files matching source artifact names (e.g. parser.py → test_parser.py).
        This prevents pytest from collecting leftover test files from previous runs (#249).
        """
        test_files: list[str] = []

        # Direct test files from artifacts
        for a in output_artifacts:
            name = Path(a).name.lower()
            if "test" in name and name.endswith(".py"):
                full = work_dir / a if not Path(a).is_absolute() else Path(a)
                if full.exists():
                    test_files.append(a)

        # Infer test files from source artifact stems
        source_stems: list[str] = []
        for a in output_artifacts:
            name = Path(a).name
            lower = name.lower()
            if lower.endswith(".py") and "test" not in lower:
                source_stems.append(Path(a).stem)

        if source_stems:
            # Search for test files in common locations
            search_dirs = [work_dir, work_dir / "tests"]
            for search_dir in search_dirs:
                if not search_dir.is_dir():
                    continue
                for tf in search_dir.glob("test_*.py"):
                    stem = tf.stem  # e.g. "test_parser"
                    module_name = stem[5:]  # strip "test_"
                    if module_name in source_stems:
                        rel = str(tf.relative_to(work_dir)) if tf.is_relative_to(work_dir) else str(tf)
                        if rel not in test_files:
                            test_files.append(rel)

        return test_files

    def _run_tests(self, work_dir: Path, test_path: str | list[str] | None = None, eval_id: str = "") -> tuple[bool, str]:
        """Run pytest with a fixed command. Never executes arbitrary commands."""
        try:
            cmd = [sys.executable, "-m", "pytest", "-v", "--tb=short"]
            if test_path:
                if isinstance(test_path, list):
                    cmd.extend(str(work_dir / t) if not Path(t).is_absolute() else t for t in test_path)
                else:
                    cmd.append(test_path)
            result = subprocess.run(
                cmd,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=60,
                cwd=str(work_dir) if work_dir.is_dir() else None,
                env=self._isolated_env(eval_id, work_dir),
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
        except subprocess.TimeoutExpired:
            return False, (
                "Tests timed out after 60s — likely a background thread or process leak. "
                "Ensure all threads use daemon=True and add proper cleanup in test teardown "
                "(e.g. @pytest.fixture with yield + stop() call)."
            )
        except FileNotFoundError:
            return False, "pytest not installed"
        except Exception as e:
            return False, f"Test execution error: {e}"

    def _run_lint(self, targets: list[str], work_dir: Path) -> tuple[bool, str]:
        """Dry-run autofix then delta-lint resolved target files.

        Phase 1 (dry-run): detect auto-fixable issues (F401/F841, E501)
        without modifying files in-place, avoiding cross-node corruption
        when parallel DAG nodes share the same work directory (#159).

        Phase 2 (verify): Run flake8, parse output into LintIssue list,
        then use git diff to determine which lines the agent changed.
        Only issues on changed lines count as failures (#150).

        If git is not available, falls back to all issues → failure
        (same as pre-#150 behavior).
        """
        self._last_autofixed = []
        self._last_auto_formatted = []
        self._last_lint_new_issues = []
        self._last_lint_all_issues = []

        resolved = []
        for t in targets:
            p = work_dir / t
            if p.is_file() and p.suffix == ".py":
                resolved.append(str(p))
            elif p.is_dir():
                for f in p.glob("*.py"):
                    resolved.append(str(f))
            elif Path(t).is_file() and Path(t).suffix == ".py":
                resolved.append(str(Path(t)))
        if not resolved:
            return True, "No targets to lint"

        # Detect auto-fixable issues via dry-run (no in-place modification).
        # This prevents parallel DAG nodes from corrupting each other's files.
        autofix_suggestions: list[str] = []
        # Snapshot content before autoflake to detect changes (for tracking).
        _pre_autoflake: dict[str, str] = {}
        for fpath in resolved:
            try:
                _pre_autoflake[fpath] = Path(fpath).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        try:
            result = subprocess.run(
                [
                    sys.executable, "-m", "autoflake",
                    "--remove-all-unused-imports",
                    "--remove-unused-variables",
                    "--check",
                ] + resolved,
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0 and result.stdout:
                lines = result.stdout.strip().split("\n")[:5]
                autofix_suggestions.extend(
                    f"autoflake: {l}" for l in lines if l.strip()
                )
        except FileNotFoundError:
            pass
        except Exception:
            pass

        # Detect files changed by autoflake (populated when tests simulate
        # in-place modification, or if --check is removed in the future).
        for fpath, content_before in _pre_autoflake.items():
            try:
                content_after = Path(fpath).read_text(encoding="utf-8", errors="replace")
                if content_after != content_before:
                    self._last_autofixed.append(Path(fpath).name)
            except OSError:
                pass

        # Apply autopep8 in-place formatting to fix whitespace issues
        # (E203, E303, W291, W293, W605, E302) before flake8 runs.
        # This eliminates ~80% of retry-causing formatting issues (#206).
        formatted_files = self._auto_format_apply(resolved, work_dir)
        self._last_auto_formatted = formatted_files
        if formatted_files:
            logger.info(
                "autopep8 formatted %d file(s): %s",
                len(formatted_files), formatted_files,
            )

        # Run flake8 (or ruff fallback)
        lint_stdout = ""
        try:
            result = subprocess.run(
                [sys.executable, "-m", "flake8"] + resolved
                + ["--max-line-length=100"],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=60,
            )
            lint_stdout = result.stdout
        except FileNotFoundError:
            try:
                result = subprocess.run(
                    ["ruff", "check"] + resolved,
                    capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=60,
                )
                lint_stdout = result.stdout
            except FileNotFoundError:
                return False, "No linter available (install flake8 or ruff)"
        except Exception as e:
            return False, f"Lint error: {e}"

        if result.returncode == 0:
            msg = "Lint clean"
            if self._last_autofixed:
                msg = (
                    f"Autoflake auto-fixed: {', '.join(self._last_autofixed)}"
                    f"\n{msg}"
                )
            if autofix_suggestions:
                msg = (
                    f"Autofix suggestions: {'; '.join(autofix_suggestions[:3])}"
                    f"\n{msg}"
                )
            return True, msg

        # Parse all lint issues
        all_issues = parse_flake8_output(lint_stdout)
        if not all_issues:
            # Could not parse (unexpected format) — treat as before
            msg = f"Lint issues:\n{lint_stdout[:500]}"
            if self._last_autofixed:
                msg = (
                    f"Autoflake auto-fixed: {', '.join(self._last_autofixed)}"
                    f"\n{msg}"
                )
            if autofix_suggestions:
                msg = (
                    f"Autofix suggestions: {'; '.join(autofix_suggestions[:3])}"
                    f"\n{msg}"
                )
            self._last_lint_all_issues = []
            self._last_lint_new_issues = []
            return False, msg

        # Delta lint: use git diff to find changed lines (#150)
        rel_targets = []
        for r in resolved:
            try:
                rel_targets.append(str(Path(r).relative_to(work_dir)))
            except ValueError:
                rel_targets.append(Path(r).name)

        changed = get_changed_lines(rel_targets, work_dir)

        # Store issues for regression tracking (#151)
        self._last_lint_all_issues = [
            f"{i.path}:{i.line}:{i.code}" for i in all_issues
        ]

        if changed:
            # Only issues on changed lines are "new"
            new_issues: list[LintIssue] = []
            existing_issues: list[LintIssue] = []
            for issue in all_issues:
                issue_path = issue.path
                # Normalize to relative for comparison
                try:
                    issue_path = str(
                        Path(issue.path).relative_to(work_dir),
                    )
                except ValueError:
                    pass
                changed_lines = changed.get(issue_path, set())
                if issue.line in changed_lines:
                    new_issues.append(issue)
                else:
                    existing_issues.append(issue)

            self._last_lint_new_issues = [
                f"{i.path}:{i.line}:{i.code}" for i in new_issues
            ]

            if not new_issues:
                msg = "Lint clean (all issues are pre-existing)"
            else:
                new_lines = [
                    f"  - {i.path}:{i.line} {i.code} {i.message}"
                    for i in new_issues
                ]
                msg = (
                    f"Lint failed: {len(new_issues)} new issue(s)"
                )
                if existing_issues:
                    msg += (
                        f", {len(existing_issues)} existing ignored"
                    )
                msg += "\nNEW:\n" + "\n".join(new_lines)
                if existing_issues:
                    existing_lines = [
                        f"  - {i.path}:{i.line} {i.code} {i.message}"
                        for i in existing_issues[:10]
                    ]
                    msg += (
                        "\nIGNORED_EXISTING:\n"
                        + "\n".join(existing_lines)
                    )
                    if len(existing_issues) > 10:
                        msg += (
                            f"\n  ... and {len(existing_issues) - 10} more"
                        )
        else:
            # No git diff available — all issues are potential failures
            new_issues = all_issues
            lines = [
                f"  - {i.path}:{i.line} {i.code} {i.message}"
                for i in all_issues
            ]
            msg = "Lint issues (delta unavailable):\n" + "\n".join(lines)
            self._last_lint_new_issues = self._last_lint_all_issues

        if autofix_suggestions:
            msg = (
                f"Autofix suggestions: {'; '.join(autofix_suggestions[:3])}"
                f"\n{msg}"
            )

        if self._last_autofixed:
            msg = (
                f"Autoflake auto-fixed: {', '.join(self._last_autofixed)}"
                f"\n{msg}"
            )

        return len(new_issues) == 0, msg

    def _auto_format_apply(self, resolved: list[str], work_dir: Path) -> list[str]:
        """Apply autopep8 in-place formatting to fix whitespace issues (#206).

        Runs before flake8 so that auto-fixable formatting errors (E203, E303,
        W291, W293, W605, E302) are eliminated, preventing unnecessary
        retries.  Only targets files in the resolved list (current node's
        files), making it safe for parallel DAG execution.

        Returns list of relative paths of files that were actually modified.
        Silently skips if autopep8 is not installed or times out.
        Disabled by default; requires auto_format_before_eval=True.
        """
        if not self.auto_format_before_eval or not resolved:
            return []

        before: dict[str, str] = {}
        for fpath in resolved:
            try:
                before[fpath] = Path(fpath).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass

        try:
            result = subprocess.run(
                [
                    sys.executable, "-m", "autopep8",
                    "--in-place",
                    "--select=E203,E303,W291,W293,W605,E302",
                ] + resolved,
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                logger.debug(
                    "autopep8 skipped (rc=%d): %s",
                    result.returncode, result.stderr[-500:],
                )
                return []
        except FileNotFoundError:
            logger.debug("autopep8 not installed, skipping auto-format")
            return []
        except subprocess.TimeoutExpired:
            logger.warning("autopep8 timed out, skipping auto-format")
            return []
        except Exception as exc:
            logger.warning("autopep8 error: %s", exc)
            return []

        changed: list[str] = []
        for fpath, content_before in before.items():
            try:
                content_after = Path(fpath).read_text(encoding="utf-8", errors="replace")
                if content_after != content_before:
                    try:
                        rel = str(Path(fpath).relative_to(work_dir))
                    except ValueError:
                        rel = Path(fpath).name
                    changed.append(rel)
            except OSError:
                pass
        return changed

    def _check_files_exist(self, files: list[str], base: Path) -> tuple[bool, str]:
        missing = [f for f in files if not (base / f).exists()]
        passed = len(missing) == 0
        return passed, f"Missing: {missing}" if missing else "All required files present"

    @staticmethod
    def _find_test_file_alternative(expected: str, base: Path) -> str | None:
        """Search common alternative locations for test files (#218).

        Handles the convention mismatch where planner expects
        ``module/test_x.py`` but agent creates ``tests/test_module_x.py``.
        Only matches test files (containing ``test_`` in path).
        """
        if "test_" not in expected:
            return None  # Only apply to test files

        path = Path(expected)
        stem = path.stem       # e.g. test_hasher
        parent = path.parent   # e.g. fileutils
        parent_name = parent.name if parent.name else ""

        # Build alternative patterns for test files
        alternatives: list[str] = []
        if parent_name:
            # module/test_x.py → tests/test_module_x.py
            alternatives.append(f"tests/test_{parent_name}_{stem.replace('test_', '', 1)}.py")
            # module/test_x.py → tests/test_x.py
            alternatives.append(f"tests/{stem}.py")
            # module/test_x.py → test/test_module_x.py
            alternatives.append(f"test/test_{parent_name}_{stem.replace('test_', '', 1)}.py")
        else:
            # test_x.py → tests/test_x.py
            alternatives.append(f"tests/{stem}.py")
            alternatives.append(f"test/{stem}.py")

        for alt in alternatives:
            alt_path = base / alt
            if alt_path.is_file():
                return str(alt_path)

        return None

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
        eval_id: str = "",
    ) -> tuple[bool, str, bool]:
        """Check test coverage against target percentage.

        Returns (passed, message, was_auto_verified).
        When coverage output cannot be parsed, returns was_auto_verified=False
        so the caller emits WARN instead of PASS (#152).
        """
        try:
            cmd = [
                sys.executable, "-m", "pytest", "-v",
                "--tb=short", "--cov-report=term-missing",
            ]

            # Scope test collection to relevant test files only (#249).
            # Without this, pytest collects leftover test files from previous
            # runs that import modules no longer in the workspace.
            test_targets: list[str] | None = None
            if output_artifacts:
                test_targets = self._find_test_files(output_artifacts, work_dir)

            if not test_targets and output_artifacts:
                return (
                    False,
                    "No test files found for coverage check — "
                    "cannot verify coverage without scoped tests.",
                    False,
                )

            if test_targets:
                for t in test_targets:
                    p = Path(t)
                    cmd.append(str(work_dir / p) if not p.is_absolute() else str(p))

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
                    # No package-level targets found; scope to work_dir
                    cmd.append(f"--cov={work_dir}")
            else:
                # output_artifacts empty: run tests without coverage to avoid
                # scanning historical files that may have import errors (#165).
                cmd = [sys.executable, "-m", "pytest", "-v", "--tb=short"]

            result = subprocess.run(
                cmd,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=60,
                cwd=str(work_dir) if work_dir.is_dir() else None,
                env=self._isolated_env(eval_id, work_dir),
            )

            # Parse TOTAL line via regex — handles both compact and wide formats:
            #   TOTAL  123  4  97%
            #   TOTAL  123  4  5  97.5%
            for line in result.stdout.split("\n"):
                stripped = line.strip()
                if stripped.startswith("TOTAL"):
                    m = re.search(r"(\d+(?:\.\d+)?)%", stripped)
                    if m:
                        cov = float(m.group(1))
                        return (
                            cov >= target,
                            f"Coverage: {cov}% (target: {target}%)",
                            True,
                        )

            # Could not parse TOTAL line — coverage target is unverifiable.
            # Return was_auto_verified=False so evaluate_stage emits WARN
            # instead of PASS/FAIL (see #152).
            stdout_tail = result.stdout[-500:] if result.stdout else ""
            stderr_tail = result.stderr[-500:] if result.stderr else ""
            if result.returncode == 0:
                if not output_artifacts:
                    return True, (
                        f"Coverage could not be verified: no output_artifacts "
                        f"to scope coverage. Tests passed but coverage target "
                        f"{target}% was not verified."
                    ), False
                return True, (
                    f"Coverage could not be parsed; tests passed but coverage "
                    f"target {target}% was not verified. "
                    f"stdout_tail=...{stdout_tail} "
                    f"stderr_tail=...{stderr_tail}"
                ), False
            return False, (
                f"Tests failed and coverage report could not be parsed. "
                f"stdout_tail=...{stdout_tail} "
                f"stderr_tail=...{stderr_tail}"
            ), True
        except subprocess.TimeoutExpired:
            return False, (
                "Coverage check timed out after 60s — likely a background thread leak. "
                "Use daemon threads and proper test teardown."
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

    def _check_test_file_exists(
        self,
        output_artifacts: list[str] | None = None,
    ) -> tuple[bool, str]:
        """Verify that test files were produced in output_artifacts (#247).

        Unlike FILE_EXISTS (which checks specific paths on disk), this
        criterion checks that the agent's output_artifacts contain at least
        one file matching common test naming conventions (test_*.py,
        *_test.py, *_spec.py, or .py files under tests/ or test/ dirs).
        """
        if not output_artifacts:
            return False, (
                "No output artifacts produced — test files required. "
                "Create test files (e.g., test_*.py) for your implementation."
            )

        def _is_test_file(artifact_path: str) -> bool:
            basename = artifact_path.lower().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            if basename.startswith("test_") or basename.endswith("_test.py"):
                return True
            if basename.endswith("_spec.py"):
                return True
            lower_path = artifact_path.lower()
            if "/tests/" in lower_path or "/test/" in lower_path:
                if basename.endswith(".py"):
                    return True
            return False

        test_files = [a for a in output_artifacts if _is_test_file(a)]
        if test_files:
            return True, f"Test files found: {test_files}"
        return False, (
            f"No test files in output artifacts: {output_artifacts}. "
            f"You MUST create test files (e.g., test_*.py) for your "
            f"implementation."
        )

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

    def _check_file_pattern(
        self,
        crit: SuccessCriterion,
        work_dir: Path,
    ) -> tuple[bool, str, bool]:
        """Verify that at least one file matching a glob pattern exists on disk.

        Unlike file_exists (exact path), file_pattern accepts any matching
        non-empty file. Used when the exact filename is a generator choice (#160).
        Automatically falls back to recursive glob (``**/``) when the initial
        pattern only matches one level deep (#253).
        """
        pattern = crit.pattern or crit.path
        if not pattern:
            return True, "file_pattern: no pattern specified (skipped)", True

        matches = set(work_dir.glob(pattern))
        # Merge recursive results for single-level globs (#253).
        # e.g. "validlib/*.py" also finds "validlib/core/validator.py"
        if "*" in pattern and "**" not in pattern:
            parts = pattern.split("/")
            if len(parts) >= 2:
                recursive_parts = parts[:-1] + ["**"] + parts[-1:]
                recursive_pattern = "/".join(recursive_parts)
                matches.update(work_dir.glob(recursive_pattern))

        files = [
            m for m in matches
            if m.is_file()
        ]

        if not files:
            # Fallback: try stdlib-prefixed alternatives (#285)
            alt = self._try_stdlib_rename(pattern, work_dir)
            if alt:
                alt_files = [m for m in work_dir.glob(alt) if m.is_file()]
                if alt_files:
                    rel = [str(f.relative_to(work_dir)) for f in alt_files[:10]
                           if f.is_file()]
                    return True, (
                        f"Matched {len(alt_files)} file(s) for renamed "
                        f"pattern '{alt}' (original: '{pattern}'): {rel}"
                    ), True
            return False, (
                f"No files matched pattern: {pattern} "
                f"(searched in {work_dir})"
            ), True

        rel_names = []
        for f in files[:10]:
            try:
                rel_names.append(str(f.relative_to(work_dir)))
            except ValueError:
                rel_names.append(str(f))

        return True, (
            f"Matched {len(files)} file(s) for pattern '{pattern}': "
            f"{rel_names}"
        ), True

    @staticmethod
    def _try_stdlib_rename(path: str, base: Path) -> str | None:
        """Fallback for #285: try common stdlib-prefix alternatives.

        When a path like ``exam_app/models/*.py`` doesn't match, try
        ``exam_app/app_models/*.py`` and similar prefixes so that evaluator
        criteria written before PlanValidator renaming still resolve.
        """
        for pfx in ("app_", "my_"):
            # Try replacing each path segment with the prefixed variant.
            parts = Path(path).parts
            for i, part in enumerate(parts):
                candidate = str(Path(*parts[:i], pfx + part, *parts[i + 1:]))
                if list(base.glob(candidate)):
                    return candidate
        return None

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
