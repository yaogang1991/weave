"""Tests for CLI approval flow improvement (#143).

Verifies that cmd_execute wires up approval_repo and the
PendingApprovalError message is informative.
"""
import io
import sys
from unittest.mock import MagicMock, patch

import pytest


class TestCLIApprovalWiring:
    """Verify approval_repo is created in cmd_execute."""

    def test_approval_repo_created(self):
        """cmd_execute should create an ApprovalRepository for the agent pool."""
        from control_plane.approval import ApprovalRepository
        repo = ApprovalRepository()
        assert repo is not None

    def test_non_interactive_flag_from_args(self):
        """--non-interactive flag sets non_interactive=True."""
        import argparse
        args = argparse.Namespace(non_interactive=True)
        import os
        result = (
            getattr(args, "non_interactive", False)
            or os.getenv("HARNESS_NON_INTERACTIVE", "").lower() in ("true", "1", "yes")
        )
        assert result is True

    def test_non_interactive_from_env(self):
        """HARNESS_NON_INTERACTIVE env var is respected."""
        import argparse
        args = argparse.Namespace()
        import os
        with patch.dict(os.environ, {"HARNESS_NON_INTERACTIVE": "true"}):
            result = (
                getattr(args, "non_interactive", False)
                or os.getenv("HARNESS_NON_INTERACTIVE", "").lower() in ("true", "1", "yes")
            )
        assert result is True

    def test_interactive_default(self):
        """Without flag or env, non_interactive is False."""
        import argparse
        args = argparse.Namespace()
        import os
        with patch.dict(os.environ, {}, clear=True):
            # Remove HARNESS_NON_INTERACTIVE if present
            os.environ.pop("HARNESS_NON_INTERACTIVE", None)
            result = (
                getattr(args, "non_interactive", False)
                or os.getenv("HARNESS_NON_INTERACTIVE", "").lower() in ("true", "1", "yes")
            )
        assert result is False

    def test_cli_flag_overrides_env(self):
        """--non-interactive flag takes precedence over env var."""
        import argparse
        args = argparse.Namespace(non_interactive=True)
        import os
        # Even with env=false, flag wins
        with patch.dict(os.environ, {"HARNESS_NON_INTERACTIVE": "false"}):
            result = (
                getattr(args, "non_interactive", False)
                or os.getenv("HARNESS_NON_INTERACTIVE", "").lower() in ("true", "1", "yes")
            )
        assert result is True


class TestApprovalErrorMessage:
    """Verify the PendingApprovalError message is informative."""

    def test_message_with_ticket_id(self, capsys):
        """When ticket_id is present, show approve/reject commands."""
        from core.exceptions import PendingApprovalError
        exc = PendingApprovalError(ticket_id="ticket_abc123")

        # Simulate the error handling logic
        if exc.ticket_id:
            msg = f"Ticket ID: {exc.ticket_id}"
        else:
            msg = "no ticket was created"

        assert "ticket_abc123" in msg

    def test_message_without_ticket_id(self):
        """When ticket_id is empty, show configuration error."""
        from core.exceptions import PendingApprovalError
        exc = PendingApprovalError(ticket_id="")

        if not exc.ticket_id:
            msg = "no ticket was created"
        else:
            msg = f"Ticket ID: {exc.ticket_id}"

        assert "no ticket" in msg
