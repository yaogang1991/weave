"""
ArtifactHandoffService: collect and structure handoff artifacts between DAG nodes.

Extracted from DAGExecutionEngine._collect_input_artifacts (#177 PR4).
Manages three types of handoff:
1. Output artifacts from successful dependency nodes
2. Auto-eval results passed to downstream evaluator agents
3. Retry feedback and error-type-specific guidance
4. Soft dependency failure warnings
"""
from __future__ import annotations

import logging
from typing import Any

from core.models import (
    DAG,
    HandoffArtifact,
    NodeStatus,
)

logger = logging.getLogger(__name__)


class ArtifactHandoffService:
    """Collects and structures handoff artifacts between DAG nodes.

    Responsible for gathering upstream node outputs, evaluation results,
    memory sharing, retry feedback with error-type guidance, and soft
    dependency warnings — all packaged as HandoffArtifact instances.
    """

    def __init__(
        self,
        memory_manager: Any | None = None,
        session_id: str | None = None,
        isolation_guard: Any | None = None,
    ) -> None:
        self._memory_manager = memory_manager
        self._session_id = session_id
        self._isolation_guard = isolation_guard

    def collect(
        self,
        dag: DAG,
        node_id: str,
        failed_soft: list[str] | None = None,
        is_terminal_success: Any = None,
    ) -> list[HandoffArtifact]:
        """Collect output artifacts from all dependency nodes.

        Args:
            dag: The execution DAG.
            node_id: The node to collect artifacts for.
            failed_soft: List of soft dependency IDs that failed/skipped.
            is_terminal_success: Callable to check if a NodeStatus is
                terminal success. Defaults to checking SUCCESS/PARTIAL_PASS/WARNED.
        """
        if is_terminal_success is None:
            is_terminal_success = self._default_is_terminal_success

        dependencies = dag.get_dependencies(node_id)
        artifacts: list[HandoffArtifact] = []

        for dep_id in dependencies:
            dep_node = dag.nodes[dep_id]
            if not is_terminal_success(dep_node.status):
                continue

            # Basic output artifact from dependency
            artifact = HandoffArtifact(
                from_agent=dep_node.agent_type,
                to_agent=dag.nodes[node_id].agent_type,
                content=dep_node.result.get("summary", ""),
                file_paths=dep_node.output_artifacts,
                metadata={
                    "from_node": dep_id,
                    "task": dep_node.task_description,
                },
            )
            artifacts.append(artifact)

            # Pass auto-eval results to downstream evaluator agents (#145).
            self._maybe_add_eval_result(
                artifacts, dag, node_id, dep_node, dep_id,
            )

            # M3.2: Share relevant memories from upstream agent
            self._maybe_share_memory(dag, node_id, dep_node)

        # Include evaluation feedback from previous attempt (retry scenario)
        node = dag.nodes[node_id]
        if node.eval_feedback:
            self._add_retry_feedback(artifacts, node)

        # Soft dependency warning
        if failed_soft:
            self._add_soft_dep_warning(artifacts, dag, node_id, failed_soft)

        # #511 isolation layer: scan handoffs for injection patterns
        if self._isolation_guard and artifacts:
            scan = self._isolation_guard.scan_handoffs(
                artifacts, from_node_id="upstream", to_node_id=node_id,
            )
            if scan.injected:
                logger.warning(
                    "Isolation guard flagged %d/%d handoff artifacts for node %s "
                    "(#511): risk=%s patterns=%s",
                    scan.injected_artifact_count,
                    scan.total_artifact_count,
                    node_id,
                    scan.risk_level,
                    scan.patterns_matched,
                )
                return scan.sanitized_artifacts

        return artifacts

    # ------------------------------------------------------------------
    # Internal methods
    # ------------------------------------------------------------------

    @staticmethod
    def _default_is_terminal_success(status: NodeStatus) -> bool:
        return status in (
            NodeStatus.SUCCESS,
            NodeStatus.PARTIAL_PASS,
            NodeStatus.WARNED,
        )

    @staticmethod
    def _maybe_add_eval_result(
        artifacts: list[HandoffArtifact],
        dag: DAG,
        node_id: str,
        dep_node: Any,
        dep_id: str,
    ) -> None:
        """Pass auto-eval results to downstream evaluator agents (#145)."""
        if not dep_node.auto_eval_result:
            return
        if dep_node.auto_eval_result.get("passed") is not True:
            return
        if dag.nodes[node_id].agent_type != "evaluator":
            return

        eval_info = dep_node.auto_eval_result
        criteria = eval_info.get("criteria_results", {})
        has_warnings = criteria and not all(criteria.values())

        header = (
            "AUTOMATED EVALUATION RESULTS "
            "(passed via threshold — some criteria have WARNINGS)"
            if has_warnings
            else "AUTOMATED EVALUATION RESULTS (already verified)"
        )
        summary = (
            f"{header}:\n"
            f"- Passed: {eval_info.get('passed')}\n"
            f"- Score: {eval_info.get('score')}\n"
            f"- Criteria: {criteria}\n"
            f"- Feedback:\n{eval_info.get('feedback', '')}\n"
        )
        artifacts.append(HandoffArtifact(
            from_agent="auto_evaluator",
            to_agent="evaluator",
            content=summary,
            metadata={
                "type": "evaluation_result",
                "passed": eval_info.get("passed"),
                "score": eval_info.get("score"),
                "criteria_results": criteria,
                "feedback": eval_info.get("feedback"),
                "has_warnings": has_warnings,
            },
        ))

    def _maybe_share_memory(
        self, dag: DAG, node_id: str, dep_node: Any,
    ) -> None:
        """M3.2: Share relevant memories from upstream agent."""
        if not self._memory_manager or not self._session_id:
            return
        if dep_node.agent_type == dag.nodes[node_id].agent_type:
            return
        try:
            from memory.sharing import MemorySharing
            sharing = MemorySharing(self._memory_manager)
            sharing.share_with_downstream(
                from_agent=dep_node.agent_type,
                to_agent=dag.nodes[node_id].agent_type,
                session_id=self._session_id,
                dag=dag,
                node_id=node_id,
            )
        except Exception as e:
            logger.debug("Memory sharing failed: %s", e)

    @staticmethod
    def _add_retry_feedback(
        artifacts: list[HandoffArtifact], node: Any,
    ) -> None:
        """Include evaluation feedback from previous attempt with targeted guidance."""
        feedback = node.eval_feedback

        # Detect error patterns and add targeted guidance (#311)
        naming_guidance = ""
        if _has_import_error(feedback):
            naming_guidance += (
                "\nNAMING MISMATCH DETECTED: Your tests import "
                "symbols that don't exist in the source modules. "
                "To fix:\n"
                "1. READ the source files first to discover the "
                "actual class/function names\n"
                "2. Run: `python -c 'from module import Symbol'` "
                "to verify each import\n"
                "3. Fix your TEST code to match the actual source "
                "API — do NOT modify the source\n"
            )
        if _has_type_error(feedback):
            naming_guidance += (
                "\nTYPE ERROR DETECTED: Your code calls functions "
                "with wrong arguments or mismatched async/sync "
                "patterns.\n"
                "1. Check if async functions are called without "
                "`await` or `asyncio.run()`\n"
                "2. Verify function signatures match actual "
                "parameter names\n"
            )
        if _has_timeout(feedback):
            naming_guidance += (
                "\nTIMEOUT DETECTED: Your tests or code hung during "
                "execution.\n"
                "1. Check for infinite loops or missing loop "
                "termination conditions\n"
                "2. Use daemon threads and proper teardown in tests\n"
                "3. Add timeouts to any blocking operations "
                "(network, subprocess)\n"
                "4. Avoid global state or locks that can deadlock\n"
            )
        if _has_coverage_low(feedback):
            naming_guidance += (
                "\nLOW COVERAGE DETECTED: Coverage is below target.\n"
                "1. Do NOT rewrite existing tests or source code\n"
                "2. ADD new test functions that cover untested "
                "branches and edge cases\n"
                "3. Focus on: error paths, boundary conditions, "
                "empty inputs\n"
                "4. Run coverage to see which lines are missed: "
                "`pytest --cov=module --cov-report=term-missing`\n"
            )
        if _has_runtime_error(feedback):
            naming_guidance += (
                "\nRUNTIME ERROR DETECTED: Source code has bugs "
                "that cause crashes.\n"
                "1. Read the traceback to find the exact crash "
                "location\n"
                "2. You may EDIT source files to fix the bug "
                "(targeted fix, not rewrite)\n"
                "3. Common fixes: add missing method calls, "
                "fix None checks, add initialization\n"
            )
        if _has_init_import_error(feedback):
            naming_guidance += (
                "\n__INIT__.PY IMPORT ERROR DETECTED: Your "
                "__init__.py eagerly imports modules/packages "
                "that don't exist yet (#423).\n"
                "1. Rewrite __init__.py to be MINIMAL — just a "
                "docstring or empty\n"
                "2. Do NOT import from submodules that other "
                "nodes haven't created yet\n"
                "3. Do NOT import external packages that may "
                "not be installed\n"
                "4. If re-exports are needed, use lazy imports: "
                "def __getattr__(name): ...\n"
            )
        if _has_lint_error(feedback):
            naming_guidance += (
                "\nLINT ERROR DETECTED: Your code has flake8 "
                "lint errors.\n"
                "1. READ the specific error codes and line numbers "
                "in the feedback above\n"
                "2. Use the EDIT tool to fix ONLY the reported "
                "issues — do NOT rewrite entire files\n"
                "3. Common fixes:\n"
                "   - F811 (redefinition): Remove the duplicate "
                "definition, keep only one\n"
                "   - F401 (unused import): Remove the unused "
                "import line\n"
                "   - E302/E303 (blank lines): Add/remove "
                "blank lines as specified\n"
                "   - E501 (line too long): Break long lines\n"
                "4. After editing, verify: flake8 --max-line-length=100 "
                "<file>\n"
            )

        retry_hint = (
            f"RETRY ATTEMPT #{node.retry_count}: Your previous "
            f"attempt FAILED evaluation.\n\n"
            f"Evaluation feedback:\n{feedback}\n\n"
            f"{naming_guidance}"
            f"IMPORTANT: Do NOT repeat the same approach. "
            f"Analyze what went wrong and try a DIFFERENT "
            f"strategy."
        )
        artifacts.append(HandoffArtifact(
            from_agent="evaluator",
            to_agent=node.agent_type,
            content=retry_hint,
            metadata={
                "type": "eval_feedback",
                "attempt": node.retry_count,
            },
        ))

    @staticmethod
    def _add_soft_dep_warning(
        artifacts: list[HandoffArtifact],
        dag: DAG,
        node_id: str,
        failed_soft: list[str],
    ) -> None:
        """Soft dependency warning (#271): inform node about failed soft deps."""
        dep_summaries = []
        for dep_id in failed_soft:
            dep_node = dag.nodes[dep_id]
            dep_summaries.append(
                f"- {dep_id} ({dep_node.agent_type}): "
                f"{dep_node.status.value}"
                f"{'; ' + dep_node.error[:200] if dep_node.error else ''}"
            )
        warning_content = (
            "DEPENDENCY WARNING: The following soft (optional) "
            "dependencies failed or were skipped:\n"
            + "\n".join(dep_summaries)
            + "\n\nYou may proceed, but outputs from these nodes "
            "are NOT available."
        )
        artifacts.append(HandoffArtifact(
            from_agent="dag_engine",
            to_agent=dag.nodes[node_id].agent_type,
            content=warning_content,
            metadata={
                "type": "dependency_warning",
                "failed_soft_deps": failed_soft,
                "dep_statuses": {
                    dep_id: dag.nodes[dep_id].status.value
                    for dep_id in failed_soft
                },
            },
        ))


