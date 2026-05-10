"""
审批票据系统 — 持久化的高风险操作审批流。

设计决策：
1. 审批票据与 Job/Run 解耦但可关联（通过 job_id/run_id）
2. 票据持久化到 JSON 文件（原子写入）
3. 支持 pending/approved/rejected/expired 四态
4. Worker 重启后可扫描并处理 pending ticket
5. 票据有过期时间，超时自动转 expired
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator


# =============================================================================
# Status enum
# =============================================================================


class TicketStatus(str, Enum):
    """Lifecycle states of an approval ticket."""

    PENDING = "pending"     # 等待审批
    APPROVED = "approved"   # 已批准
    REJECTED = "rejected"   # 已拒绝
    EXPIRED = "expired"     # 已过期（超时未处理）


# =============================================================================
# Data model
# =============================================================================


class ApprovalTicket(BaseModel):
    """A ticket requesting human (or automated) approval for a risky tool call."""

    id: str                          # ticket_xxx
    job_id: str                      # 关联的 job
    run_id: str | None = None        # 关联的 run
    node_id: str | None = None       # 关联的 DAG 节点
    tool_name: str                   # 被审批的工具名
    args_hash: str                   # 参数哈希（用于防篡改验证）
    args_preview: str                # 参数预览（供人阅读）
    risk_level: str                  # low/medium/high/critical
    status: TicketStatus = TicketStatus.PENDING
    requested_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None    # "user" / "auto" / "timeout"
    reason: str = ""                 # 审批理由（approve/reject 时填写）
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("status", mode="before")
    @classmethod
    def _reject_invalid_status(cls, v: Any) -> Any:
        if isinstance(v, str) and v not in {m.value for m in TicketStatus}:
            raise ValueError(f"Invalid TicketStatus: {v!r}")
        return v

    @field_validator("risk_level")
    @classmethod
    def _valid_risk_level(cls, v: str) -> str:
        allowed = {"low", "medium", "high", "critical"}
        if v not in allowed:
            raise ValueError(f"Invalid risk_level: {v!r}, must be one of {allowed}")
        return v

    def is_terminal(self) -> bool:
        """Return True if the ticket has reached a terminal state."""
        return self.status in {
            TicketStatus.APPROVED,
            TicketStatus.REJECTED,
            TicketStatus.EXPIRED,
        }

    def is_pending(self) -> bool:
        """Return True if the ticket is still awaiting a decision."""
        return self.status == TicketStatus.PENDING

    def verify_args(self, args: dict[str, Any]) -> bool:
        """Verify that *args* matches the stored args_hash (tamper check)."""
        computed = _compute_args_hash(args)
        return computed == self.args_hash


# =============================================================================
# Helpers
# =============================================================================


def _utc_now() -> datetime:
    """Current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


def _compute_args_hash(args: dict[str, Any]) -> str:
    """Compute a short SHA-256 hash of *args* for tamper detection."""
    payload = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _make_args_preview(args: dict[str, Any], max_len: int = 200) -> str:
    """Create a human-readable preview of *args*, truncated to *max_len* chars."""
    preview = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
    if len(preview) > max_len:
        preview = preview[:max_len] + "..."
    return preview


