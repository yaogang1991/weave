"""Tests for #666: PendingApprovalError handling fix.

Verifies that:
1. Interactive mode prompts user for approval (no PendingApprovalError raised)
2. Non-interactive mode still returns pending_approval
3. Node executor sets PENDING_APPROVAL then re-raises for caller handling
4. Downstream nodes are skipped when a dependency is PENDING_APPROVAL
"""
import os

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from core.models import DAG, DAGNode, NodeStatus
from core.exceptions import PendingApprovalError
from core.node_executor import NodeExecutor
from guardrails.policy import (
    Guardrails, GuardrailPolicy, PermissionMode,
)


def _make_watchdog_mock():
    w = MagicMock()
    w.get_heartbeat_settings.return_value = (30.0, 5)
    w.get_alert_threshold.return_value = 3
    w._running_nodes = {}
    return w


# ---------------------------------------------------------------------------
# Guardrails interactive prompt tests
# ---------------------------------------------------------------------------


class TestInteractiveApprovalPrompt:
    """ACCEPT_EDITS + interactive=True prompts user instead of returning pending."""

    def _make_interactive_guardrails(self) -> Guardrails:
        policy = GuardrailPolicy(
            mode=PermissionMode.ACCEPT_EDITS,
            auto_approve_read=True,
        )
        return Guardrails(
            policy,
            MagicMock(),
            interactive=True,
        )

    def test_high_risk_approved_via_prompt(self):
        g = self._make_interactive_guardrails()
        with patch("builtins.input", return_value="y"), \
             patch("sys.stdin.isatty", return_value=True):
            result = g.evaluate("bash", {"command": "curl http://example.com"})
        assert result.decision == "allowed"
        assert "approved by user" in result.reason

    def test_high_risk_denied_via_prompt(self):
        g = self._make_interactive_guardrails()
        with patch("builtins.input", return_value="n"), \
             patch("sys.stdin.isatty", return_value=True):
            result = g.evaluate("bash", {"command": "curl http://example.com"})
        assert result.decision == "blocked"
        assert "denied by user" in result.reason

    def test_high_risk_denied_on_eof(self):
        g = self._make_interactive_guardrails()
        with patch("builtins.input", side_effect=EOFError), \
             patch("sys.stdin.isatty", return_value=True):
            result = g.evaluate("bash", {"command": "curl http://example.com"})
        assert result.decision == "blocked"

    def test_high_risk_denied_on_keyboard_interrupt(self):
        g = self._make_interactive_guardrails()
        with patch("builtins.input", side_effect=KeyboardInterrupt), \
             patch("sys.stdin.isatty", return_value=True):
            result = g.evaluate("bash", {"command": "curl http://example.com"})
        assert result.decision == "blocked"

    def test_medium_still_auto_approved(self):
        g = self._make_interactive_guardrails()
        result = g.evaluate("write", {"file_path": "/tmp/a.txt", "content": "x"})
        assert result.decision == "allowed"

    def test_low_still_auto_approved(self):
        g = self._make_interactive_guardrails()
        result = g.evaluate("read", {"file_path": "/tmp/test.txt"})
        assert result.decision == "allowed"

    def test_non_tty_with_env_auto_approves(self):
        """#830/#835: Non-TTY + WEAVE_NON_INTERACTIVE → auto-approve."""
        g = self._make_interactive_guardrails()
        with patch("sys.stdin.isatty", return_value=False), \
             patch.dict(os.environ, {"WEAVE_NON_INTERACTIVE": "true"}):
            result = g.evaluate("bash", {"command": "ls"})
        assert result.decision == "allowed"
        assert "auto-approved" in result.reason

    def test_non_tty_without_env_defers_to_approval(self):
        """#835: Non-TTY without WEAVE_NON_INTERACTIVE → pending_approval."""
        g = self._make_interactive_guardrails()
        with patch("sys.stdin.isatty", return_value=False), \
             patch.dict(os.environ, {}, clear=True):
            result = g.evaluate("bash", {"command": "ls"})
        assert result.decision == "pending_approval"


class TestNonInteractiveStillPending:
    """ACCEPT_EDITS + interactive=False still returns pending_approval."""

    def test_high_risk_pending_without_interactive(self):
        policy = GuardrailPolicy(
            mode=PermissionMode.ACCEPT_EDITS,
            auto_approve_read=True,
        )
        g = Guardrails(policy, MagicMock(), interactive=False)
        result = g.evaluate("bash", {"command": "curl http://example.com"})
        assert result.decision == "pending_approval"


# ---------------------------------------------------------------------------
# Node executor tests
# ---------------------------------------------------------------------------


class TestNodeExecutorPendingApproval:
    """node_executor sets PENDING_APPROVAL and re-raises for caller handling."""

    @pytest.mark.asyncio
    async def test_sets_pending_approval_and_raises(self):
        """When agent raises PendingApprovalError, node becomes PENDING_APPROVAL then re-raises."""
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="node_a",
            agent_type="generator",
            task_description="Generate code",
        ))

        async def mock_agent_executor(node, artifacts, **kwargs):
            raise PendingApprovalError(ticket_id="ticket_test")

        node_executor = NodeExecutor(
            agent_executor=mock_agent_executor,
            emit_func=AsyncMock(),
            watchdog=_make_watchdog_mock(),
        )

        with pytest.raises(PendingApprovalError):
            await node_executor.execute_node(dag, "node_a")
        assert dag.nodes["node_a"].status == NodeStatus.PENDING_APPROVAL

    @pytest.mark.asyncio
    async def test_downstream_skipped_on_pending_approval_hard_dep(self):
        """Node with PENDING_APPROVAL hard dep should be SKIPPED."""
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="upstream",
            agent_type="generator",
            task_description="Upstream",
            status=NodeStatus.PENDING_APPROVAL,
        ))
        dag.add_node(DAGNode(
            id="downstream",
            agent_type="evaluator",
            task_description="Downstream",
        ))
        dag.add_edge("upstream", "downstream")

        executor_called = False

        async def mock_agent_executor(node, artifacts, **kwargs):
            nonlocal executor_called
            executor_called = True
            return {"output": "ok"}

        node_executor = NodeExecutor(
            agent_executor=mock_agent_executor,
            emit_func=AsyncMock(),
            watchdog=_make_watchdog_mock(),
        )

        await node_executor.execute_node(dag, "downstream")
        assert dag.nodes["downstream"].status == NodeStatus.SKIPPED
        assert not executor_called

    @pytest.mark.asyncio
    async def test_downstream_continues_on_soft_pending_approval(self):
        """Soft dep PENDING_APPROVAL: node continues with warning."""
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="upstream",
            agent_type="generator",
            task_description="Upstream",
            status=NodeStatus.PENDING_APPROVAL,
        ))
        dag.add_node(DAGNode(
            id="downstream",
            agent_type="evaluator",
            task_description="Downstream",
        ))
        dag.add_edge("upstream", "downstream", dependency_type="soft")

        executor_called = False

        async def mock_agent_executor(node, artifacts, **kwargs):
            nonlocal executor_called
            executor_called = True
            return {"output": "ok"}

        node_executor = NodeExecutor(
            agent_executor=mock_agent_executor,
            emit_func=AsyncMock(),
            watchdog=_make_watchdog_mock(),
        )

        await node_executor.execute_node(dag, "downstream")
        assert executor_called
        assert dag.nodes["downstream"].status == NodeStatus.SUCCESS
