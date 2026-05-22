"""EvaluationPipeline — post-execution evaluation for DAG nodes (ADR-0015).

Extracted from NodeExecutor.execute_node. Handles:
- Token usage recording
- Artifact collection and preservation on retry
- Zero-output fast-fail with test-node degeneration recovery
- Test file requirement enforcement
- Evaluator invocation with timeout
- Quality gate result processing and regression detection
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

from core.models import (
    DAG,
    NodeStatus,
    ExecutionEvent,
)
from core.config import NodeTimeoutConfig
from core.quality_gate import QualityGate
from core.retry_policy import RetryPolicyEngine
from core.progress import ProgressTracker

logger = logging.getLogger(__name__)

EventHandler = Callable[[ExecutionEvent], Coroutine[Any, Any, None]]

_TEST_NODE_PATTERN: re.Pattern = re.compile(
    r'\b(tests?|specs?)\b', re.IGNORECASE,
)


@dataclass
class EvalOutcome:
    """Result of evaluation pipeline for a node execution attempt."""

    passed: bool
    node_status: NodeStatus
    result: dict[str, Any] | None = None
    output_artifacts: list[str] = field(default_factory=list)
    error: str = ""
    eval_feedback: str = ""
    auto_eval_result: dict[str, Any] | None = None
    restored_artifacts: list[str] | None = None
    event_type: str = ""
    event_details: dict[str, Any] = field(default_factory=dict)


class EvaluationPipeline:
    """Post-execution evaluation pipeline for DAG nodes (ADR-0015).

    Token recording -> artifact collection -> zero-output check ->
    test-file check -> evaluator -> quality gate.

    Receives NodeTimeoutConfig directly (not via getter functions).
    Independently testable.
    """

    def __init__(
        self,
        evaluator: Any | None = None,
        quality_gate: QualityGate | None = None,
        budget_manager: Any | None = None,
        artifact_path: str = "./data/artifacts",
        work_dir: str | None = None,
        node_timeout_config: NodeTimeoutConfig | None = None,
        emit_func: EventHandler | None = None,
    ) -> None:
        self._evaluator = evaluator
        self._quality_gate = quality_gate or QualityGate()
        self._budget_manager = budget_manager
        self._artifact_path = artifact_path
        self._work_dir = work_dir
        self._node_timeout_config = node_timeout_config
        self._emit_func = emit_func

    @property
    def evaluator(self) -> Any | None:
        return self._evaluator

    @evaluator.setter
    def evaluator(self, value: Any | None) -> None:
        self._evaluator = value

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def evaluate(
        self,
        dag: DAG,
        node_id: str,
        result: dict[str, Any],
        *,
        workspace_path: str | None = None,
        executor: concurrent.futures.ThreadPoolExecutor | None = None,
        emit_func: EventHandler | None = None,
    ) -> EvalOutcome:
        """Run full evaluation pipeline for a node.

        Returns EvalOutcome with passed=True on success,
        passed=False on failure (caller decides retry).
        """
        node = dag.nodes[node_id]

        # Use provided emit_func or fall back to constructor one
        emit = emit_func or self._emit_func

        # Step 1: Record token usage
        self._record_token_usage(dag, node_id, result)

        # Step 2: Collect artifacts
        node = self._collect_artifacts(dag, node_id, node, result)

        # Step 3: Zero-output fast-fail
        fail = await self._check_zero_output(dag, node_id, node, emit)
        if fail is not None:
            return fail

        # Step 3.5: Post-processing: fix known invalid patterns (#767)
        self._fix_pyproject_build_backend(dag, node_id)

        # Step 4: Test file requirement
        fail = await self._check_test_files(dag, node_id, emit)
        if fail is not None:
            return fail

        # Step 5: Evaluation gate
        eval_result, fail = await self._run_evaluation(
            dag, node_id,
            workspace_path=workspace_path,
            executor=executor,
            emit=emit,
        )
        if fail is not None:
            return fail

        # Step 6: Determine final status
        node = dag.nodes[node_id]
        if (
            self._evaluator
            and node.success_criteria
            and node.agent_type == "generator"
        ):
            final_status = QualityGate.eval_status_to_node_status(
                eval_result.eval_status,
            )
        else:
            final_status = NodeStatus.SUCCESS

        reported_artifacts = result.get("artifacts", [])

        return EvalOutcome(
            passed=True,
            node_status=final_status,
            result=result,
            output_artifacts=reported_artifacts,
            event_type="completed",
            event_details={"output_count": len(node.output_artifacts or [])},
        )

    # ------------------------------------------------------------------
    # Step 1: Token usage
    # ------------------------------------------------------------------

    def _record_token_usage(
        self,
        dag: DAG,
        node_id: str,
        result: dict[str, Any],
    ) -> None:
        token_usage = result.get("token_usage", {})
        if not token_usage:
            return
        total = (
            token_usage.get("input_tokens", 0)
            + token_usage.get("output_tokens", 0)
        )
        if total == 0:
            return
        dag.update_node(node_id, token_usage={
            "input_tokens": token_usage.get("input_tokens", 0),
            "output_tokens": token_usage.get("output_tokens", 0),
            "total_tokens": total,
        }, actual_tokens=total)
        if self._budget_manager:
            self._budget_manager.record_usage(
                input_tokens=token_usage.get("input_tokens", 0),
                output_tokens=token_usage.get("output_tokens", 0),
            )

    # ------------------------------------------------------------------
    # Step 2: Artifact collection
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_artifacts(
        dag: DAG,
        node_id: str,
        node: Any,
        result: dict[str, Any],
    ) -> Any:
        previous_artifacts = node.output_artifacts or []
        reported_artifacts = result.get("artifacts", [])
        if not reported_artifacts and previous_artifacts:
            reported_artifacts = previous_artifacts
            logger.info(
                "Node %s: retry produced no new artifacts, preserving %d "
                "from previous attempt",
                node_id, len(previous_artifacts),
            )
        node = dag.update_node(node_id, output_artifacts=reported_artifacts)
        logger.debug(
            "Node %s (%s) produced artifacts: %s",
            node_id, node.agent_type, node.output_artifacts,
        )
        return node

    # ------------------------------------------------------------------
    # Step 3: Zero-output fast-fail
    # ------------------------------------------------------------------

    async def _check_zero_output(
        self,
        dag: DAG,
        node_id: str,
        node: Any,
        emit: EventHandler | None,
    ) -> EvalOutcome | None:
        if node.output_artifacts or not self._requires_output_artifacts(node):
            return None

        upstream_artifacts = self._collect_upstream_artifacts(dag, node_id)
        if upstream_artifacts and self._is_test_node(node):
            logger.warning(
                "Node %s (%s) produced zero output but upstream has "
                "%d artifacts — inheriting for evaluation (#626)",
                node_id, node.agent_type, len(upstream_artifacts),
            )
            dag.update_node(
                node_id,
                output_artifacts=upstream_artifacts,
            )
            await self._do_emit(emit, ExecutionEvent(
                node_id=node_id,
                event_type="degeneration_recovered",
                details={
                    "reason": "inherited_upstream_artifacts",
                    "inherited_count": len(upstream_artifacts),
                },
            ))
            return None

        error_msg = (
            f"Node produced zero output artifacts. "
            f"Agent type: {node.agent_type}, "
            f"task: {node.task_description[:200]}. "
            f"This typically indicates the agent exhausted "
            f"its iteration budget without writing any files."
        )
        dag.update_node(
            node_id,
            error=error_msg,
            status=NodeStatus.FAILED,
            completed_at=datetime.now(timezone.utc),
            retry_count=node.retry_count + 1,
        )
        logger.warning(
            "Node %s (%s) fast-failed: zero output artifacts",
            node_id, node.agent_type,
        )
        await self._do_emit(emit, ExecutionEvent(
            node_id=node_id,
            event_type="failed",
            details={
                "reason": "zero_output_artifacts",
                "agent_type": node.agent_type,
            },
        ))
        return EvalOutcome(
            passed=False,
            node_status=NodeStatus.FAILED,
            error=error_msg,
            event_type="failed",
            event_details={"reason": "zero_output_artifacts"},
        )

    # ------------------------------------------------------------------
    # Step 3.5: Post-processing: fix known invalid patterns (#767)
    # ------------------------------------------------------------------

    # Known invalid build-backend values that LLMs commonly produce.
    _INVALID_BUILD_BACKENDS: dict[str, str] = {
        "setuptools.backends._legacy:_Backend": "setuptools.build_meta",
        "setuptools.backends._legacy": "setuptools.build_meta",
    }

    def _fix_pyproject_build_backend(
        self,
        dag: DAG,
        node_id: str,
    ) -> None:
        """Fix known-invalid build-backend values in pyproject.toml (#767).

        Some LLMs (particularly non-Claude models) produce invalid
        build-backend values like ``setuptools.backends._legacy:_Backend``
        despite prompt instructions. This post-processing step detects
        and auto-fixes them before evaluation runs, saving ~280s of
        wasted retry time per occurrence.
        """
        if not self._work_dir:
            return

        node = dag.nodes.get(node_id)
        if not node or node.agent_type != "generator":
            return

        artifacts = node.output_artifacts or []
        pyproject_files = [
            a for a in artifacts
            if a.endswith("pyproject.toml")
        ]
        if not pyproject_files:
            return

        import os
        for rel_path in pyproject_files:
            full_path = os.path.join(self._work_dir, rel_path)
            if not os.path.isfile(full_path):
                continue

            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    content = f.read()
            except (OSError, UnicodeDecodeError):
                continue

            original = content
            for invalid, valid in self._INVALID_BUILD_BACKENDS.items():
                content = content.replace(invalid, valid)

            if content != original:
                try:
                    with open(full_path, "w", encoding="utf-8") as f:
                        f.write(content)
                    logger.warning(
                        "Fixed invalid build-backend in %s "
                        "(auto-corrected known pattern) (#767)",
                        rel_path,
                    )
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Step 4: Test file requirement
    # ------------------------------------------------------------------

    async def _check_test_files(
        self,
        dag: DAG,
        node_id: str,
        emit: EventHandler | None,
    ) -> EvalOutcome | None:
        node = dag.nodes[node_id]
        test_check = self._quality_gate.check_test_file_requirement(
            node, node_id,
        )
        if test_check is None:
            return None

        dag.update_node(
            node_id,
            eval_feedback=test_check.eval_feedback,
            error=test_check.error,
            status=test_check.node_status,
            completed_at=datetime.now(timezone.utc),
        )
        await self._do_emit(emit, ExecutionEvent(
            node_id=node_id,
            event_type=test_check.event_type,
            details=test_check.event_details,
        ))
        return EvalOutcome(
            passed=False,
            node_status=test_check.node_status,
            error=test_check.error,
            eval_feedback=test_check.eval_feedback,
            event_type=test_check.event_type,
            event_details=test_check.event_details,
        )

    # ------------------------------------------------------------------
    # Step 5: Evaluator + quality gate
    # ------------------------------------------------------------------

    async def _run_evaluation(
        self,
        dag: DAG,
        node_id: str,
        *,
        workspace_path: str | None = None,
        executor: concurrent.futures.ThreadPoolExecutor | None = None,
        emit: EventHandler | None = None,
    ) -> tuple[Any, EvalOutcome | None]:
        node = dag.nodes[node_id]

        if not (
            self._evaluator
            and node.success_criteria
            and node.agent_type == "generator"
        ):
            return None, None

        if not self._work_dir:
            logger.error(
                "Node %s: work_dir not set — cannot evaluate safely.",
                node_id,
            )
            error_msg = (
                "Evaluation skipped: work_dir not configured. "
                "Pass --project to set the working directory."
            )
            dag.update_node(
                node_id,
                status=NodeStatus.FAILED,
                error=error_msg,
                completed_at=datetime.now(timezone.utc),
            )
            await self._do_emit(emit, ExecutionEvent(
                node_id=node_id,
                event_type="failed",
                details={"reason": "no_work_dir"},
            ))
            return None, EvalOutcome(
                passed=False,
                node_status=NodeStatus.FAILED,
                error=error_msg,
                event_type="failed",
                event_details={"reason": "no_work_dir"},
            )

        eval_work_dir = workspace_path or self._work_dir

        eval_stall_timeout = self._get_stall_timeout("evaluator", node=node)
        eval_tracker = ProgressTracker(stall_timeout=eval_stall_timeout)
        eval_timeout = self._get_node_timeout("evaluator")

        try:
            loop = asyncio.get_running_loop()
            eval_result = await asyncio.wait_for(
                loop.run_in_executor(
                    executor,
                    functools.partial(
                        self._evaluator.evaluate_stage,
                        node_id, node_id, node.success_criteria,
                        self._artifact_path,
                        work_dir=eval_work_dir,
                        output_artifacts=node.output_artifacts or None,
                        owned_files=node.owned_files or None,
                        progress_tracker=eval_tracker,
                    ),
                ),
                timeout=eval_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Node %s: evaluate_stage timed out after %ds",
                node_id, eval_timeout,
            )
            error_msg = f"Evaluation timed out after {eval_timeout}s"
            dag.update_node(
                node_id,
                status=NodeStatus.FAILED,
                error=error_msg,
                completed_at=datetime.now(timezone.utc),
            )
            await self._do_emit(emit, ExecutionEvent(
                node_id=node_id,
                event_type="failed",
                details={"reason": "eval_timeout", "timeout": eval_timeout},
            ))
            return None, EvalOutcome(
                passed=False,
                node_status=NodeStatus.FAILED,
                error=error_msg,
                event_type="failed",
                event_details={"reason": "eval_timeout"},
            )

        outcome = self._quality_gate.evaluate(
            eval_result, node, node_id, eval_work_dir,
        )

        dag.update_node(
            node_id, auto_eval_result=outcome.auto_eval_result,
        )

        if not outcome.passed:
            new_retry_count = node.retry_count + outcome.retry_count_increment
            updates: dict[str, Any] = {
                "retry_count": new_retry_count,
                "eval_feedback": outcome.eval_feedback,
                "error": outcome.error,
                "status": outcome.node_status,
                "completed_at": datetime.now(timezone.utc),
            }
            if outcome.restored_artifacts is not None:
                updates["output_artifacts"] = outcome.restored_artifacts
            if new_retry_count >= node.max_retries:
                updates["auto_eval_result"] = None
            dag.update_node(node_id, **updates)

            await self._do_emit(emit, ExecutionEvent(
                node_id=node_id,
                event_type="failed",
                details={
                    "reason": "evaluation_failed",
                    "score": eval_result.score,
                    "attempt": dag.nodes[node_id].retry_count,
                },
            ))
            return None, EvalOutcome(
                passed=False,
                node_status=outcome.node_status,
                error=outcome.error,
                eval_feedback=outcome.eval_feedback,
                auto_eval_result=outcome.auto_eval_result,
                restored_artifacts=outcome.restored_artifacts,
                event_type="failed",
                event_details={
                    "reason": "evaluation_failed",
                    "score": eval_result.score,
                },
            )

        return eval_result, None

    # ------------------------------------------------------------------
    # Timeout helpers
    # ------------------------------------------------------------------

    def _get_stall_timeout(
        self, agent_type: str, node: Any = None,
    ) -> int:
        if self._node_timeout_config is not None:
            from core.node_utils import (
                estimate_feature_count,
                extract_node_complexity,
            )
            file_count, test_count, dep_count = (
                extract_node_complexity(node) if node else (0, 0, 0)
            )
            feature_count = (
                estimate_feature_count(
                    getattr(node, "task_description", ""),
                )
                if node else 0
            )
            return self._node_timeout_config.stall_timeout_for(
                agent_type,
                file_count=file_count,
                test_count=test_count,
                dep_count=dep_count,
                feature_count=feature_count,
            )
        return 120

    def _get_node_timeout(self, agent_type: str) -> int:
        if self._node_timeout_config is not None:
            return self._node_timeout_config.timeout_for(agent_type)
        return 300

    # ------------------------------------------------------------------
    # Artifact helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _requires_output_artifacts(node: Any) -> bool:
        return RetryPolicyEngine.requires_output_artifacts(node)

    @classmethod
    def _is_test_node(cls, node: Any) -> bool:
        return bool(_TEST_NODE_PATTERN.search(node.task_description))

    @staticmethod
    def _collect_upstream_artifacts(dag: DAG, node_id: str) -> list[str]:
        hard_deps = dag.get_hard_dependencies(node_id)
        soft_deps = dag.get_soft_dependencies(node_id)
        dep_ids = set(hard_deps + soft_deps)

        seen: set[str] = set()
        artifacts: list[str] = []
        for dep_id in dep_ids:
            dep_node = dag.nodes.get(dep_id)
            if (
                dep_node
                and QualityGate.is_terminal_success(dep_node.status)
                and dep_node.output_artifacts
            ):
                for a in dep_node.output_artifacts:
                    if a not in seen:
                        seen.add(a)
                        artifacts.append(a)
        return artifacts

    # ------------------------------------------------------------------
    # Event emission helper
    # ------------------------------------------------------------------

    @staticmethod
    async def _do_emit(
        emit_func: EventHandler | None,
        event: ExecutionEvent,
    ) -> None:
        if emit_func is not None:
            try:
                await emit_func(event)
            except Exception as exc:
                logger.warning(
                    "EvaluationPipeline emit failed: %s: %s",
                    type(exc).__name__, exc,
                )
