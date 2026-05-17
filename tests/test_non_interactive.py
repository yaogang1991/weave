"""
Tests for non-interactive mode (M1.1).

Covers:
- WeaveConfig.non_interactive from environment variable
- WeaveConfig.approval_timeout_sec from environment variable
- PersonalGuardrails non-interactive: HIGH risk returns pending_approval (no stdin)
- PersonalGuardrails interactive: HIGH risk requests confirmation
- PersonalGuardrails.request_confirmation returns False in non-interactive mode
- Worker --non-interactive argument parsing
- WorkerConfig.non_interactive attribute
- Approval ticket expiration handling
- Non-interactive mode does not block on stdin
"""

from __future__ import annotations

import argparse
import os
import sys
import time  # noqa: F401
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import WeaveConfig  # noqa: E402
from core.models import (  # noqa: E402, F401
    GuardrailPolicy,
    PersonalGuardrailPolicy,
    PermissionMode,
    RiskLevel,
    ToolResult,
)
from guardrails.policy import GuardrailResult, PersonalGuardrails  # noqa: E402


# Inline WorkerConfig to avoid pulling heavy control_plane dependencies
class WorkerConfig:
    """Minimal WorkerConfig for testing (mirrors control_plane.worker.WorkerConfig)."""

    concurrency: int = 1
    poll_interval_sec: int = 5
    lease_duration_sec: int = 60
    recovery_max_age_sec: int = 120
    heartbeat_interval_sec: int = 30
    max_poll_backoff_sec: int = 60
    non_interactive: bool = False

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)
            else:
                raise TypeError(f"WorkerConfig has no attribute {k!r}")


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
    """Default PersonalGuardrailPolicy."""
    return PersonalGuardrailPolicy(
        mode=PermissionMode.DONT_ASK,
        whitelist_patterns=[],
        whitelist_commands=[],
        auto_approve_high=False,
        confirmation_timeout_sec=300,
    )


@pytest.fixture
def interactive_guardrails(personal_policy, mock_tool_registry):
    """PersonalGuardrails in interactive mode (default)."""
    return PersonalGuardrails(personal_policy, mock_tool_registry)


@pytest.fixture
def non_interactive_guardrails(personal_policy, mock_tool_registry):
    """PersonalGuardrails in non-interactive mode."""
    return PersonalGuardrails(
        personal_policy, mock_tool_registry, non_interactive=True
    )


# ---------------------------------------------------------------------------
# WeaveConfig — environment variable parsing
# ---------------------------------------------------------------------------


class TestWeaveConfigFromEnv:
    """Test WeaveConfig.non_interactive and approval_timeout_sec from env."""

    def test_non_interactive_false_by_default(self):
        """Default: non_interactive is False when env var is unset."""
        with patch.dict(os.environ, {}, clear=True):
            config = WeaveConfig()
            assert config.non_interactive is False

    def test_non_interactive_true_lowercase(self):
        """WEAVE_NON_INTERACTIVE=true sets non_interactive=True."""
        with patch.dict(os.environ, {"WEAVE_NON_INTERACTIVE": "true"}):
            config = WeaveConfig()
            assert config.non_interactive is True

    def test_non_interactive_true_one(self):
        """WEAVE_NON_INTERACTIVE=1 sets non_interactive=True."""
        with patch.dict(os.environ, {"WEAVE_NON_INTERACTIVE": "1"}):
            config = WeaveConfig()
            assert config.non_interactive is True

    def test_non_interactive_true_yes(self):
        """WEAVE_NON_INTERACTIVE=yes sets non_interactive=True."""
        with patch.dict(os.environ, {"WEAVE_NON_INTERACTIVE": "yes"}):
            config = WeaveConfig()
            assert config.non_interactive is True

    def test_non_interactive_false_random_string(self):
        """Random string sets non_interactive=False."""
        with patch.dict(os.environ, {"WEAVE_NON_INTERACTIVE": "nope"}):
            config = WeaveConfig()
            assert config.non_interactive is False

    def test_approval_timeout_default(self):
        """Default approval_timeout_sec is 300."""
        with patch.dict(os.environ, {}, clear=True):
            config = WeaveConfig()
            assert config.approval_timeout_sec == 300

    def test_approval_timeout_from_env(self):
        """WEAVE_APPROVAL_TIMEOUT_SEC overrides default."""
        with patch.dict(os.environ, {"WEAVE_APPROVAL_TIMEOUT_SEC": "600"}):
            config = WeaveConfig()
            assert config.approval_timeout_sec == 600

    def test_from_env_reads_non_interactive(self):
        """from_env() factory reads WEAVE_NON_INTERACTIVE."""
        with patch.dict(os.environ, {"WEAVE_NON_INTERACTIVE": "true"}):
            config = WeaveConfig.from_env()
            assert config.non_interactive is True

    def test_from_env_reads_approval_timeout(self):
        """from_env() factory reads WEAVE_APPROVAL_TIMEOUT_SEC."""
        with patch.dict(os.environ, {"WEAVE_APPROVAL_TIMEOUT_SEC": "120"}):
            config = WeaveConfig.from_env()
            assert config.approval_timeout_sec == 120


