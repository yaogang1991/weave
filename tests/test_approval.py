"""
Tests for control_plane/approval.py — ApprovalTicket model and ApprovalRepository.

Covers:
- TicketStatus enum values and comparison
- ApprovalTicket model instantiation and validation
- ApprovalRepository.create_ticket (ID generation, args_hash, expires_at)
- ApprovalRepository.get_ticket round-trip
- ApprovalRepository.list_tickets (status filter, job_id filter)
- ApprovalRepository.approve_ticket (state transition, timestamps)
- ApprovalRepository.reject_ticket (state transition, timestamps)
- ApprovalRepository.expire_tickets (timeout auto-expiry)
- ApprovalRepository.get_pending_for_job (job-scoped queries)
- ApprovalRepository.get_stats (aggregated counts)
- args_hash tamper verification
- Atomic write guarantees (no intermediate .tmp files left behind)
- Error handling for non-existent tickets and invalid state transitions
"""

from __future__ import annotations

import json
import time
from datetime import timedelta
from pathlib import Path

import pytest

from control_plane.approval import (
    ApprovalTicket,
    ApprovalRepository,
    TicketStatus,
    _compute_args_hash,
    _make_args_preview,
    _utc_now,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def tmp_approval_repo(tmp_path: Path) -> ApprovalRepository:
    """An ApprovalRepository backed by a temporary directory."""
    return ApprovalRepository(str(tmp_path / "approvals"))


@pytest.fixture
def sample_args() -> dict:
    """Sample tool arguments for ticket creation."""
    return {"command": "rm -rf /", "target": "production", "force": True}


# =============================================================================
# TicketStatus enum
# =============================================================================


class TestTicketStatus:
    def test_status_values(self):
        assert TicketStatus.PENDING == "pending"
        assert TicketStatus.APPROVED == "approved"
        assert TicketStatus.REJECTED == "rejected"
        assert TicketStatus.EXPIRED == "expired"

    def test_status_membership(self):
        assert TicketStatus.PENDING in {
            TicketStatus.PENDING,
            TicketStatus.APPROVED,
            TicketStatus.REJECTED,
            TicketStatus.EXPIRED,
        }


# =============================================================================
# ApprovalTicket model
# =============================================================================


class TestApprovalTicketModel:
    def test_defaults(self):
        now = _utc_now()
        ticket = ApprovalTicket(
            id="ticket_test_001",
            job_id="job_001",
            tool_name="dangerous_tool",
            args_hash="abcd1234",
            args_preview='{"cmd": "test"}',
            risk_level="high",
            requested_at=now,
            created_at=now,
            updated_at=now,
        )
        assert ticket.status == TicketStatus.PENDING
        assert ticket.decided_at is None
        assert ticket.decided_by is None
        assert ticket.reason == ""
        assert ticket.run_id is None
        assert ticket.node_id is None
        assert ticket.expires_at is None

    def test_is_terminal(self):
        now = _utc_now()
        for status in (TicketStatus.APPROVED, TicketStatus.REJECTED, TicketStatus.EXPIRED):
            ticket = ApprovalTicket(
                id="t1", job_id="j1", tool_name="t", args_hash="h",
                args_preview="p", risk_level="high", status=status,
                requested_at=now, created_at=now, updated_at=now,
            )
            assert ticket.is_terminal()

    def test_is_pending(self):
        now = _utc_now()
        ticket = ApprovalTicket(
            id="t1", job_id="j1", tool_name="t", args_hash="h",
            args_preview="p", risk_level="high", status=TicketStatus.PENDING,
            requested_at=now, created_at=now, updated_at=now,
        )
        assert ticket.is_pending()
        assert not ticket.is_terminal()

    def test_risk_level_validation(self):
        now = _utc_now()
        # Valid levels
        for level in ("low", "medium", "high", "critical"):
            ticket = ApprovalTicket(
                id="t1", job_id="j1", tool_name="t", args_hash="h",
                args_preview="p", risk_level=level,
                requested_at=now, created_at=now, updated_at=now,
            )
            assert ticket.risk_level == level

        # Invalid level
        with pytest.raises(ValueError, match="Invalid risk_level"):
            ApprovalTicket(
                id="t1", job_id="j1", tool_name="t", args_hash="h",
                args_preview="p", risk_level="extreme",
                requested_at=now, created_at=now, updated_at=now,
            )

    def test_invalid_status_validation(self):
        now = _utc_now()
        with pytest.raises(ValueError, match="Invalid TicketStatus"):
            ApprovalTicket(
                id="t1", job_id="j1", tool_name="t", args_hash="h",
                args_preview="p", risk_level="high", status="invalid_status",
                requested_at=now, created_at=now, updated_at=now,
            )

    def test_model_dump_roundtrip(self):
        now = _utc_now()
        ticket = ApprovalTicket(
            id="ticket_abc123",
            job_id="job_001",
            run_id="run_001",
            node_id="node_001",
            tool_name="file_delete",
            args_hash="hash1234",
            args_preview='{"path": "/tmp"}',
            risk_level="critical",
            status=TicketStatus.PENDING,
            requested_at=now,
            decided_at=None,
            decided_by=None,
            reason="",
            expires_at=now + timedelta(seconds=300),
            created_at=now,
            updated_at=now,
        )
        dumped = ticket.model_dump(mode="json")
        restored = ApprovalTicket(**dumped)
        assert restored.id == ticket.id
        assert restored.job_id == ticket.job_id
        assert restored.status == ticket.status
        assert restored.risk_level == ticket.risk_level


# =============================================================================
# Helper functions
# =============================================================================


class TestHelperFunctions:
    def test_compute_args_hash_deterministic(self):
        args = {"b": 2, "a": 1}
        h1 = _compute_args_hash(args)
        h2 = _compute_args_hash(args)
        assert h1 == h2
        assert len(h1) == 16

    def test_compute_args_hash_order_independent(self):
        """Hash must be the same regardless of key insertion order."""
        h1 = _compute_args_hash({"a": 1, "b": 2})
        h2 = _compute_args_hash({"b": 2, "a": 1})
        assert h1 == h2

    def test_compute_args_hash_different_args(self):
        h1 = _compute_args_hash({"a": 1})
        h2 = _compute_args_hash({"a": 2})
        assert h1 != h2

    def test_make_args_preview_truncate(self):
        long_args = {"key": "x" * 500}
        preview = _make_args_preview(long_args, max_len=200)
        assert len(preview) <= 203  # 200 + "..."
        assert preview.endswith("...")

    def test_make_args_preview_short(self):
        args = {"cmd": "test"}
        preview = _make_args_preview(args)
        assert preview == '{"cmd": "test"}'


# =============================================================================
# ApprovalRepository.create_ticket
# =============================================================================


class TestCreateTicket:
    def test_create_ticket_defaults(self, tmp_approval_repo: ApprovalRepository):
        ticket = tmp_approval_repo.create_ticket(
            job_id="job_001",
            tool_name="file_delete",
            args={"path": "/tmp/test"},
        )
        assert ticket.id.startswith("ticket_")
        assert ticket.job_id == "job_001"
        assert ticket.tool_name == "file_delete"
        assert ticket.status == TicketStatus.PENDING
        assert ticket.risk_level == "high"
        assert ticket.run_id is None
        assert ticket.node_id is None
        assert ticket.decided_at is None
        assert ticket.decided_by is None
        assert ticket.reason == ""
        assert ticket.expires_at is not None

    def test_create_ticket_with_optional_fields(self, tmp_approval_repo: ApprovalRepository):
        ticket = tmp_approval_repo.create_ticket(
            job_id="job_002",
            tool_name="db_migrate",
            args={"direction": "up"},
            risk_level="critical",
            run_id="run_001",
            node_id="node_005",
            timeout_sec=600,
        )
        assert ticket.risk_level == "critical"
        assert ticket.run_id == "run_001"
        assert ticket.node_id == "node_005"
        assert ticket.expires_at is not None

    def test_create_ticket_generates_unique_ids(self, tmp_approval_repo: ApprovalRepository):
        t1 = tmp_approval_repo.create_ticket("job_1", "tool", {})
        t2 = tmp_approval_repo.create_ticket("job_1", "tool", {})
        assert t1.id != t2.id

    def test_create_ticket_computes_args_hash(self, tmp_approval_repo: ApprovalRepository):
        args = {"cmd": "rm", "target": "/data"}
        ticket = tmp_approval_repo.create_ticket("job_1", "tool", args)
        expected_hash = _compute_args_hash(args)
        assert ticket.args_hash == expected_hash
        assert len(ticket.args_hash) == 16

    def test_create_ticket_computes_args_preview(self, tmp_approval_repo: ApprovalRepository):
        args = {"command": "deploy", "env": "production"}
        ticket = tmp_approval_repo.create_ticket("job_1", "tool", args)
        expected_preview = _make_args_preview(args)
        assert ticket.args_preview == expected_preview

    def test_create_ticket_sets_expires_at(self, tmp_approval_repo: ApprovalRepository):
        before = _utc_now()
        ticket = tmp_approval_repo.create_ticket(
            "job_1", "tool", {}, timeout_sec=120
        )
        after = _utc_now()
        assert ticket.expires_at is not None
        assert before + timedelta(seconds=120) <= ticket.expires_at <= after + timedelta(seconds=120)

    def test_create_ticket_persists_file(self, tmp_approval_repo: ApprovalRepository, tmp_path: Path):
        ticket = tmp_approval_repo.create_ticket("job_1", "tool", {})
        file_path = tmp_path / "approvals" / f"{ticket.id}.json"
        assert file_path.exists()


# =============================================================================
# ApprovalRepository.get_ticket
# =============================================================================


class TestGetTicket:
    def test_get_ticket_existing(self, tmp_approval_repo: ApprovalRepository):
        created = tmp_approval_repo.create_ticket("job_1", "tool", {})
        fetched = tmp_approval_repo.get_ticket(created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.job_id == created.job_id
        assert fetched.status == created.status

    def test_get_ticket_nonexistent(self, tmp_approval_repo: ApprovalRepository):
        assert tmp_approval_repo.get_ticket("ticket_does_not_exist") is None

    def test_get_ticket_roundtrip_fields(self, tmp_approval_repo: ApprovalRepository):
        created = tmp_approval_repo.create_ticket(
            job_id="job_rt",
            tool_name="db_reset",
            args={"confirm": True, "table": "users"},
            risk_level="critical",
            run_id="run_rt",
            node_id="node_rt",
            timeout_sec=60,
        )
        fetched = tmp_approval_repo.get_ticket(created.id)
        assert fetched is not None
        assert fetched.tool_name == "db_reset"
        assert fetched.risk_level == "critical"
        assert fetched.run_id == "run_rt"
        assert fetched.node_id == "node_rt"
        assert fetched.args_hash == created.args_hash
        assert fetched.args_preview == created.args_preview
        assert fetched.expires_at is not None


# =============================================================================
# ApprovalRepository.list_tickets
# =============================================================================


class TestListTickets:
    def test_list_all(self, tmp_approval_repo: ApprovalRepository):
        t1 = tmp_approval_repo.create_ticket("job_1", "tool_a", {})
        t2 = tmp_approval_repo.create_ticket("job_2", "tool_b", {})
        all_tickets = tmp_approval_repo.list_tickets()
        assert len(all_tickets) == 2
        ids = {t.id for t in all_tickets}
        assert ids == {t1.id, t2.id}

    def test_list_by_status(self, tmp_approval_repo: ApprovalRepository):
        t1 = tmp_approval_repo.create_ticket("job_1", "tool_a", {})
        t2 = tmp_approval_repo.create_ticket("job_2", "tool_b", {})
        tmp_approval_repo.approve_ticket(t1.id)

        pending = tmp_approval_repo.list_tickets(status=TicketStatus.PENDING)
        approved = tmp_approval_repo.list_tickets(status=TicketStatus.APPROVED)

        assert len(pending) == 1
        assert pending[0].id == t2.id
        assert len(approved) == 1
        assert approved[0].id == t1.id

    def test_list_by_job_id(self, tmp_approval_repo: ApprovalRepository):
        t1 = tmp_approval_repo.create_ticket("job_A", "tool", {})
        tmp_approval_repo.create_ticket("job_B", "tool", {})  # side effect: creates ticket
        t3 = tmp_approval_repo.create_ticket("job_A", "tool", {})

        job_a_tickets = tmp_approval_repo.list_tickets(job_id="job_A")
        assert len(job_a_tickets) == 2
        ids = {t.id for t in job_a_tickets}
        assert ids == {t1.id, t3.id}

    def test_list_by_status_and_job_id(self, tmp_approval_repo: ApprovalRepository):
        t1 = tmp_approval_repo.create_ticket("job_A", "tool", {})
        t2 = tmp_approval_repo.create_ticket("job_A", "tool", {})
        tmp_approval_repo.approve_ticket(t1.id)

        result = tmp_approval_repo.list_tickets(
            status=TicketStatus.PENDING, job_id="job_A"
        )
        assert len(result) == 1
        assert result[0].id == t2.id

    def test_list_empty(self, tmp_approval_repo: ApprovalRepository):
        assert tmp_approval_repo.list_tickets() == []

    def test_list_returns_sorted_by_created_at(self, tmp_approval_repo: ApprovalRepository):
        import time
        t1 = tmp_approval_repo.create_ticket("job_1", "tool", {})
        time.sleep(0.01)
        t2 = tmp_approval_repo.create_ticket("job_1", "tool", {})
        time.sleep(0.01)
        t3 = tmp_approval_repo.create_ticket("job_1", "tool", {})

        tickets = tmp_approval_repo.list_tickets()
        assert tickets[0].id == t1.id
        assert tickets[1].id == t2.id
        assert tickets[2].id == t3.id


# =============================================================================
# ApprovalRepository.approve_ticket
# =============================================================================


class TestApproveTicket:
    def test_approve_pending(self, tmp_approval_repo: ApprovalRepository):
        ticket = tmp_approval_repo.create_ticket("job_1", "tool", {})
        approved = tmp_approval_repo.approve_ticket(ticket.id, reason="Looks safe")

        assert approved.status == TicketStatus.APPROVED
        assert approved.decided_at is not None
        assert approved.decided_by == "user"
        assert approved.reason == "Looks safe"

    def test_approve_persists(self, tmp_approval_repo: ApprovalRepository):
        ticket = tmp_approval_repo.create_ticket("job_1", "tool", {})
        tmp_approval_repo.approve_ticket(ticket.id)
        fetched = tmp_approval_repo.get_ticket(ticket.id)
        assert fetched is not None
        assert fetched.status == TicketStatus.APPROVED

    def test_approve_nonexistent(self, tmp_approval_repo: ApprovalRepository):
        with pytest.raises(ValueError, match="Ticket not found"):
            tmp_approval_repo.approve_ticket("ticket_does_not_exist")

    def test_approve_already_approved(self, tmp_approval_repo: ApprovalRepository):
        ticket = tmp_approval_repo.create_ticket("job_1", "tool", {})
        tmp_approval_repo.approve_ticket(ticket.id)
        with pytest.raises(ValueError, match="Cannot approve"):
            tmp_approval_repo.approve_ticket(ticket.id)

    def test_approve_already_rejected(self, tmp_approval_repo: ApprovalRepository):
        ticket = tmp_approval_repo.create_ticket("job_1", "tool", {})
        tmp_approval_repo.reject_ticket(ticket.id)
        with pytest.raises(ValueError, match="Cannot approve"):
            tmp_approval_repo.approve_ticket(ticket.id)

    def test_approve_with_custom_decided_by(self, tmp_approval_repo: ApprovalRepository):
        ticket = tmp_approval_repo.create_ticket("job_1", "tool", {})
        approved = tmp_approval_repo.approve_ticket(
            ticket.id, reason="Auto-approved", decided_by="auto"
        )
        assert approved.decided_by == "auto"
        assert approved.reason == "Auto-approved"

    def test_approve_updates_updated_at(self, tmp_approval_repo: ApprovalRepository):
        ticket = tmp_approval_repo.create_ticket("job_1", "tool", {})
        original_updated = ticket.updated_at
        time.sleep(0.01)
        approved = tmp_approval_repo.approve_ticket(ticket.id)
        assert approved.updated_at > original_updated


# =============================================================================
# ApprovalRepository.reject_ticket
# =============================================================================


class TestRejectTicket:
    def test_reject_pending(self, tmp_approval_repo: ApprovalRepository):
        ticket = tmp_approval_repo.create_ticket("job_1", "tool", {})
        rejected = tmp_approval_repo.reject_ticket(ticket.id, reason="Too risky")

        assert rejected.status == TicketStatus.REJECTED
        assert rejected.decided_at is not None
        assert rejected.decided_by == "user"
        assert rejected.reason == "Too risky"

    def test_reject_persists(self, tmp_approval_repo: ApprovalRepository):
        ticket = tmp_approval_repo.create_ticket("job_1", "tool", {})
        tmp_approval_repo.reject_ticket(ticket.id)
        fetched = tmp_approval_repo.get_ticket(ticket.id)
        assert fetched is not None
        assert fetched.status == TicketStatus.REJECTED

    def test_reject_nonexistent(self, tmp_approval_repo: ApprovalRepository):
        with pytest.raises(ValueError, match="Ticket not found"):
            tmp_approval_repo.reject_ticket("ticket_does_not_exist")

    def test_reject_already_approved(self, tmp_approval_repo: ApprovalRepository):
        ticket = tmp_approval_repo.create_ticket("job_1", "tool", {})
        tmp_approval_repo.approve_ticket(ticket.id)
        with pytest.raises(ValueError, match="Cannot reject"):
            tmp_approval_repo.reject_ticket(ticket.id)

    def test_reject_with_auto_decider(self, tmp_approval_repo: ApprovalRepository):
        ticket = tmp_approval_repo.create_ticket("job_1", "tool", {})
        rejected = tmp_approval_repo.reject_ticket(
            ticket.id, reason="Policy violation", decided_by="auto"
        )
        assert rejected.decided_by == "auto"


# =============================================================================
# ApprovalRepository.expire_tickets
# =============================================================================


class TestExpireTickets:
    def test_expire_overdue_tickets(self, tmp_approval_repo: ApprovalRepository):
        # Create a ticket with a very short timeout
        ticket = tmp_approval_repo.create_ticket(
            "job_1", "tool", {}, timeout_sec=1
        )
        # Wait for it to expire
        time.sleep(1.5)
        expired = tmp_approval_repo.expire_tickets()
        assert len(expired) == 1
        assert expired[0].id == ticket.id
        assert expired[0].status == TicketStatus.EXPIRED
        assert expired[0].decided_by == "timeout"
        assert "expired" in expired[0].reason.lower()

    def test_expire_persists(self, tmp_approval_repo: ApprovalRepository):
        ticket = tmp_approval_repo.create_ticket(
            "job_1", "tool", {}, timeout_sec=1
        )
        time.sleep(1.5)
        tmp_approval_repo.expire_tickets()
        fetched = tmp_approval_repo.get_ticket(ticket.id)
        assert fetched is not None
        assert fetched.status == TicketStatus.EXPIRED

    def test_expire_does_not_affect_fresh_tickets(self, tmp_approval_repo: ApprovalRepository):
        ticket = tmp_approval_repo.create_ticket(
            "job_1", "tool", {}, timeout_sec=300
        )
        expired = tmp_approval_repo.expire_tickets()
        assert len(expired) == 0
        fetched = tmp_approval_repo.get_ticket(ticket.id)
        assert fetched is not None
        assert fetched.status == TicketStatus.PENDING

    def test_expire_does_not_affect_already_decided(self, tmp_approval_repo: ApprovalRepository):
        t1 = tmp_approval_repo.create_ticket("job_1", "tool", {}, timeout_sec=1)
        t2 = tmp_approval_repo.create_ticket("job_2", "tool", {}, timeout_sec=1)
        tmp_approval_repo.approve_ticket(t1.id)
        time.sleep(1.5)
        expired = tmp_approval_repo.expire_tickets()
        assert len(expired) == 1
        assert expired[0].id == t2.id

    def test_expire_returns_empty_when_none_pending(self, tmp_approval_repo: ApprovalRepository):
        expired = tmp_approval_repo.expire_tickets()
        assert expired == []


# =============================================================================
# ApprovalRepository.get_pending_for_job
# =============================================================================


class TestGetPendingForJob:
    def test_get_pending(self, tmp_approval_repo: ApprovalRepository):
        t1 = tmp_approval_repo.create_ticket("job_A", "tool", {})
        t2 = tmp_approval_repo.create_ticket("job_A", "tool", {})
        tmp_approval_repo.create_ticket("job_B", "tool", {})

        pending = tmp_approval_repo.get_pending_for_job("job_A")
        assert len(pending) == 2
        ids = {t.id for t in pending}
        assert ids == {t1.id, t2.id}

    def test_get_pending_excludes_approved(self, tmp_approval_repo: ApprovalRepository):
        t1 = tmp_approval_repo.create_ticket("job_A", "tool", {})
        t2 = tmp_approval_repo.create_ticket("job_A", "tool", {})
        tmp_approval_repo.approve_ticket(t1.id)

        pending = tmp_approval_repo.get_pending_for_job("job_A")
        assert len(pending) == 1
        assert pending[0].id == t2.id

    def test_get_pending_no_results(self, tmp_approval_repo: ApprovalRepository):
        assert tmp_approval_repo.get_pending_for_job("nonexistent_job") == []


# =============================================================================
# ApprovalRepository.get_stats
# =============================================================================


class TestGetStats:
    def test_stats_empty(self, tmp_approval_repo: ApprovalRepository):
        stats = tmp_approval_repo.get_stats()
        assert stats == {
            "pending": 0,
            "approved": 0,
            "consumed": 0,
            "rejected": 0,
            "expired": 0,
        }

    def test_stats_mixed(self, tmp_approval_repo: ApprovalRepository):
        tmp_approval_repo.create_ticket("job_1", "tool", {})
        tmp_approval_repo.create_ticket("job_2", "tool", {})
        t3 = tmp_approval_repo.create_ticket("job_3", "tool", {})
        t4 = tmp_approval_repo.create_ticket("job_4", "tool", {})
        tmp_approval_repo.approve_ticket(t3.id)
        tmp_approval_repo.reject_ticket(t4.id)

        stats = tmp_approval_repo.get_stats()
        assert stats == {
            "pending": 2,
            "approved": 1,
            "consumed": 0,
            "rejected": 1,
            "expired": 0,
        }


# =============================================================================
# args_hash tamper verification
# =============================================================================


class TestArgsHashTamperCheck:
    def test_verify_args_matches(self, tmp_approval_repo: ApprovalRepository):
        args = {"cmd": "ls", "path": "/tmp"}
        ticket = tmp_approval_repo.create_ticket("job_1", "tool", args)
        assert ticket.verify_args(args)

    def test_verify_args_tampered(self, tmp_approval_repo: ApprovalRepository):
        original_args = {"cmd": "ls", "path": "/tmp"}
        ticket = tmp_approval_repo.create_ticket("job_1", "tool", original_args)
        tampered_args = {"cmd": "rm", "path": "/tmp"}
        assert not ticket.verify_args(tampered_args)

    def test_args_hash_16_chars(self, tmp_approval_repo: ApprovalRepository):
        ticket = tmp_approval_repo.create_ticket("job_1", "tool", {"a": 1})
        assert len(ticket.args_hash) == 16


# =============================================================================
# Atomic write guarantees
# =============================================================================


class TestAtomicWrite:
    def test_no_tmp_files_after_create(self, tmp_approval_repo: ApprovalRepository, tmp_path: Path):
        tmp_approval_repo.create_ticket("job_1", "tool", {})
        approvals_dir = tmp_path / "approvals"
        tmp_files = list(approvals_dir.glob("*.tmp"))
        assert len(tmp_files) == 0, f"Found leftover .tmp files: {tmp_files}"

    def test_no_tmp_files_after_approve(self, tmp_approval_repo: ApprovalRepository, tmp_path: Path):
        ticket = tmp_approval_repo.create_ticket("job_1", "tool", {})
        tmp_approval_repo.approve_ticket(ticket.id)
        approvals_dir = tmp_path / "approvals"
        tmp_files = list(approvals_dir.glob("*.tmp"))
        assert len(tmp_files) == 0, f"Found leftover .tmp files: {tmp_files}"

    def test_no_tmp_files_after_reject(self, tmp_approval_repo: ApprovalRepository, tmp_path: Path):
        ticket = tmp_approval_repo.create_ticket("job_1", "tool", {})
        tmp_approval_repo.reject_ticket(ticket.id)
        approvals_dir = tmp_path / "approvals"
        tmp_files = list(approvals_dir.glob("*.tmp"))
        assert len(tmp_files) == 0, f"Found leftover .tmp files: {tmp_files}"

    def test_no_tmp_files_after_expire(self, tmp_approval_repo: ApprovalRepository, tmp_path: Path):
        tmp_approval_repo.create_ticket("job_1", "tool", {}, timeout_sec=1)  # noqa: F841
        time.sleep(1.5)
        tmp_approval_repo.expire_tickets()
        approvals_dir = tmp_path / "approvals"
        tmp_files = list(approvals_dir.glob("*.tmp"))
        assert len(tmp_files) == 0, f"Found leftover .tmp files: {tmp_files}"


# =============================================================================
# Update ticket
# =============================================================================


class TestUpdateTicket:
    def test_update_ticket_refreshes_timestamp(self, tmp_approval_repo: ApprovalRepository):
        ticket = tmp_approval_repo.create_ticket("job_1", "tool", {})
        original_updated = ticket.updated_at
        time.sleep(0.01)
        ticket.reason = "Updated reason"
        updated = tmp_approval_repo.update_ticket(ticket)
        assert updated.reason == "Updated reason"
        assert updated.updated_at > original_updated

    def test_update_ticket_persists(self, tmp_approval_repo: ApprovalRepository):
        ticket = tmp_approval_repo.create_ticket("job_1", "tool", {})
        ticket.reason = "New reason"
        tmp_approval_repo.update_ticket(ticket)
        fetched = tmp_approval_repo.get_ticket(ticket.id)
        assert fetched is not None
        assert fetched.reason == "New reason"


# =============================================================================
# Ticket file naming
# =============================================================================


class TestTicketFileNaming:
    def test_file_named_by_ticket_id(self, tmp_approval_repo: ApprovalRepository, tmp_path: Path):
        ticket = tmp_approval_repo.create_ticket("job_1", "tool", {})
        expected_file = tmp_path / "approvals" / f"{ticket.id}.json"
        assert expected_file.exists()

    def test_file_is_valid_json(self, tmp_approval_repo: ApprovalRepository, tmp_path: Path):
        ticket = tmp_approval_repo.create_ticket("job_1", "tool", {})
        file_path = tmp_path / "approvals" / f"{ticket.id}.json"
        with open(file_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["id"] == ticket.id
        assert data["status"] == "pending"
        assert "args_hash" in data
        assert "args_preview" in data
