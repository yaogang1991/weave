"""
RetryPolicyEngine — retry regression detection and best-attempt tracking.

Extracted from DAGExecutionEngine as part of #177 PR4.
Behavior-preserving extraction: all logic is identical, just relocated
for testability and separation of concerns.
"""
from __future__ import annotations

import logging
import os
import re

from typing import Any

from core.models import CriterionType, DAGNode, SuccessCriterion

logger = logging.getLogger(__name__)


class RetryPolicyEngine:
    """Tracks best attempt across retries and detects regressions.

    Responsibilities:
    - Best-attempt tracking (score, artifacts, feedback, lint issues)
    - Score-based regression detection
    - Lint issue regression detection with tolerable codes
    - File snapshot capture/restore for rollback
    - Zero-output artifact requirement check
    - Exponential backoff computation
    """

    # Codes that are purely formatting/whitespace and safe to tolerate on retry.
    # E999 is intentionally excluded — it indicates SyntaxError, not formatting.
    RETRY_TOLERABLE_CODES: frozenset[str] = frozenset({
        # Whitespace / formatting
        "E501",  # line too long
        "E303",  # too many blank lines
        "W291",  # trailing whitespace
        "W293",  # whitespace before ':'
        "E203",  # whitespace before ':'
        "E302",  # expected 2 blank lines
        "E261",  # at least two spaces before inline comment
        "E265",  # block comment should start with '# '
        # Unused imports/variables — cosmetic, not functional
        "F401",  # module imported but unused
        "F841",  # local variable assigned but never used
    })

    def __init__(self) -> None:
        self._best_attempts: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Best-attempt tracking
    # ------------------------------------------------------------------

    def get_best(self, node_id: str) -> dict[str, Any] | None:
        """Return the best attempt dict for a node, or None."""
        return self._best_attempts.get(node_id)

    def record_attempt(
        self,
        node_id: str,
        score: float,
        feedback: str,
        output_artifacts: list[str],
        work_dir: str,
        eval_metadata: dict[str, Any],
        criteria_results: dict[str, bool] | None = None,
        passed: bool = False,
    ) -> tuple[bool, dict[str, Any] | None]:
        """Record an attempt and detect regression.

        Returns (is_regression, best_attempt_dict).
        If is_regression is True, the caller should restore files from
        best_attempt_dict and reset output_artifacts.
        """
        current_issues = set(
            eval_metadata.get(
                "lint_new_issues",
                eval_metadata.get("lint_all_issues", []),
            )
        )
        prev_best = self._best_attempts.get(node_id)
        is_regression = False

        entry = {
            "score": score,
            "artifacts": list(output_artifacts),
            "feedback": feedback,
            "lint_issues": current_issues,
            "artifact_set": set(output_artifacts or []),
            "file_snapshot": self.capture_file_snapshot(
                work_dir, output_artifacts,
            ),
            "criteria_results": criteria_results or {},
            "passed": passed,
        }

        if prev_best is None:
            # First failure — record as initial best
            self._best_attempts[node_id] = entry
        elif score > prev_best["score"]:
            # Score improved — update best
            self._best_attempts[node_id] = entry
        else:
            # Score not improved — check issue-level regression (#151)
            prev_issues: set[str] = prev_best.get("lint_issues", set())
            new_in_current = current_issues - prev_issues
            fixed_from_prev = prev_issues - current_issues

            # Check if all current issues are lint-only (#154).
            all_issues_lint_only = (
                len(current_issues) > 0
                and all(
                    self.is_tolerable_lint_issue(iss)
                    for iss in current_issues
                )
            )

            if new_in_current and not fixed_from_prev:
                # Only new issues, nothing fixed
                if all_issues_lint_only:
                    # Lint-only — allow retry, update best (#154)
                    is_regression = False
                    self._best_attempts[node_id] = entry
                else:
                    is_regression = True
            elif (
                len(new_in_current) > len(fixed_from_prev)
                and score < prev_best["score"]
            ):
                # More new issues than fixed AND score dropped
                is_regression = True
            else:
                # Partial progress: some issues fixed, some new
                self._best_attempts[node_id] = entry

            logger.warning(
                "Retry score %.1f <= best %.1f "
                "(new_issues=%d, fixed=%d, regression=%s)",
                score, prev_best["score"],
                len(new_in_current), len(fixed_from_prev),
                is_regression,
            )

        return is_regression, self._best_attempts.get(node_id)

    # ------------------------------------------------------------------
    # File snapshot
    # ------------------------------------------------------------------

    @staticmethod
    @staticmethod
    def _safe_path(work_dir: str, art: str) -> str | None:
        """Resolve path and return it only if within work_dir, else None."""
        resolved = os.path.realpath(os.path.join(work_dir, art))
        if not resolved.startswith(os.path.realpath(work_dir) + os.sep):
            return None
        return resolved

    @staticmethod
    def capture_file_snapshot(
        work_dir: str,
        artifacts: list[str],
    ) -> dict[str, str]:
        """Capture file contents for rollback on regression."""
        snapshot: dict[str, str] = {}
        for art in artifacts:
            path = RetryPolicyEngine._safe_path(work_dir, art)
            if path is None:
                continue
            try:
                if os.path.isfile(path):
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        snapshot[art] = f.read()
            except OSError:
                pass
        return snapshot

    @staticmethod
    def restore_file_snapshot(
        work_dir: str,
        snapshot: dict[str, str],
    ) -> None:
        """Restore files from a previous snapshot.

        Creates a .bak backup of existing files before overwriting.
        """
        for art, content in snapshot.items():
            path = RetryPolicyEngine._safe_path(work_dir, art)
            if path is None:
                continue
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            # Backup existing file before overwriting (atomic)
            if os.path.isfile(path):
                backup_path = path + ".bak"
                tmp_bak = backup_path + ".tmp"
                with open(path, "r", encoding="utf-8", errors="replace") as src:
                    backup_content = src.read()
                with open(tmp_bak, "w", encoding="utf-8") as dst:
                    dst.write(backup_content)
                os.replace(tmp_bak, backup_path)
            # Write new content atomically
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, path)

    @staticmethod
    def rollback_restore(work_dir: str, snapshot: dict[str, str]) -> None:
        """Rollback a previous restore by copying .bak files back (#487).

        For each file in snapshot, if a .bak backup exists, restore it.
        Removes the .bak file after successful restore.
        """
        for art in snapshot:
            path = RetryPolicyEngine._safe_path(work_dir, art)
            if path is None:
                continue
            backup_path = path + ".bak"
            if os.path.isfile(backup_path):
                import shutil
                shutil.move(backup_path, path)

    # ------------------------------------------------------------------
    # Lint issue tolerance
    # ------------------------------------------------------------------

    @classmethod
    def is_tolerable_lint_issue(cls, issue: str) -> bool:
        """Check if a lint issue is formatting-only and safe to tolerate.

        Expects issues in 'path:line:CODE' format from evaluator metadata.
        E999 (SyntaxError) is NOT tolerable — it blocks execution.
        """
        m = re.search(r":([A-Z]\d{2,3})(?:\s|$)", issue)
        if m:
            return m.group(1) in cls.RETRY_TOLERABLE_CODES
        return False

    # ------------------------------------------------------------------
    # Zero-output artifact check
    # ------------------------------------------------------------------

    @staticmethod
    def requires_output_artifacts(node: DAGNode) -> bool:
        """Check whether a node is expected to produce output file artifacts.

        Returns True only when criteria explicitly depend on disk files.
        FILE_CHANGED-only nodes are excluded (#377) — they modify existing
        files rather than creating new ones.
        """
        is_producer = (
            node.agent_type in ("generator", "worker")
            or node.agent_type not in ("planner", "evaluator")
        )

        file_criteria = {
            CriterionType.FILE_EXISTS,
            CriterionType.FILE_CHANGED,
            CriterionType.FILE_PATTERN,
            CriterionType.TEST_FILE_EXISTS,
            CriterionType.TESTS_PASS,
        }
        found_file_types: set[CriterionType] = set()
        file_keywords = {"file", "coverage", "lint"}
        test_keywords = {"tests pass", "test pass", "test file"}
        has_legacy_file_criteria = False

        for crit in node.success_criteria:
            if isinstance(crit, SuccessCriterion) and crit.type in file_criteria:
                found_file_types.add(crit.type)
            elif isinstance(crit, str) and is_producer:
                lower = crit.lower()
                if any(kw in lower for kw in file_keywords):
                    has_legacy_file_criteria = True
                if any(kw in lower for kw in test_keywords):
                    has_legacy_file_criteria = True

        if not found_file_types and not has_legacy_file_criteria:
            return False
        # FILE_CHANGED-only means modifying existing files, not creating new (#377)
        if found_file_types == {CriterionType.FILE_CHANGED}:
            return False
        return True

    # ------------------------------------------------------------------
    # Backoff
    # ------------------------------------------------------------------

    @staticmethod
    def compute_backoff(
        retry_count: int,
        base: int = 5,
        cap: int = 300,
    ) -> float:
        """Compute exponential backoff delay in seconds."""
        return min(base ** retry_count, cap)
