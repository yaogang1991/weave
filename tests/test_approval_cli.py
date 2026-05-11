"""
Tests for approval ticket CLI commands: tickets, approve, reject.

Covers:
- tickets command: list all, filter by status, filter by job_id
- approve command: success, not-found (E3001), already-decided (E3002)
- reject command: success, not-found (E3001), already-decided (E3003)
- State correctness after approve/reject
- Traceability: decided_by, decided_at, reason, previous_status
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

# ------------------------------------------------------------------------------
# Mock heavy LLM dependencies before importing main
# ------------------------------------------------------------------------------

# Create mock modules
_mock_anthropic = MagicMock()
_mock_openai = MagicMock()

sys.modules["anthropic"] = _mock_anthropic
sys.modules["openai"] = _mock_openai

# Now safe to import main
import main as main_module
from control_plane.approval import ApprovalRepository, ApprovalTicket, TicketStatus


# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

def _make_namespace(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


@pytest.fixture
def tmp_approval_repo(tmp_path: Path, monkeypatch) -> ApprovalRepository:
    """ApprovalRepository backed by a temp directory, patched into main module."""
    repo = ApprovalRepository(str(tmp_path / "approvals"))
    monkeypatch.setattr(main_module, "ApprovalRepository", lambda: repo)
    return repo


@pytest.fixture
def sample_ticket(tmp_approval_repo: ApprovalRepository) -> ApprovalTicket:
    """A single pending ticket for tests."""
    return tmp_approval_repo.create_ticket(
        job_id="job_abc",
        tool_name="bash",
        args={"command": "rm -rf /tmp/test"},
        risk_level="high",
    )


@pytest.fixture
def multiple_tickets(tmp_approval_repo: ApprovalRepository) -> list[ApprovalTicket]:
    """Multiple tickets in various states."""
    t1 = tmp_approval_repo.create_ticket(
        job_id="job_abc",
        tool_name="bash",
        args={"command": "echo hello"},
        risk_level="high",
    )
    t2 = tmp_approval_repo.create_ticket(
        job_id="job_def",
        tool_name="write_file",
        args={"path": "/tmp/foo.txt", "content": "bar"},
        risk_level="medium",
    )
    t3 = tmp_approval_repo.create_ticket(
        job_id="job_abc",
        tool_name="delete_file",
        args={"path": "/tmp/old.txt"},
        risk_level="critical",
    )
    # Approve t2, reject t3 to get mixed states
    tmp_approval_repo.approve_ticket(t2.id, reason="Looks safe")
    tmp_approval_repo.reject_ticket(t3.id, reason="Too risky")
    return [t1, t2, t3]


# ------------------------------------------------------------------------------
# cmd_tickets tests
# ------------------------------------------------------------------------------

class TestCmdTickets:

    @pytest.mark.asyncio
    async def test_list_all_tickets(self, multiple_tickets, capsys):
        """tickets command should list all tickets with stats."""
        args = _make_namespace(status=None, job_id=None)
        await main_module.cmd_tickets(args)
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert "tickets" in output
        assert "count" in output
        assert "stats" in output
        assert output["count"] == 3
        # Stats should reflect all statuses
        assert output["stats"]["pending"] == 1
        assert output["stats"]["approved"] == 1
        assert output["stats"]["rejected"] == 1
        assert output["stats"]["expired"] == 0

    @pytest.mark.asyncio
    async def test_filter_by_status_pending(self, multiple_tickets, capsys):
        """tickets --status pending should only return pending tickets."""
        args = _make_namespace(status="pending", job_id=None)
        await main_module.cmd_tickets(args)
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["count"] == 1
        assert output["tickets"][0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_filter_by_status_approved(self, multiple_tickets, capsys):
        """tickets --status approved should only return approved tickets."""
        args = _make_namespace(status="approved", job_id=None)
        await main_module.cmd_tickets(args)
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["count"] == 1
        assert output["tickets"][0]["status"] == "approved"

    @pytest.mark.asyncio
    async def test_filter_by_status_rejected(self, multiple_tickets, capsys):
        """tickets --status rejected should only return rejected tickets."""
        args = _make_namespace(status="rejected", job_id=None)
        await main_module.cmd_tickets(args)
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["count"] == 1
        assert output["tickets"][0]["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_filter_by_job_id(self, multiple_tickets, capsys):
        """tickets --job job_abc should only return tickets for that job."""
        args = _make_namespace(status=None, job_id="job_abc")
        await main_module.cmd_tickets(args)
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["count"] == 2
        for t in output["tickets"]:
            assert t["job_id"] == "job_abc"

    @pytest.mark.asyncio
    async def test_filter_by_job_and_status(self, multiple_tickets, capsys):
        """tickets --job job_abc --status pending should combine filters."""
        args = _make_namespace(status="pending", job_id="job_abc")
        await main_module.cmd_tickets(args)
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["count"] == 1
        assert output["tickets"][0]["job_id"] == "job_abc"
        assert output["tickets"][0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_ticket_fields_shape(self, sample_ticket, capsys):
        """Each ticket in output should have the expected fields."""
        args = _make_namespace(status=None, job_id=None)
        await main_module.cmd_tickets(args)
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        t = output["tickets"][0]
        assert "id" in t
        assert "job_id" in t
        assert "tool_name" in t
        assert "status" in t
        assert "risk_level" in t
        assert "args_preview" in t
        assert "requested_at" in t
        assert "expires_at" in t

    @pytest.mark.asyncio
    async def test_empty_repo(self, tmp_approval_repo, capsys):
        """tickets command on empty repo should return empty list."""
        args = _make_namespace(status=None, job_id=None)
        await main_module.cmd_tickets(args)
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["tickets"] == []
        assert output["count"] == 0
        assert output["stats"] == {"pending": 0, "approved": 0, "consumed": 0, "rejected": 0, "expired": 0}


# ------------------------------------------------------------------------------
# cmd_approve tests
# ------------------------------------------------------------------------------

class TestCmdApprove:

    @pytest.mark.asyncio
    async def test_approve_success(self, sample_ticket, capsys):
        """approve <ticket_id> should approve a pending ticket."""
        args = _make_namespace(ticket_id=sample_ticket.id, reason="Safe command, reviewed")
        await main_module.cmd_approve(args)
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["ticket_id"] == sample_ticket.id
        assert output["status"] == "approved"
        assert output["previous_status"] == "pending"
        assert output["decided_by"] == "user"
        assert output["reason"] == "Safe command, reviewed"
        assert output["message"] == "Ticket approved"
        assert output["decided_at"] is not None

    @pytest.mark.asyncio
    async def test_approve_no_reason(self, sample_ticket, capsys):
        """approve without --reason should default to empty string."""
        args = _make_namespace(ticket_id=sample_ticket.id, reason="")
        await main_module.cmd_approve(args)
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["status"] == "approved"
        assert output["reason"] == ""

    @pytest.mark.asyncio
    async def test_approve_not_found(self, tmp_approval_repo, capsys):
        """approve on non-existent ticket should print E3001 to stderr and exit."""
        args = _make_namespace(ticket_id="ticket_nonexistent", reason="")
        with pytest.raises(SystemExit) as exc_info:
            await main_module.cmd_approve(args)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        err = json.loads(captured.err)
        assert err["code"] == "E3001"
        assert "not found" in err["error"]

    @pytest.mark.asyncio
    async def test_approve_already_approved(self, tmp_approval_repo, sample_ticket, capsys):
        """approve on already-approved ticket should print E3002 to stderr and exit."""
        tmp_approval_repo.approve_ticket(sample_ticket.id, reason="Already done")
        args = _make_namespace(ticket_id=sample_ticket.id, reason="")
        with pytest.raises(SystemExit) as exc_info:
            await main_module.cmd_approve(args)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        err = json.loads(captured.err)
        assert err["code"] == "E3002"

    @pytest.mark.asyncio
    async def test_approve_already_rejected(self, tmp_approval_repo, sample_ticket, capsys):
        """approve on already-rejected ticket should print E3002 to stderr and exit."""
        tmp_approval_repo.reject_ticket(sample_ticket.id, reason="Already rejected")
        args = _make_namespace(ticket_id=sample_ticket.id, reason="")
        with pytest.raises(SystemExit) as exc_info:
            await main_module.cmd_approve(args)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        err = json.loads(captured.err)
        assert err["code"] == "E3002"

    @pytest.mark.asyncio
    async def test_approve_state_correctness(self, tmp_approval_repo, sample_ticket, capsys):
        """After approve, ticket status should be approved in repository."""
        args = _make_namespace(ticket_id=sample_ticket.id, reason="Reviewed")
        await main_module.cmd_approve(args)

        ticket = tmp_approval_repo.get_ticket(sample_ticket.id)
        assert ticket is not None
        assert ticket.status == TicketStatus.APPROVED

    @pytest.mark.asyncio
    async def test_approve_traceability(self, tmp_approval_repo, sample_ticket, capsys):
        """Approved ticket should have decided_by, decided_at, and reason set."""
        before = datetime.now(timezone.utc)
        args = _make_namespace(ticket_id=sample_ticket.id, reason="Traceable approval")
        await main_module.cmd_approve(args)
        after = datetime.now(timezone.utc)

        ticket = tmp_approval_repo.get_ticket(sample_ticket.id)
        assert ticket.decided_by == "user"
        assert ticket.reason == "Traceable approval"
        assert ticket.decided_at is not None
        decided = datetime.fromisoformat(ticket.decided_at.isoformat().replace("Z", "+00:00"))
        assert before <= decided <= after


# ------------------------------------------------------------------------------
# cmd_reject tests
# ------------------------------------------------------------------------------

class TestCmdReject:

    @pytest.mark.asyncio
    async def test_reject_success(self, sample_ticket, capsys):
        """reject <ticket_id> should reject a pending ticket."""
        args = _make_namespace(ticket_id=sample_ticket.id, reason="Too risky, avoid rm -rf")
        await main_module.cmd_reject(args)
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["ticket_id"] == sample_ticket.id
        assert output["status"] == "rejected"
        assert output["previous_status"] == "pending"
        assert output["decided_by"] == "user"
        assert output["reason"] == "Too risky, avoid rm -rf"
        assert output["message"] == "Ticket rejected"
        assert output["decided_at"] is not None

    @pytest.mark.asyncio
    async def test_reject_no_reason(self, sample_ticket, capsys):
        """reject without --reason should default to empty string."""
        args = _make_namespace(ticket_id=sample_ticket.id, reason="")
        await main_module.cmd_reject(args)
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["status"] == "rejected"
        assert output["reason"] == ""

    @pytest.mark.asyncio
    async def test_reject_not_found(self, tmp_approval_repo, capsys):
        """reject on non-existent ticket should print E3001 to stderr and exit."""
        args = _make_namespace(ticket_id="ticket_nonexistent", reason="")
        with pytest.raises(SystemExit) as exc_info:
            await main_module.cmd_reject(args)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        err = json.loads(captured.err)
        assert err["code"] == "E3001"
        assert "not found" in err["error"]

    @pytest.mark.asyncio
    async def test_reject_already_approved(self, tmp_approval_repo, sample_ticket, capsys):
        """reject on already-approved ticket should print E3003 to stderr and exit."""
        tmp_approval_repo.approve_ticket(sample_ticket.id, reason="Already done")
        args = _make_namespace(ticket_id=sample_ticket.id, reason="")
        with pytest.raises(SystemExit) as exc_info:
            await main_module.cmd_reject(args)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        err = json.loads(captured.err)
        assert err["code"] == "E3003"

    @pytest.mark.asyncio
    async def test_reject_already_rejected(self, tmp_approval_repo, sample_ticket, capsys):
        """reject on already-rejected ticket should print E3003 to stderr and exit."""
        tmp_approval_repo.reject_ticket(sample_ticket.id, reason="Already rejected")
        args = _make_namespace(ticket_id=sample_ticket.id, reason="")
        with pytest.raises(SystemExit) as exc_info:
            await main_module.cmd_reject(args)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        err = json.loads(captured.err)
        assert err["code"] == "E3003"

    @pytest.mark.asyncio
    async def test_reject_state_correctness(self, tmp_approval_repo, sample_ticket, capsys):
        """After reject, ticket status should be rejected in repository."""
        args = _make_namespace(ticket_id=sample_ticket.id, reason="Reviewed")
        await main_module.cmd_reject(args)

        ticket = tmp_approval_repo.get_ticket(sample_ticket.id)
        assert ticket is not None
        assert ticket.status == TicketStatus.REJECTED

    @pytest.mark.asyncio
    async def test_reject_traceability(self, tmp_approval_repo, sample_ticket, capsys):
        """Rejected ticket should have decided_by, decided_at, and reason set."""
        before = datetime.now(timezone.utc)
        args = _make_namespace(ticket_id=sample_ticket.id, reason="Traceable rejection")
        await main_module.cmd_reject(args)
        after = datetime.now(timezone.utc)

        ticket = tmp_approval_repo.get_ticket(sample_ticket.id)
        assert ticket.decided_by == "user"
        assert ticket.reason == "Traceable rejection"
        assert ticket.decided_at is not None
        decided = datetime.fromisoformat(ticket.decided_at.isoformat().replace("Z", "+00:00"))
        assert before <= decided <= after

    @pytest.mark.asyncio
    async def test_reject_succeeds_when_job_missing(self, tmp_approval_repo, sample_ticket, capsys, monkeypatch):
        """Ticket rejection should succeed even if abort path reports missing job."""
        class _RunServiceStub:
            async def abort_after_rejection(self, job_id: str, ticket_id: str, reason: str = "") -> None:
                raise ValueError(f"Job {job_id} not found")

        monkeypatch.setattr(main_module, "_make_run_service", lambda *args, **kwargs: _RunServiceStub())
        args = _make_namespace(ticket_id=sample_ticket.id, reason="Clear pending ticket only")
        await main_module.cmd_reject(args)
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["status"] == "rejected"
        assert output["ticket_id"] == sample_ticket.id


# ------------------------------------------------------------------------------
# End-to-end flow: approve + reject interleaved
# ------------------------------------------------------------------------------

class TestApproveRejectFlow:

    @pytest.mark.asyncio
    async def test_multiple_tickets_independent(self, tmp_approval_repo, multiple_tickets, capsys):
        """Approving one ticket should not affect others."""
        t1, t2, t3 = multiple_tickets  # t1=pending, t2=approved, t3=rejected

        # Approve t1
        args = _make_namespace(ticket_id=t1.id, reason="t1 approved")
        await main_module.cmd_approve(args)

        assert tmp_approval_repo.get_ticket(t1.id).status == TicketStatus.APPROVED
        assert tmp_approval_repo.get_ticket(t2.id).status == TicketStatus.APPROVED
        assert tmp_approval_repo.get_ticket(t3.id).status == TicketStatus.REJECTED

    @pytest.mark.asyncio
    async def test_stats_after_approve(self, tmp_approval_repo, sample_ticket, capsys):
        """Stats should update correctly after approval."""
        args_approve = _make_namespace(ticket_id=sample_ticket.id, reason="")
        await main_module.cmd_approve(args_approve)
        # Consume the approve output
        capsys.readouterr()

        args_tickets = _make_namespace(status=None, job_id=None)
        await main_module.cmd_tickets(args_tickets)
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["stats"]["approved"] == 1
        assert output["stats"]["pending"] == 0
