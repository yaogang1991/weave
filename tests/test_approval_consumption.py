"""Tests for ApprovalTicket consumed semantics (#132 P1-1).

Verifies that consuming an approved ticket:
- Does NOT modify the reason field
- Writes consumed_by_run_id and consumed_by_node_id
"""

from control_plane.approval import ApprovalRepository, TicketStatus


class TestApprovalConsumptionSemantics:
    """Verify structured consumption without reason pollution."""

    def test_reason_not_modified_on_consumption(self):
        """Consuming a ticket must not append '[consumed on execution]' to reason."""
        repo = ApprovalRepository()
        ticket = repo.create_ticket(
            job_id="job_1",
            tool_name="bash",
            args={"command": "rm -rf /"},
            risk_level="critical",
        )
        approved = repo.approve_ticket(ticket.id, reason="LGTM, safe to proceed")
        original_reason = approved.reason

        repo.consume_ticket(approved, run_id="run_1", node_id="node_1")

        assert approved.reason == original_reason
        assert "[consumed on execution]" not in approved.reason

    def test_consumed_by_fields_written(self):
        """consumed_by_run_id and consumed_by_node_id are set on consumption."""
        repo = ApprovalRepository()
        ticket = repo.create_ticket(
            job_id="job_2",
            tool_name="write",
            args={"file_path": "/etc/config"},
            risk_level="high",
        )
        approved = repo.approve_ticket(ticket.id, reason="ok")

        repo.consume_ticket(approved, run_id="run_abc", node_id="node_gen")

        assert approved.consumed_by_run_id == "run_abc"
        assert approved.consumed_by_node_id == "node_gen"

    def test_consumed_status_transition(self):
        """Ticket transitions from APPROVED to CONSUMED."""
        repo = ApprovalRepository()
        ticket = repo.create_ticket(
            job_id="job_3",
            tool_name="bash",
            args={"command": "deploy"},
            risk_level="critical",
        )
        approved = repo.approve_ticket(ticket.id, reason="go ahead")

        assert approved.status == TicketStatus.APPROVED

        repo.consume_ticket(approved)

        assert approved.status == TicketStatus.CONSUMED
        assert approved.consumed_at is not None

    def test_consumed_ticket_not_reusable(self):
        """Consumed tickets are not found by find_approved_ticket."""
        repo = ApprovalRepository()
        ticket = repo.create_ticket(
            job_id="job_4",
            tool_name="bash",
            args={"command": "test"},
            risk_level="high",
        )
        approved = repo.approve_ticket(ticket.id, reason="yes")

        repo.consume_ticket(approved)

        # Should NOT find the consumed ticket
        found = repo.find_approved_ticket(
            job_id="job_4",
            tool_name="bash",
            args={"command": "test"},
        )
        assert found is None

    def test_reason_preserves_original_approval_reason(self):
        """Multiple consume calls don't accumulate garbage in reason."""
        repo = ApprovalRepository()
        ticket = repo.create_ticket(
            job_id="job_5",
            tool_name="edit",
            args={"file_path": "src/main.py"},
            risk_level="medium",
        )
        approved = repo.approve_ticket(ticket.id, reason="Approved by admin")

        repo.consume_ticket(approved, run_id="run_x", node_id="node_y")

        # Reason should still be exactly what the approver wrote
        assert approved.reason == "Approved by admin"
