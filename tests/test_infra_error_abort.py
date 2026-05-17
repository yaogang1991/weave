"""
Tests for #187: infrastructure error detection in adapt_to_failure.

Ensures that infrastructure/environment errors (missing tools, broken
paths, etc.) cause immediate abort instead of wasting retry budget.
"""
import pytest
from unittest.mock import MagicMock, patch

from core.models import DAG, DAGNode, NodeStatus
from orchestrator.intelligent_orchestrator import (  # noqa: F401
    IntelligentOrchestrator,
    _is_infrastructure_error,
    INFRASTRUCTURE_ERROR_PATTERNS,
    _KNOWN_TOOL_COMMANDS,
)


@pytest.fixture
def orchestrator(tmp_path):
    from core.config import LLMConfig
    from session.store import SessionStore

    store = SessionStore(base_path=str(tmp_path / "events"))
    config = LLMConfig(model="test-model")
    return IntelligentOrchestrator(
        llm_config=config,
        session_store=store,
        agent_registry=MagicMock(),
    )


def _make_dag_with_failed_node(error: str) -> tuple[DAG, str]:
    """Create a DAG with a single failed node containing the given error."""
    dag = DAG(reasoning="test")
    node = DAGNode(
        id="gen_1",
        agent_type="generator",
        task_description="test",
        status=NodeStatus.FAILED,
        error=error,
    )
    dag.add_node(node)
    return dag, "gen_1"


class TestInfrastructureErrorDetection:
    """Unit tests for _is_infrastructure_error pattern matching."""

    def test_no_linter_available(self):
        assert _is_infrastructure_error("FAIL: No linter available")

    def test_command_not_found(self):
        assert _is_infrastructure_error("bash: python: command not found")

    def test_command_not_found_pytest(self):
        assert _is_infrastructure_error("bash: pytest: command not found")

    def test_command_not_found_flake8(self):
        assert _is_infrastructure_error("bash: flake8: command not found")

    def test_command_not_found_unknown_tool_not_infra(self):
        """Project-specific CLI commands should NOT be classified as infra."""
        assert not _is_infrastructure_error("bash: my_project_cli: command not found")

    def test_command_not_found_make_target_not_infra(self):
        """Make targets should NOT be classified as infra."""
        assert not _is_infrastructure_error("make: generate: command not found")

    def test_module_not_found_not_infra(self):
        """ModuleNotFoundError is often a code issue, not infra."""
        assert not _is_infrastructure_error("ModuleNotFoundError: No module named 'flake8'")

    def test_no_such_file_not_infra(self):
        """FileNotFoundError is often a code issue, not infra."""
        assert not _is_infrastructure_error(
            "FileNotFoundError: No such file or directory: reporter/report.py"
        )

    def test_permission_denied(self):
        assert _is_infrastructure_error("Permission denied: /root/secret")

    def test_connection_refused(self):
        assert _is_infrastructure_error("Connection refused: localhost:5432")

    def test_no_module_named_not_infra(self):
        """No module named is often a code issue, not infra."""
        assert not _is_infrastructure_error("No module named 'pytest'")

    def test_code_quality_error_not_infra(self):
        """Normal code errors should NOT be classified as infrastructure."""
        assert not _is_infrastructure_error("AssertionError: expected 1 got 2")

    def test_empty_error(self):
        assert not _is_infrastructure_error("")

    def test_none_error(self):
        assert not _is_infrastructure_error(None)


class TestAdaptToFailureInfraAbort:
    """adapt_to_failure should abort immediately for infrastructure errors
    without calling the LLM."""

    @pytest.mark.asyncio
    async def test_no_linter_aborts_without_llm_call(self, orchestrator):
        """Infrastructure error → immediate abort, no LLM call."""
        dag, node_id = _make_dag_with_failed_node(
            "Evaluation failed: No linter available (install flake8 or ruff)"
        )
        with patch.object(orchestrator.llm, "call") as mock_llm:
            decision = await orchestrator.adapt_to_failure(dag, node_id)
            mock_llm.assert_not_called()

        assert decision.action == "abort"
        assert "infrastructure" in decision.reasoning.lower()

    @pytest.mark.asyncio
    async def test_module_not_found_goes_to_llm(self, orchestrator):
        """ModuleNotFoundError may be fixable by retry — should reach LLM."""
        dag, node_id = _make_dag_with_failed_node(
            "ModuleNotFoundError: No module named 'requests'"
        )
        with patch.object(orchestrator.llm, "call", return_value={
            "content": '{"action": "retry", "reasoning": "fix the import"}',
        }):
            decision = await orchestrator.adapt_to_failure(dag, node_id)

        assert decision.action == "retry"

    @pytest.mark.asyncio
    async def test_code_error_does_not_abort_early(self, orchestrator):
        """Normal code errors should still go through LLM-based decision."""
        dag, node_id = _make_dag_with_failed_node(
            "AssertionError: tests/test_main.py::test_x failed"
        )
        with patch.object(orchestrator.llm, "call", return_value={
            "content": '{"action": "retry", "reasoning": "fix the test"}',
        }):
            decision = await orchestrator.adapt_to_failure(dag, node_id)

        # Should reach the LLM (not aborted early)
        assert decision.action == "retry"

    @pytest.mark.asyncio
    async def test_error_passed_as_param(self, orchestrator):
        """Infrastructure error passed via the `error` param (not node.error)."""
        dag = DAG(reasoning="test")
        node = DAGNode(
            id="gen_2",
            agent_type="generator",
            task_description="test",
            status=NodeStatus.FAILED,
        )
        dag.add_node(node)

        with patch.object(orchestrator.llm, "call") as mock_llm:
            decision = await orchestrator.adapt_to_failure(
                dag, "gen_2", error="command not found: python3",
            )
            mock_llm.assert_not_called()

        assert decision.action == "abort"
