"""
File existence checker: FILE_EXISTS, FILE_PATTERN, TEST_FILE_EXISTS.

Extracted from EvaluatorEngine as part of #178 PR 2.
"""
from __future__ import annotations

import logging
from pathlib import Path

from core.models import CriterionType, SuccessCriterion
from evaluator.models import CheckResult, EvaluationContext

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
        return CheckResult(passed=False, message=f"Unhandled criterion type: {ct}")

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
            # Generator with FILE_EXISTS criteria but zero output (#372).
            # Distinguish None (untracked → vacuous pass) from [] (confirmed empty → fail).
            if output_artifacts is not None and len(output_artifacts) == 0:
                return CheckResult(
                    passed=False,
                    message=(
                        "FILE_EXISTS criteria but no output files produced. "
                        "The generator did not create any files."
                    ),
                )
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
                try:
                    matches = list(eval_root.glob(f"**/*{stem}*"))
                except NotImplementedError:
                    matches = []
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
                try:
                    matches = list(base.glob(candidate))
                except NotImplementedError:
                    # Python 3.12+ rejects non-relative patterns (#591)
                    matches = []
                if matches:
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
        output_artifacts = context.artifacts
        pattern = crit.pattern or crit.path
        if not pattern:
            return CheckResult(
                passed=True,
                message="file_pattern: no pattern specified (skipped)",
            )

        try:
            matches = set(work_dir.glob(pattern))
        except NotImplementedError:
            # Python 3.12+ rejects non-relative patterns (#591)
            matches = set()
        if "*" in pattern and "**" not in pattern:
            parts = pattern.split("/")
            if len(parts) >= 2:
                recursive_parts = parts[:-1] + ["**"] + parts[-1:]
                try:
                    matches.update(work_dir.glob("/".join(recursive_parts)))
                except NotImplementedError:
                    pass

        files = [m for m in matches if m.is_file()]

        # Cross-reference against output_artifacts to avoid false PASS from
        # pre-existing files (#332). When output_artifacts is available,
        # at least one matched file must be in the node's own output.
        if output_artifacts and files:
            owned = self._filter_owned(files, output_artifacts, work_dir)
            if not owned:
                # All matches are pre-existing files from other nodes/harness
                rel_preexisting = []
                for f in files[:5]:
                    try:
                        rel_preexisting.append(str(f.relative_to(work_dir)))
                    except ValueError:
                        rel_preexisting.append(str(f))
                return CheckResult(
                    passed=False,
                    message=(
                        f"Pattern '{pattern}' matched {len(files)} pre-existing "
                        f"file(s) but NONE were created by this node: "
                        f"{rel_preexisting}. Output artifacts: {output_artifacts}"
                    ),
                )
            # Report only owned files
            files = owned

        if not files:
            alt = self._try_stdlib_rename(pattern, work_dir)
            if alt:
                alt_files = [m for m in work_dir.glob(alt) if m.is_file()]
                if alt_files:
                    # Also check ownership for renamed pattern
                    if output_artifacts:
                        owned = self._filter_owned(alt_files, output_artifacts, work_dir)
                        if not owned:
                            return CheckResult(
                                passed=False,
                                message=(
                                    f"Renamed pattern '{alt}' matched {len(alt_files)} "
                                    f"pre-existing file(s) but NONE were created by this node. "
                                    f"Output artifacts: {output_artifacts}"
                                ),
                            )
                        alt_files = owned
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

    @staticmethod
    def _filter_owned(
        files: list[Path],
        output_artifacts: list[str],
        work_dir: Path,
    ) -> list[Path]:
        """Filter matched files to only those in output_artifacts (#332).

        Compares relative paths so that absolute artifacts and glob results
        can be matched correctly.
        """
        # Normalize output_artifacts to relative paths using forward slashes
        # for cross-platform comparison (Windows uses backslash, artifacts use forward slash)
        owned_rels: set[str] = set()
        for art in output_artifacts:
            p = Path(art)
            if p.is_absolute():
                try:
                    owned_rels.add(str(p.relative_to(work_dir)).replace("\\", "/"))
                except ValueError:
                    owned_rels.add(art.replace("\\", "/"))
            else:
                owned_rels.add(art.replace("\\", "/"))

        owned = []
        for f in files:
            try:
                rel = str(f.relative_to(work_dir)).replace("\\", "/")
            except ValueError:
                rel = str(f).replace("\\", "/")
            if rel in owned_rels or any(rel.endswith("/" + a) for a in owned_rels):
                owned.append(f)
        return owned

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