# ---------------------------------------------------------------------------
# Error pattern detection helpers (module-level for testability)
# ---------------------------------------------------------------------------

def _has_import_error(feedback: str) -> bool:
    return (
        "ImportError" in feedback
        or "cannot import" in feedback
        or "ModuleNotFoundError" in feedback
    )


def _has_type_error(feedback: str) -> bool:
    return (
        "TypeError" in feedback
        or "unexpected keyword" in feedback
    )


def _has_timeout(feedback: str) -> bool:
    return (
        "timed out" in feedback
        or "TimeoutExpired" in feedback
        or "timeout" in feedback.lower()
    )


def _has_coverage_low(feedback: str) -> bool:
    lower = feedback.lower()
    return (
        "coverage" in lower
        and ("below target" in lower
             or "not verified" in lower
             or "could not be parsed" in lower)
    )


def _has_runtime_error(feedback: str) -> bool:
    return (
        "RuntimeError" in feedback
        or "AttributeError" in feedback
        or "KeyError" in feedback
    )


def _has_init_import_error(feedback: str) -> bool:
    """Detect __init__.py eager-import failures (#423)."""
    return (
        "__init__" in feedback
        and ("import_check" in feedback or "ImportError" in feedback)
    )


def _has_lint_error(feedback: str) -> bool:
    """Detect lint errors (flake8 codes like F811, F401, E302, etc.)."""
    import re
    return bool(re.search(r"\b[FWE]\d{3}\b", feedback))