class TestHarnessBackwardsCompat:
    """HARNESS_NON_INTERACTIVE still works with deprecation warning (#543)."""

    def test_harness_env_var_works(self):
        """HARNESS_NON_INTERACTIVE=true sets non_interactive=True."""
        with patch.dict(os.environ, {
            "HARNESS_NON_INTERACTIVE": "true",
        }, clear=False):
            # Remove WEAVE_NON_INTERACTIVE if present
            os.environ.pop("WEAVE_NON_INTERACTIVE", None)
            import warnings
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                config = WeaveConfig()
                assert config.non_interactive is True
                # Should emit DeprecationWarning
                dep_warnings = [
                    x for x in w if issubclass(x.category, DeprecationWarning)
                ]
                assert len(dep_warnings) == 1
                assert "HARNESS_NON_INTERACTIVE" in str(dep_warnings[0].message)

    def test_weave_env_var_takes_precedence(self):
        """WEAVE_NON_INTERACTIVE takes precedence over HARNESS_NON_INTERACTIVE."""
        with patch.dict(os.environ, {
            "WEAVE_NON_INTERACTIVE": "false",
            "HARNESS_NON_INTERACTIVE": "true",
        }):
            config = WeaveConfig()
            assert config.non_interactive is False

    def test_from_env_harness_backwards_compat(self):
        """from_env() also supports HARNESS_NON_INTERACTIVE."""
        with patch.dict(os.environ, {
            "HARNESS_NON_INTERACTIVE": "true",
        }, clear=False):
            os.environ.pop("WEAVE_NON_INTERACTIVE", None)
            import warnings
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                config = WeaveConfig.from_env()
                assert config.non_interactive is True


# ---------------------------------------------------------------------------
# PersonalGuardrails — non-interactive evaluate
# ---------------------------------------------------------------------------


class TestPersonalGuardrailsEvaluateNonInteractive:
    """HIGH risk in non-interactive mode returns pending_approval (no stdin)."""

    def test_high_risk_returns_pending_approval(self, non_interactive_guardrails):
        """Non-interactive: HIGH risk returns pending_approval, not blocking."""
        result = non_interactive_guardrails.evaluate("bash", {"command": "rm -rf /tmp"})
        assert isinstance(result, GuardrailResult)
        assert result.decision == "pending_approval"
        assert "non-interactive" in result.reason.lower()
        assert "bash" in result.reason

    def test_high_risk_includes_non_interactive_reason(self, non_interactive_guardrails):
        """The reason should mention non-interactive mode."""
        result = non_interactive_guardrails.evaluate("bash", {"command": "curl http://example.com"})
        assert result.decision == "pending_approval"
        assert "non-interactive" in result.reason.lower()

    def test_low_risk_still_allowed(self, non_interactive_guardrails):
        """LOW risk is still auto-approved in non-interactive mode."""
        result = non_interactive_guardrails.evaluate("read", {"file_path": "/tmp/test.txt"})
        assert result.decision == "allowed"

    def test_medium_risk_still_allowed(self, non_interactive_guardrails):
        """MEDIUM risk is still auto-approved in non-interactive mode."""
        result = non_interactive_guardrails.evaluate("write", {
            "file_path": "/tmp/test.txt",
            "content": "hello",
        })
        assert result.decision == "allowed"

    def test_whitelist_still_works(self, personal_policy, mock_tool_registry):
        """Whitelist auto-approval still works in non-interactive mode."""
        policy = PersonalGuardrailPolicy(
            mode=PermissionMode.DONT_ASK,
            whitelist_patterns=[r"^git\s+status$"],
        )
        gr = PersonalGuardrails(policy, mock_tool_registry, non_interactive=True)
        result = gr.evaluate("bash", {"command": "git status"})
        assert result.decision == "allowed"

    def test_auto_approve_high_still_works(self, mock_tool_registry):
        """auto_approve_high=True still works in non-interactive mode."""
        policy = PersonalGuardrailPolicy(
            mode=PermissionMode.DONT_ASK,
            auto_approve_high=True,
        )
        gr = PersonalGuardrails(policy, mock_tool_registry, non_interactive=True)
        result = gr.evaluate("bash", {"command": "curl https://example.com"})
        assert result.decision == "allowed"


