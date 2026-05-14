"""
File existence checker: FILE_EXISTS, FILE_PATTERN, TEST_FILE_EXISTS.

Extracted from EvaluatorEngine as part of #178 PR 2.
"""
from __future__ import annotations

import logging
from pathlib import Path

from core.models import CriterionType, SuccessCriterion
from evaluator.models import CheckResult, CheckSeverity, EvaluationContext

logger = logging.getLogger(__name__)


class FileExistsChecker:
    """Handles FILE_EXISTS, FILE_PATTERN, and TEST_FILE_EXISTS criteria."""

    def check(
        self,
        criterion: SuccessCriterion,
        context: EvaluationContext,
    ) -> CheckResult:
        ct = criterion.type
        if ct == CriterionType.FILE_EXISTS:
            return self._check_file_exists(criterion, context)
        if ct == CriterionType.FILE_PATTERN:
            return self._check_file_pattern(criterion, context)
        if ct == CriterionType.TEST_FILE_EXISTS:
            return self._check_test_file_exists(criterion, context)
        return CheckResult(passed=True, message="Unhandled criterion type")

    # ------------------------------------------------------------------
    # FILE_EXISTS
    # ------------------------------------------------------------------

    def _check_file_exists(
        self,
        crit: SuccessCriterion,
        context: EvaluationContext,
    ) -> CheckResult:
        work_dir = context.work_dir
        output_artifacts = context.artifacts

        candidates: list[str] = []
        if crit.path:
            candidates.extend(f.strip() for f in crit.path.split(","))
        if output_artifacts:
            candidates.extend(output_artifacts)

        if not candidates:
            return CheckResult(passed=True, message="No specific files listed")

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
                matches = [m for m in matches if m.is_file()]
                if matches:
                    verified.append(str(matches[0]))
                    continue
            missing.append(cand)

        if missing:
            still_missing, newly_verified = self._resolve_missing(missing, eval_root)
            verified.extend(newly_verified)
            missing = still_missing

        if missing:
            return CheckResult(
                passed=False,
                message=(
                    f"Required file(s) missing: {missing}. "
                    f"Found on disk: {verified or 'none'}. "
                    f"To pass, create the required file(s) at the exact path(s), "
                    f"or adjust the plan criterion to file_pattern if "
                    f"alternative filenames are acceptable."
                ),
            )
        return CheckResult(
            passed=True,
            message=f"All {len(verified)} file(s) verified on disk",
        )

    def _resolve_missing(
        self, missing: list[str], eval_root: Path,
    ) -> tuple[list[str], list[str]]:
        """Try fallbacks for missing files: test path alternatives, stdlib rename.

        Returns (still_missing, newly_verified) — caller must merge newly_verified
        into its own verified list to get an accurate count (#305).
        """
        still_missing: list[str] = []
        newly_verified: list[str] = []
        for m in missing:
            alt = self._find_test_file_alternative(m, eval_root)
            if alt:
                logger.info("FILE_EXISTS fallback: %s → %s", m, alt)
                newly_verified.append(alt)
                continue
            renamed = self._try_stdlib_rename(m, eval_root)
            if renamed:
                renamed_files = [
                    f for f in eval_root.glob(renamed) if f.is_file()
                ]
                if renamed_files:
                    logger.info(
                        "FILE_EXISTS stdlib rename fallback: %s → %s",
                        m, renamed,
                    )
                    newly_verified.append(str(renamed_files[0]))
                    continue
            still_missing.append(m)
        return still_missing, newly_verified

    @staticmethod
    def _find_test_file_alternative(expected: str, base: Path) -> str | None:
        """Search common alternative locations for test files (#218)."""
        if "test_" not in expected:
            return None
        path = Path(expected)
        stem = path.stem
        parent = path.parent
        parent_name = parent.name if parent.name else ""

        alternatives: list[str] = []
        if parent_name:
            alternatives.append(f"tests/test_{parent_name}_{stem.replace('test_', '', 1)}.py")
            alternatives.append(f"tests/{stem}.py")
            alternatives.append(f"test/test_{parent_name}_{stem.replace('test_', '', 1)}.py")
        else:
            alternatives.append(f"tests/{stem}.py")
            alternatives.append(f"test/{stem}.py")

        for alt in alternatives:
            alt_path = base / alt
            if alt_path.is_file():
                return str(alt_path)
        return None

    @staticmethod
    def _try_stdlib_rename(path: str, base: Path) -> str | None:
        """Fallback for #285: try common stdlib-prefix alternatives."""
        for pfx in ("app_", "my_"):
            parts = Path(path).parts
            for i, part in enumerate(parts):
                candidate = str(Path(*parts[:i], pfx + part, *parts[i + 1:]))
                if list(base.glob(candidate)):
                    return candidate
        return None

    # ------------------------------------------------------------------
    # FILE_PATTERN
    # ------------------------------------------------------------------

    def _check_file_pattern(
        self,
        crit: SuccessCriterion,
        context: EvaluationContext,
    ) -> CheckResult:
        work_dir = context.work_dir
        pattern = crit.pattern or crit.path
        if not pattern:
            return CheckResult(
                passed=True,
                message="file_pattern: no pattern specified (skipped)",
            )

        matches = set(work_dir.glob(pattern))
        if "*" in pattern and "**" not in pattern:
            parts = pattern.split("/")
            if len(parts) >= 2:
                recursive_parts = parts[:-1] + ["**"] + parts[-1:]
                matches.update(work_dir.glob("/".join(recursive_parts)))

        files = [m for m in matches if m.is_file()]

        if not files:
            alt = self._try_stdlib_rename(pattern, work_dir)
            if alt:
                alt_files = [m for m in work_dir.glob(alt) if m.is_file()]
                if alt_files:
                    rel = [
                        str(f.relative_to(work_dir))
                        for f in alt_files[:10] if f.is_file()
                    ]
                    return CheckResult(
                        passed=True,
                        message=(
                            f"Matched {len(alt_files)} file(s) for renamed "
                            f"pattern '{alt}' (original: '{pattern}'): {rel}"
                        ),
                    )
            return CheckResult(
                passed=False,
                message=f"No files matched pattern: {pattern} (searched in {work_dir})",
            )

        rel_names = []
        for f in files[:10]:
            try:
                rel_names.append(str(f.relative_to(work_dir)))
            except ValueError:
                rel_names.append(str(f))

        return CheckResult(
            passed=True,
            message=(
                f"Matched {len(files)} file(s) for pattern '{pattern}': "
                f"{rel_names}"
            ),
        )

    # ------------------------------------------------------------------
    # TEST_FILE_EXISTS
    # ------------------------------------------------------------------

    def _check_test_file_exists(
        self,
        crit: SuccessCriterion,
        context: EvaluationContext,
    ) -> CheckResult:
        output_artifacts = context.artifacts
        if not output_artifacts:
            return CheckResult(
                passed=False,
                message=(
                    "No output artifacts produced — test files required. "
                    "Create test files (e.g., test_*.py) for your implementation."
                ),
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
            return CheckResult(passed=True, message=f"Test files found: {test_files}")
        return CheckResult(
            passed=False,
            message=(
                f"No test files in output artifacts: {output_artifacts}. "
                f"You MUST create test files (e.g., test_*.py) for your "
                f"implementation."
            ),
        )
