"""CLI approval ticket commands — tickets, approve, reject."""

from __future__ import annotations

import json
import sys

from control_plane.approval import ApprovalRepository, TicketStatus

from cli.utils import _make_repository, _make_run_service


async def cmd_tickets(args):
    """List approval tickets."""
    repo = ApprovalRepository()

    repo.expire_tickets()

    status = TicketStatus(args.status) if args.status else None
    tickets = repo.list_tickets(status=status, job_id=args.job_id)

    result = {
        "tickets": [
            {
                "id": t.id,
                "job_id": t.job_id,
                "tool_name": t.tool_name,
                "status": t.status.value,
                "risk_level": t.risk_level,
                "args_preview": t.args_preview,
                "requested_at": t.requested_at.isoformat(),
                "expires_at": t.expires_at.isoformat() if t.expires_at else None,
            }
            for t in tickets
        ],
        "count": len(tickets),
        "stats": repo.get_stats(),
    }
    print(json.dumps(result, indent=2, default=str))


async def cmd_approve(args):
    """Approve an approval ticket."""
    repo = ApprovalRepository()
    job_repo = _make_repository()
    service = _make_run_service(job_repo, non_interactive=True)
    ticket = repo.get_ticket(args.ticket_id)

    if not ticket:
        sys.stderr.write(json.dumps({"error": f"Ticket {args.ticket_id} not found",
                                     "code": "E3001"}) + "\n")
        sys.exit(1)

    previous_status = ticket.status.value

    try:
        ticket = repo.approve_ticket(args.ticket_id, reason=args.reason or "")
        await service.resume_after_approval(ticket.job_id, ticket.id)
    except ValueError as e:
        sys.stderr.write(json.dumps({"error": str(e), "code": "E3002"}) + "\n")
        sys.exit(1)

    print(json.dumps({
        "ticket_id": ticket.id,
        "status": ticket.status.value,
        "previous_status": previous_status,
        "decided_by": ticket.decided_by,
        "reason": ticket.reason,
        "decided_at": ticket.decided_at.isoformat() if ticket.decided_at else None,
        "message": "Ticket approved",
    }, indent=2, default=str))


async def cmd_reject(args):
    """Reject an approval ticket."""
    repo = ApprovalRepository()
    job_repo = _make_repository()
    service = _make_run_service(job_repo, non_interactive=True)
    ticket = repo.get_ticket(args.ticket_id)

    if not ticket:
        sys.stderr.write(json.dumps({"error": f"Ticket {args.ticket_id} not found",
                                     "code": "E3001"}) + "\n")
        sys.exit(1)

    previous_status = ticket.status.value

    try:
        ticket = repo.reject_ticket(args.ticket_id, reason=args.reason or "")
        try:
            await service.abort_after_rejection(ticket.job_id, ticket.id, reason=args.reason or "")
        except ValueError as abort_error:
            if "not found" not in str(abort_error):
                raise
    except ValueError as e:
        sys.stderr.write(json.dumps({"error": str(e), "code": "E3003"}) + "\n")
        sys.exit(1)

    print(json.dumps({
        "ticket_id": ticket.id,
        "status": ticket.status.value,
        "previous_status": previous_status,
        "decided_by": ticket.decided_by,
        "reason": ticket.reason,
        "decided_at": ticket.decided_at.isoformat() if ticket.decided_at else None,
        "message": "Ticket rejected",
    }, indent=2, default=str))
