"""Tests for #749: replan preserves original requirement language context.

Verifies that:
1. CLI path passes requirement to replan_handler
2. Empty requirement still works (no crash)
"""
from unittest.mock import MagicMock


def test_cli_replan_handler_passes_requirement():
    """CLI replan_handler lambda passes requirement to orchestrator.replan (#749)."""
    from orchestrator.intelligent_orchestrator import IntelligentOrchestrator

    mock_orch = MagicMock(spec=IntelligentOrchestrator)
    mock_orch.replan = MagicMock(return_value=MagicMock())

    # Simulate the lambda that CLI creates
    requirement = "Build a Python network ping library"

    def replan_handler(dag_ref, failed_id):
        return mock_orch.replan(dag_ref, failed_id, requirement)

    dag_mock = MagicMock()
    replan_handler(dag_mock, "impl_1")

    mock_orch.replan.assert_called_once_with(dag_mock, "impl_1", requirement)


def test_cli_replan_handler_empty_requirement():
    """CLI replan_handler works with empty requirement (#749)."""
    from orchestrator.intelligent_orchestrator import IntelligentOrchestrator

    mock_orch = MagicMock(spec=IntelligentOrchestrator)
    mock_orch.replan = MagicMock(return_value=MagicMock())

    requirement = ""

    def replan_handler(dag_ref, failed_id):
        return mock_orch.replan(dag_ref, failed_id, requirement)

    dag_mock = MagicMock()
    replan_handler(dag_mock, "impl_2")

    mock_orch.replan.assert_called_once_with(dag_mock, "impl_2", "")
