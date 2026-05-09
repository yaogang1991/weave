# ADR 0008: Unified Tri-State Guardrail Entry (M1.1)

**Status:** Accepted
**Date:** 2026-05-09
**Deciders:** Project Lead

## Context

The guardrail system needs to handle tool execution with different risk levels and permission modes. M1 had two paths:

1. **Interactive path**: `request_confirmation()` reads stdin for HIGH risk
2. **Auto-approve path**: LOW/MEDIUM risk passes through

This bifurcation caused issues:
- Worker mode couldn't use stdin (blocks the process)
- No persistent record of approval decisions
- Dual paths made testing and reasoning harder

## Decision

We unified into a **tri-state entry point** (`guardrails/policy.py` — `check_and_execute()`):

- `allowed` — tool executes immediately
- `blocked` — tool rejected, returns error
- `pending_approval(ticket_id)` — tool paused, `ApprovalTicket` created

The approval ticket system (`control_plane/approval.py`) provides persistence:
- `TicketStatus`: pending → approved | rejected | expired
- Tickets survive process restarts
- CLI commands: `tickets`, `approve`, `reject`
- Non-interactive mode: HIGH risk → pending ticket (no stdin blocking)

## Consequences

**Positive:**
- Single code path — all guardrail decisions go through one function
- Persistent approvals — survive restarts, auditable
- Non-interactive friendly — no stdin blocking needed
- Testable — clear return types, no side effects in guardrail logic

**Negative:**
- Ticket cleanup needed (expired tickets accumulate)
- Extra step for human approval (must run `approve` command)

## Alternatives Considered

- **Keep dual path**: Simpler for interactive mode but broken for unattended operation.
- **Callback-based**: Asynchronous approval via callbacks. More complex, no real benefit over ticket polling.