def _json_dump_atomic(data: dict[str, Any], path: Path) -> None:
    """Write *data* to *path* atomically via a temporary file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(path))
    except Exception:
        # Best-effort cleanup of the temp file on failure
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


# =============================================================================
# Repository
# =============================================================================


class ApprovalRepository:
    """Persistent store for :class:`ApprovalTicket` objects.

    All mutations are atomic (write-to-temp + rename).  The store is
    *single-writer-safe* when backed by a local POSIX filesystem; concurrent
    writers to the same file are not protected and require external locking.
    """

    def __init__(self, base_path: str = "./data/approvals") -> None:
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _ticket_file(self, ticket_id: str) -> Path:
        return self.base_path / f"{ticket_id}.json"

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ticket_to_dict(ticket: ApprovalTicket) -> dict[str, Any]:
        return ticket.model_dump(mode="json")

    @staticmethod
    def _dict_to_ticket(data: dict[str, Any]) -> ApprovalTicket:
        return ApprovalTicket(**data)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_ticket(
        self,
        job_id: str,
        tool_name: str,
        args: dict[str, Any],
        risk_level: str = "high",
        run_id: str | None = None,
        node_id: str | None = None,
        timeout_sec: int = 300,
    ) -> ApprovalTicket:
        """Create a new approval ticket, persist it, and return it.

        Args:
            job_id: The associated job ID.
            tool_name: Name of the tool requiring approval.
            args: Tool arguments to be reviewed.
            risk_level: One of ``low``, ``medium``, ``high``, ``critical``.
            run_id: Optional associated run ID.
            node_id: Optional associated DAG node ID.
            timeout_sec: Seconds until the ticket auto-expires.

        Returns:
            The created :class:`ApprovalTicket`.
        """
        now = _utc_now()
        ticket_id = f"ticket_{uuid.uuid4().hex[:8]}"
        args_hash = _compute_args_hash(args)
        args_preview = _make_args_preview(args)
        expires_at = now + timedelta(seconds=timeout_sec)

        ticket = ApprovalTicket(
            id=ticket_id,
            job_id=job_id,
            run_id=run_id,
            node_id=node_id,
            tool_name=tool_name,
            args_hash=args_hash,
            args_preview=args_preview,
            risk_level=risk_level,
            status=TicketStatus.PENDING,
            requested_at=now,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
        self._persist_ticket(ticket)
        return ticket

    def get_ticket(self, ticket_id: str) -> ApprovalTicket | None:
        """Load a ticket by ID, or ``None`` if not found."""
        path = self._ticket_file(ticket_id)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return self._dict_to_ticket(data)

    def update_ticket(self, ticket: ApprovalTicket) -> ApprovalTicket:
        """Persist an updated ticket.  The *updated_at* field is refreshed."""
        ticket.updated_at = _utc_now()
        self._persist_ticket(ticket)
        return ticket

    def _persist_ticket(self, ticket: ApprovalTicket) -> None:
        path = self._ticket_file(ticket.id)
        _json_dump_atomic(self._ticket_to_dict(ticket), path)

    def list_tickets(
        self,
        status: TicketStatus | None = None,
        job_id: str | None = None,
        tool_name: str | None = None,
    ) -> list[ApprovalTicket]:
        """Return all tickets, optionally filtered by status, job_id, and/or tool_name."""
        tickets: list[ApprovalTicket] = []
        for path in self.base_path.glob("*.json"):
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            ticket = self._dict_to_ticket(data)
            if status is not None and ticket.status != status:
                continue
            if job_id is not None and ticket.job_id != job_id:
                continue
            if tool_name is not None and ticket.tool_name != tool_name:
                continue
            tickets.append(ticket)
        # Sort by creation time for stable ordering
        tickets.sort(key=lambda t: t.created_at)
        return tickets

    # ------------------------------------------------------------------
    # Status transitions
    # ------------------------------------------------------------------

    def approve_ticket(
        self,
        ticket_id: str,
        reason: str = "",
        decided_by: str = "user",
    ) -> ApprovalTicket:
        """Approve a pending ticket.

        Raises:
            ValueError: If the ticket does not exist or is not in ``PENDING`` status.
        """
        ticket = self.get_ticket(ticket_id)
        if ticket is None:
            raise ValueError(f"Ticket not found: {ticket_id}")
        if ticket.status != TicketStatus.PENDING:
            raise ValueError(
                f"Cannot approve ticket {ticket_id}: status is {ticket.status.value}, "
                f"expected {TicketStatus.PENDING.value}"
            )
        # Reject expired tickets even if still in PENDING status
        now = _utc_now()
        if ticket.expires_at is not None and ticket.expires_at < now:
            ticket.status = TicketStatus.EXPIRED
            ticket.decided_at = now
            ticket.decided_by = "timeout"
            ticket.reason = "Ticket expired (timeout)"
            ticket.updated_at = now
            self._persist_ticket(ticket)
            raise ValueError(
                f"Cannot approve ticket {ticket_id}: ticket expired at "
                f"{ticket.expires_at.isoformat()}"
            )
        ticket.status = TicketStatus.APPROVED
        ticket.decided_at = _utc_now()
        ticket.decided_by = decided_by
        ticket.reason = reason
        ticket.updated_at = _utc_now()
        self._persist_ticket(ticket)
        return ticket

    def reject_ticket(
        self,
        ticket_id: str,
        reason: str = "",
        decided_by: str = "user",
    ) -> ApprovalTicket:
        """Reject a pending ticket.

        Raises:
            ValueError: If the ticket does not exist or is not in ``PENDING`` status.
        """
        ticket = self.get_ticket(ticket_id)
        if ticket is None:
            raise ValueError(f"Ticket not found: {ticket_id}")
        if ticket.status != TicketStatus.PENDING:
            raise ValueError(
                f"Cannot reject ticket {ticket_id}: status is {ticket.status.value}, "
                f"expected {TicketStatus.PENDING.value}"
            )
        # Reject expired tickets even if still in PENDING status
        now = _utc_now()
        if ticket.expires_at is not None and ticket.expires_at < now:
            ticket.status = TicketStatus.EXPIRED
            ticket.decided_at = now
            ticket.decided_by = "timeout"
            ticket.reason = "Ticket expired (timeout)"
            ticket.updated_at = now
            self._persist_ticket(ticket)
            raise ValueError(
                f"Cannot reject ticket {ticket_id}: ticket expired at "
                f"{ticket.expires_at.isoformat()}"
            )
        ticket.status = TicketStatus.REJECTED
        ticket.decided_at = _utc_now()
        ticket.decided_by = decided_by
        ticket.reason = reason
        ticket.updated_at = _utc_now()
        self._persist_ticket(ticket)
        return ticket

    def expire_tickets(self) -> list[ApprovalTicket]:
        """Scan all ``PENDING`` tickets and mark expired ones.

        Returns:
            The list of tickets that were transitioned to ``EXPIRED``.
        """
        now = _utc_now()
        expired: list[ApprovalTicket] = []
        for ticket in self.list_tickets(status=TicketStatus.PENDING):
            if ticket.expires_at is not None and ticket.expires_at < now:
                ticket.status = TicketStatus.EXPIRED
                ticket.decided_at = now
                ticket.decided_by = "timeout"
                ticket.reason = "Ticket expired (timeout)"
                ticket.updated_at = now
                self._persist_ticket(ticket)
                expired.append(ticket)
        return expired

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_pending_for_job(self, job_id: str) -> list[ApprovalTicket]:
        """Return all pending tickets for a given job."""
        return self.list_tickets(status=TicketStatus.PENDING, job_id=job_id)

    def consume_ticket(self, ticket: ApprovalTicket) -> None:
        """Mark an approved ticket as consumed so it cannot be reused.

        Transitions the ticket to EXPIRED status with a 'consumed' marker
        in the reason field.
        """
        if ticket.status != TicketStatus.APPROVED:
            return
        ticket.status = TicketStatus.EXPIRED
        ticket.reason = (ticket.reason or "") + " [consumed]"
        ticket.updated_at = datetime.now(timezone.utc)
        self._persist_ticket(ticket)

    def find_approved_ticket(
        self,
        job_id: str,
        tool_name: str,
        args: dict[str, Any],
        node_id: str | None = None,
    ) -> ApprovalTicket | None:
        """Find a matching approved ticket for the same tool call.

        Matching is by job_id + tool_name + args_hash.
        run_id is NOT used for matching because re-running a job creates a new run_id.
        node_id is used as a weak match (only if both sides have it).
        """
        args_hash = _compute_args_hash(args)
        for ticket in self.list_tickets(status=TicketStatus.APPROVED, job_id=job_id):
            if ticket.tool_name != tool_name:
                continue
            if ticket.args_hash != args_hash:
                continue
            if node_id is not None and ticket.node_id is not None and ticket.node_id != node_id:
                continue
            return ticket
        return None

    def get_stats(self) -> dict[str, int]:
        """Return count of tickets in each status.

        Returns:
            A dict mapping status name to count.
        """
        stats: dict[str, int] = {
            TicketStatus.PENDING.value: 0,
            TicketStatus.APPROVED.value: 0,
            TicketStatus.REJECTED.value: 0,
            TicketStatus.EXPIRED.value: 0,
        }
        for ticket in self.list_tickets():
            stats[ticket.status.value] += 1
        return stats
