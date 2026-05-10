"""
Core exceptions for the Harness execution engine.

Placed in core/ to avoid circular imports (core must not depend on agent/ or
control_plane/). All modules that need PendingApprovalError import from here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from guardrails.policy import GuardrailResult


class PendingApprovalError(Exception):
    """Raised when a tool call requires human approval before execution.

    Propagation chain:
        WorkerAgent._execute_tool()
          -> Guardrails.check_and_execute() creates ticket
          -> raises PendingApprovalError
        -> DAGEngine._execute_single_node() (transparent, no retry)
        -> RunService.run_job() (update run status, re-raise)
        -> Worker._execute_job() (enter PENDING_APPROVAL poll loop)
    """

    def __init__(
        self,
        ticket_id: str,
        guardrail_result: GuardrailResult | None = None,
    ) -> None:
        self.ticket_id = ticket_id
        self.guardrail_result = guardrail_result
        super().__init__(f"Pending approval required: {ticket_id}")