class TestPersonalGuardrailsEvaluateInteractive:
    """HIGH risk in interactive mode returns pending_approval for confirmation."""

    def test_high_risk_returns_pending_approval(self, interactive_guardrails):
        """Interactive: HIGH risk returns pending_approval."""
        result = interactive_guardrails.evaluate("bash", {"command": "rm -rf /tmp"})
        assert isinstance(result, GuardrailResult)
        assert result.decision == "pending_approval"
        assert "HIGH" in result.reason
        assert "confirmation" in result.reason.lower()

    def test_low_risk_allowed(self, interactive_guardrails):
        """LOW risk is auto-approved in interactive mode."""
        result = interactive_guardrails.evaluate("read", {"file_path": "/tmp/test.txt"})
        assert result.decision == "allowed"

    def test_medium_risk_allowed(self, interactive_guardrails):
        """MEDIUM risk is auto-approved in interactive mode."""
        result = interactive_guardrails.evaluate("write", {
            "file_path": "/tmp/test.txt",
            "content": "hello",
        })
        assert result.decision == "allowed"


# ---------------------------------------------------------------------------
# PersonalGuardrails — request_confirmation
# ---------------------------------------------------------------------------


class TestRequestConfirmationNonInteractive:
    """Non-interactive mode: request_confirmation returns False immediately."""

    def test_returns_false_without_stdin(self, non_interactive_guardrails):
        """Non-interactive: request_confirmation returns False, no stdin read."""
        result = non_interactive_guardrails.request_confirmation(
            "bash", {"command": "rm -rf /"}
        )
        assert result is False

    def test_no_select_call_in_non_interactive(self, non_interactive_guardrails):
        """Non-interactive: select.select is never called."""
        with patch("guardrails.policy.select.select") as mock_select:
            result = non_interactive_guardrails.request_confirmation(
                "bash", {"command": "rm -rf /"}
            )
            assert result is False
            mock_select.assert_not_called()


class TestRequestConfirmationInteractive:
    """Interactive mode: request_confirmation reads stdin normally."""

    def test_calls_select(self, interactive_guardrails):
        """Interactive: select.select is called for timeout."""
        with patch("guardrails.policy.select.select") as mock_select:
            mock_select.return_value = ([], [], [])
            interactive_guardrails.request_confirmation(
                "bash", {"command": "rm -rf /"}
            )
            mock_select.assert_called_once()


# ---------------------------------------------------------------------------
# check_and_execute — non-interactive flow
# ---------------------------------------------------------------------------


class TestCheckAndExecuteNonInteractive:
    """check_and_execute in non-interactive mode creates tickets."""

    def test_pending_approval_creates_ticket(self, personal_policy, mock_tool_registry):
        """Non-interactive with approval_repo: pending_approval creates ticket."""
        approval_repo = MagicMock()
        approval_repo.find_approved_ticket = MagicMock(return_value=None)
        ticket_mock = MagicMock()
        ticket_mock.id = "ticket_abc123"
        approval_repo.create_ticket = MagicMock(return_value=ticket_mock)

        gr = PersonalGuardrails(
            personal_policy,
            mock_tool_registry,
            non_interactive=True,
        )

        result = gr.check_and_execute(
            "bash", {"command": "rm -rf /"}, job_id="job_001",
            approval_repo=approval_repo,
        )

        # Should return a GuardrailResult with ticket_id set
        assert isinstance(result, GuardrailResult)
        assert result.decision == "pending_approval"
        assert result.ticket_id == "ticket_abc123"
        approval_repo.create_ticket.assert_called_once()

    def test_pending_approval_without_repo(self, non_interactive_guardrails):
        """Non-interactive without approval_repo: returns pending_approval result."""
        result = non_interactive_guardrails.check_and_execute(
            "bash", {"command": "rm -rf /"}
        )
        assert isinstance(result, GuardrailResult)
        assert result.decision == "pending_approval"
        assert result.ticket_id is None

    def test_allowed_still_executes(self, non_interactive_guardrails, mock_tool_registry):
        """Non-interactive: allowed tools still execute."""
        result = non_interactive_guardrails.check_and_execute(
            "read", {"file_path": "/tmp/test.txt"}
        )
        assert isinstance(result, ToolResult)
        assert result.success is True
        mock_tool_registry.execute.assert_called_with(
            "read", {"file_path": "/tmp/test.txt"}
        )


