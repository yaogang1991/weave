"""
ApprovalRepository — persistent store for approval tickets.

Tickets are stored as individual JSON files under ``./data/tickets/``:
  ``{ticket_id}.json``

Provides list, approve, reject, expire operations and basic stats.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from control_plane.models import Ticket, TicketStatus


# =============================================================================
# Helpers
# =============================================================================


def _utc_now() -> datetime:
    """Current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


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
    """Persistent store for :class:`Ticket` objects."""

    def __init__(self, base_path: str = "./data/tickets") -> None:
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _ticket_path(self, ticket_id: str) -> Path:
        return self.base_path / f"{ticket_id}.json"

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    @staticmethod
    def _ticket_to_dict(ticket: Ticket) -> dict[str, Any]:
        return ticket.model_dump(mode="json")

    @staticmethod
    def _dict_to_ticket(data: dict[str, Any]) -> Ticket:
        return Ticket(**data)

    def _persist_ticket(self, ticket: Ticket) -> None:
        path = self._ticket_path(ticket.id)
        _json_dump_atomic(self._ticket_to_dict(ticket), path)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_ticket(
        self,
        job_id: str,
        tool_name: str,
        risk_level: str = "medium",
        args_preview: str = "",
        expires_in_sec: int = 3600,
    ) -> Ticket:
        """Create a new pending ticket, persist it, and return it."""
        now = _utc_now()
        ticket = Ticket(
            id=f"ticket_{uuid.uuid4().hex[:12]}",
            job_id=job_id,
            tool_name=tool_name,
            status=TicketStatus.PENDING,
            risk_level=risk_level,
            args_preview=args_preview,
            requested_at=now,
            expires_at=now + timedelta(seconds=expires_in_sec),
        )
        self._persist_ticket(ticket)
        return ticket

    def get_ticket(self, ticket_id: str) -> Ticket | None:
        """Load a ticket by ID, or ``None`` if not found."""
        path = self._ticket_path(ticket_id)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return self._dict_to_ticket(data)

    def list_tickets(
        self,
        status: TicketStatus | None = None,
        job_id: str | None = None,
    ) -> list[Ticket]:
        """Return all tickets, optionally filtered by status and/or job_id."""
        tickets: list[Ticket] = []
        for path in self.base_path.glob("*.json"):
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            ticket = self._dict_to_ticket(data)
            if status is not None and ticket.status != status:
                continue
            if job_id is not None and ticket.job_id != job_id:
                continue
            tickets.append(ticket)
        tickets.sort(key=lambda t: t.requested_at)
        return tickets

    def _transition_status(
        self,
        ticket_id: str,
        to_status: TicketStatus,
        reason: str = "",
    ) -> Ticket:
        """Internal helper to transition a ticket to a new status."""
        ticket = self.get_ticket(ticket_id)
        if ticket is None:
            raise ValueError(f"Ticket not found: {ticket_id}")
        if ticket.status == to_status:
            raise ValueError(f"Ticket already in status {to_status.value}")
        ticket.status = to_status
        ticket.reason = reason
        ticket.resolved_at = _utc_now()
        self._persist_ticket(ticket)
        return ticket

    def approve_ticket(self, ticket_id: str, reason: str = "") -> Ticket:
        """Approve a pending ticket.

        Raises:
            ValueError: If the ticket is not found or not in PENDING status.
        """
        ticket = self.get_ticket(ticket_id)
        if ticket is None:
            raise ValueError(f"Ticket not found: {ticket_id}")
        if ticket.status != TicketStatus.PENDING:
            raise ValueError(
                f"Cannot approve ticket in status {ticket.status.value}"
            )
        return self._transition_status(ticket_id, TicketStatus.APPROVED, reason)

    def reject_ticket(self, ticket_id: str, reason: str = "") -> Ticket:
        """Reject a pending ticket.

        Raises:
            ValueError: If the ticket is not found or not in PENDING status.
        """
        ticket = self.get_ticket(ticket_id)
        if ticket is None:
            raise ValueError(f"Ticket not found: {ticket_id}")
        if ticket.status != TicketStatus.PENDING:
            raise ValueError(
                f"Cannot reject ticket in status {ticket.status.value}"
            )
        return self._transition_status(ticket_id, TicketStatus.REJECTED, reason)

    def expire_tickets(self) -> list[Ticket]:
        """Transition all expired pending tickets to EXPIRED."""
        now = _utc_now()
        expired: list[Ticket] = []
        for ticket in self.list_tickets(status=TicketStatus.PENDING):
            if ticket.expires_at is not None and ticket.expires_at < now:
                ticket.status = TicketStatus.EXPIRED
                ticket.resolved_at = now
                self._persist_ticket(ticket)
                expired.append(ticket)
        return expired

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, int]:
        """Return counts per ticket status."""
        stats: dict[str, int] = {}
        for status in TicketStatus:
            stats[status.value] = len(self.list_tickets(status=status))
        return stats
