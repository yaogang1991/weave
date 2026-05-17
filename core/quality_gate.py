"""
QualityGate: evaluation and feedback construction for DAG nodes.

Extracted from DAGExecutionEngine._execute_single_node (#177 PR4).
Handles evaluator invocation, result interpretation, retry feedback
generation, and regression-aware restoration logic.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from core.models import (
    EvalStatus,
    EvaluationResult,
    NodeStatus,
)

logger = logging.getLogger(__name__)


@dataclass
class EvaluationOutcome:
    """Result of a quality gate evaluation for a single node."""

    passed: bool
    node_status: NodeStatus
    eval_feedback: str = ""
    error: str = ""
    auto_eval_result: dict[str, Any] | None = None
    retry_count_increment: int = 0
    should_restore_best: bool = False
    restored_artifacts: list[str] | None = None
    event_type: str = "completed"
    event_details: dict[str, Any] = field(default_factory=dict)


class QualityGate:
    """Manages node evaluation, feedback construction, and regression tracking.

    Coordinates between the EvaluatorEngine and RetryPolicyEngine to:
    1. Run evaluation criteria against node output
    2. Map evaluation results to node statuses
    3. Build targeted retry feedback
    4. Detect and handle regressions
    """

    def __init__(self, retry_policy: Any = None) -> None:
        self._retry_policy = retry_policy

    # ------------------------------------------------------------------
    # Static helpers (originally on DAGExecutionEngine)
    # ------------------------------------------------------------------

    @staticmethod
    def eval_status_to_node_status(eval_status: EvalStatus) -> NodeStatus:
        """Map EvalStatus from evaluator to NodeStatus for DAG nodes (#270)."""
        mapping = {
            EvalStatus.CLEAN_PASS: NodeStatus.SUCCESS,
            EvalStatus.PARTIAL_PASS: NodeStatus.PARTIAL_PASS,
            EvalStatus.WARNED: NodeStatus.WARNED,
            EvalStatus.FAILED: NodeStatus.FAILED,
        }
        return mapping.get(eval_status, NodeStatus.SUCCESS)

    @staticmethod
    def is_terminal_success(status: NodeStatus) -> bool:
        """Check if a node status represents a successful terminal state (#270).

        SUCCESS, PARTIAL_PASS, and WARNED all allow downstream to continue.
        """
        return status in (
            NodeStatus.SUCCESS,
            NodeStatus.PARTIAL_PASS,
            NodeStatus.WARNED,
        )

    @staticmethod
    def is_test_file_exists_criterion(criterion: str | object) -> bool:
        """Check if a criterion requires test files to exist (#247)."""
        if hasattr(criterion, "type"):
            return getattr(criterion.type, "value", "") == "test_file_exists"
        if isinstance(criterion, str):
            lower = criterion.lower()
            return "test_file_exist" in lower or "test file exist" in lower
        return False

    @staticmethod
    def _is_test_file(artifact_path: str) -> bool:
        """Broader test file detection: test_*.py, *_test.py, *_spec.py,
        files under tests/ or test/ directories."""
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

    # ------------------------------------------------------------------
    # Test-file requirement check
    # ------------------------------------------------------------------

    def check_test_file_requirement(
        self,
        node: Any,
        node_id: str,
    ) -> EvaluationOutcome | None:
        """Check if node requires test files but produced none.

        Returns an EvaluationOutcome with failure status if the requirement
        is violated, or None if the check passes.
        """
        if not node.success_criteria:
            return None

        has_test_criterion = any(
            self.is_test_file_exists_criterion(c)
            for c in node.success_criteria
        )
        if not has_test_criterion:
            return None

        output_artifacts = node.output_artifacts or []
        has_test_files = any(
            self._is_test_file(a) for a in output_artifacts
        )
        if has_test_files:
            return None

        logger.warning(
            "Node %s: TEST_FILE_EXISTS required but no test files "
            "in output_artifacts — failing fast (#247)",
            node_id,
        )
        return EvaluationOutcome(
            passed=False,
            node_status=NodeStatus.FAILED,
            eval_feedback=(
                f"EVALUATION FAILED: You were required to create test "
                f"files, but none were found in your output.\n"
                f"Output artifacts: {output_artifacts}\n\n"
                f"You MUST create test files (e.g., test_*.py) for "
                f"your implementation. Create them using the write "
                f"tool BEFORE finishing.\n"
                f"Focus on: functional tests, edge cases, import "
                f"validation. Each source module should have a "
                f"corresponding test file."
            ),
            error="No test files created (TEST_FILE_EXISTS required)",
            event_type="failed",
            event_details={
                "reason": "no_test_files",
                "artifacts": output_artifacts,
            },
        )

    # ------------------------------------------------------------------
    # Evaluation + retry feedback
    # ------------------------------------------------------------------

    def evaluate(
        self,
        eval_result: EvaluationResult,
        node: Any,
        node_id: str,
        work_dir: str,
    ) -> EvaluationOutcome:
        """Process evaluation result and produce an outcome.

        Handles:
        - Mapping eval result to node status
        - Regression detection via RetryPolicyEngine
        - Best-attempt file restoration
        - Targeted retry feedback construction
        """
        # Map eval status to node status
        if eval_result.passed:
            mapped_status = self.eval_status_to_node_status(
                eval_result.eval_status,
            )
            return EvaluationOutcome(
                passed=True,
                node_status=mapped_status,
                auto_eval_result=eval_result.model_dump(),
                event_type="completed",
                event_details={
                    "score": eval_result.score,
                    "passed": True,
                },
            )

        # Evaluation failed — build retry feedback
        auto_eval = eval_result.model_dump()

        if self._retry_policy is None:
            return EvaluationOutcome(
                passed=False,
                node_status=NodeStatus.FAILED,
                eval_feedback=eval_result.feedback,
                error=f"Evaluation failed (score: {eval_result.score}): {eval_result.feedback}",
                auto_eval_result=auto_eval,
                retry_count_increment=1,
                event_type="retrying",
                event_details={
                    "score": eval_result.score,
                    "feedback": eval_result.feedback[:200],
                },
            )

        # Delegate regression tracking to RetryPolicyEngine
        is_regression, best = self._retry_policy.record_attempt(
            node_id=node_id,
            score=eval_result.score,
            feedback=eval_result.feedback,
            output_artifacts=node.output_artifacts,
            work_dir=work_dir,
            eval_metadata=eval_result.metadata,
            criteria_results=getattr(eval_result, "criteria_results", {}),
            passed=eval_result.passed,
        )

        # Regression detected: restore best attempt files (#212).
        should_restore = is_regression and best and "file_snapshot" in best
        restored_artifacts = None

        if should_restore:
            logger.info(
                "Node %s: restoring best attempt artifacts "
                "(score %.1f > current %.1f)",
                node_id, best["score"], eval_result.score,
            )
            self._retry_policy.restore_file_snapshot(
                work_dir, best["file_snapshot"],
            )
            # Delete extra files added by the regression attempt
            best_artifact_set = best.get(
                "artifact_set", set(best["file_snapshot"].keys()),
            )
            for artifact in (node.output_artifacts or []):
                if artifact not in best_artifact_set:
                    path = os.path.join(work_dir, artifact)
                    try:
                        if os.path.isfile(path):
                            os.remove(path)
                            logger.info(
                                "Node %s: removed extra file %s "
                                "not in best attempt",
                                node_id, artifact,
                            )
                    except OSError:
                        pass
            restored_artifacts = best.get("artifacts")

        # Build retry feedback with regression awareness
        best = self._retry_policy.get_best(node_id)
        regression_hint = ""
        if is_regression or (best and eval_result.score < best["score"]):
            regression_hint = (
                "\n\nWARNING: Your previous attempt scored higher "
                f"({best['score']:.1f} vs current {eval_result.score:.1f}). "
                "The code may already be correct — only fix the "
                "specific issues reported, do NOT rewrite working code."
            )

        # Add targeted lint fix guidance (#151)
        prev_issues = best.get("lint_issues", set()) if best else set()
        curr_issues = set(
            eval_result.metadata.get(
                "lint_new_issues",
                eval_result.metadata.get("lint_all_issues", []),
            )
        )
        new_only = curr_issues - prev_issues
        if new_only and not regression_hint:
            lint_guidance = (
                "\n\nLINT_FIX_GUIDANCE: Fix ONLY these new lint "
                "issues. Do NOT rewrite working code:\n"
                + "\n".join(
                    f"  - {iss}" for iss in sorted(new_only)[:10]
                )
            )
        else:
            lint_guidance = ""

        eval_feedback = (
            f"{eval_result.feedback}\n\n"
            f"Output artifacts: {node.output_artifacts or 'none'}\n\n"
            f"IMPORTANT: Fix the issues INCREMENTALLY. Do NOT rewrite working "
            f"code from scratch. Use the edit tool to fix specific problems.\n"
            f"Fix ALL issues listed above."
            f"{regression_hint}"
            f"{lint_guidance}"
        )

        error = f"Evaluation failed (score: {eval_result.score}): {eval_result.feedback}"

        # On regression, update auto_eval_result with best attempt info
        if is_regression and best:
            auto_eval = {
                "passed": False,
                "score": best["score"],
                "feedback": best["feedback"],
                "_note": (
                    "Updated to best-attempt result "
                    "(regression detected)"
                ),
            }

        return EvaluationOutcome(
            passed=False,
            node_status=NodeStatus.FAILED,
            eval_feedback=eval_feedback,
            error=error,
            auto_eval_result=auto_eval,
            retry_count_increment=1,
            should_restore_best=should_restore,
            restored_artifacts=restored_artifacts,
            event_type="retrying",
            event_details={
                "score": eval_result.score,
                "feedback": eval_result.feedback[:200],
                "is_regression": is_regression,
            },
        )