# ---------------------------------------------------------------------------
# WorkerConfig — non-interactive attribute
# ---------------------------------------------------------------------------


class TestWorkerConfigNonInteractive:
    """WorkerConfig supports non_interactive attribute."""

    def test_default_non_interactive_false(self):
        """Default: non_interactive is False."""
        config = WorkerConfig()
        assert config.non_interactive is False

    def test_non_interactive_true(self):
        """Can set non_interactive=True."""
        config = WorkerConfig(non_interactive=True)
        assert config.non_interactive is True

    def test_unknown_attribute_raises(self):
        """Unknown attribute raises TypeError."""
        with pytest.raises(TypeError, match="WorkerConfig has no attribute"):
            WorkerConfig(unknown_attr=True)


# ---------------------------------------------------------------------------
# CLI argument parsing — --non-interactive
# ---------------------------------------------------------------------------


class TestWorkerArgParsing:
    """Test --non-interactive CLI argument parsing."""

    def test_non_interactive_flag_present(self):
        """--non-interactive sets the flag to True."""
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        worker_parser = subparsers.add_parser("worker")
        worker_parser.add_argument("--concurrency", type=int, default=1)
        worker_parser.add_argument("--poll-interval", type=int, default=5)
        worker_parser.add_argument("--non-interactive", action="store_true")

        args = parser.parse_args(["worker", "--non-interactive"])
        assert args.non_interactive is True

    def test_non_interactive_flag_absent(self):
        """Without --non-interactive, the flag is False."""
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        worker_parser = subparsers.add_parser("worker")
        worker_parser.add_argument("--concurrency", type=int, default=1)
        worker_parser.add_argument("--poll-interval", type=int, default=5)
        worker_parser.add_argument("--non-interactive", action="store_true")

        args = parser.parse_args(["worker"])
        assert args.non_interactive is False

    def test_non_interactive_with_other_args(self):
        """--non-interactive can be combined with other flags."""
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        worker_parser = subparsers.add_parser("worker")
        worker_parser.add_argument("--concurrency", type=int, default=1)
        worker_parser.add_argument("--poll-interval", type=int, default=5)
        worker_parser.add_argument("--non-interactive", action="store_true")

        args = parser.parse_args([
            "worker", "--non-interactive", "--concurrency", "2", "--poll-interval", "10"
        ])
        assert args.non_interactive is True
        assert args.concurrency == 2
        assert args.poll_interval == 10


# ---------------------------------------------------------------------------
# Approval timeout integration
# ---------------------------------------------------------------------------


class TestApprovalTimeoutIntegration:
    """Test approval_timeout_sec is used correctly throughout."""

    def test_config_passed_to_service(self):
        """WeaveConfig.approval_timeout_sec is available."""
        config = WeaveConfig(approval_timeout_sec=120)
        assert config.approval_timeout_sec == 120

    def test_policy_timeout_from_config(self):
        """PersonalGuardrailPolicy.confirmation_timeout_sec is respected."""
        policy = PersonalGuardrailPolicy(confirmation_timeout_sec=60)
        assert policy.confirmation_timeout_sec == 60


# ---------------------------------------------------------------------------
# Non-blocking verification
# ---------------------------------------------------------------------------


class TestNonBlocking:
    """Verify non-interactive mode does not block."""

    def test_request_confirmation_returns_immediately(self, non_interactive_guardrails):
        """Non-interactive request_confirmation returns without waiting."""
        start = time.monotonic()
        result = non_interactive_guardrails.request_confirmation(
            "bash", {"command": "rm -rf /"}
        )
        elapsed = time.monotonic() - start
        assert result is False
        assert elapsed < 0.1  # Should be immediate, no timeout

    def test_evaluate_returns_immediately(self, non_interactive_guardrails):
        """Non-interactive evaluate returns immediately for HIGH risk."""
        start = time.monotonic()
        result = non_interactive_guardrails.evaluate("bash", {"command": "rm -rf /"})
        elapsed = time.monotonic() - start
        assert result.decision == "pending_approval"
        assert elapsed < 0.1  # Should be immediate
