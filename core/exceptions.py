"""
Core exceptions for the Weave execution engine.

Placed in core/ to avoid circular imports (core must not depend on agent/ or
control_plane/). All modules that need these exceptions import from here.

Fault tolerance contract (#360):
    All faults propagate via exception types, not return-value status dicts.
    Each layer catches only the exceptions in its contract.
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


class NodeTimeoutError(Exception):
    """Raised when a DAG node exceeds its wall-clock timeout.

    Propagation chain:
        DAGEngine._execute_with_timeout() — asyncio.wait_for timeout
          -> raises NodeTimeoutError
        -> DAGEngine._execute_single_node() — marks node FAILED, may retry
        -> RunService.run_job() — classifies as "timeout"
    """

    def __init__(self, node_id: str, agent_type: str, timeout: float) -> None:
        self.node_id = node_id
        self.agent_type = agent_type
        self.timeout = timeout
        super().__init__(
            f"Node {node_id} ({agent_type}) exceeded {timeout}s timeout"
        )


class RateLimitError(Exception):
    """Raised when LLM API rate-limiting is unrecoverable within retry budget.

    Contract (#360): RateLimitError does NOT consume retry budget at any layer:
    - Layer 3 (dag_engine): does not increment node.retry_count
    - Layer 4 (service): re-queues job without bumping attempt (skip_attempt_bump)

    Propagation chain:
        llm_client.call() — retries exhausted under rate-limit
          -> raises RateLimitError
        -> AgentWorker.run() — propagates
        -> agent_pool._run_with_tools() — propagates
        -> DAGEngine._execute_single_node() — marks FAILED without retry cost
        -> RunService.run_job() — classifies as "rate_limit"
    """

    def __init__(self, provider: str, model: str, retries: int) -> None:
        self.provider = provider
        self.model = model
        self.retries = retries
        super().__init__(
            f"Rate limit exhausted for {provider}/{model} after {retries} retries"
        )


class BudgetExhaustedError(Exception):
    """Raised when cumulative token usage exceeds the configured budget (M4.2).

    Propagation chain:
        NodeExecutor.execute_node() — budget_manager.check() returns False
          -> raises BudgetExhaustedError
        -> DAGExecutionEngine._execute_inner() — catches, skips remaining nodes
        -> RunService.run_job() — classifies as "budget_exhausted"
    """

    def __init__(
        self, used_tokens: int, budget_tokens: int, node_id: str = "",
    ) -> None:
        self.used_tokens = used_tokens
        self.budget_tokens = budget_tokens
        self.node_id = node_id
        super().__init__(
            f"Token budget exhausted: {used_tokens}/{budget_tokens} tokens used"
            f"{f' at node {node_id}' if node_id else ''}"
        )
