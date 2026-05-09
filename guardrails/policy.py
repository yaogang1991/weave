"""
Guardrails: permission system and risk classification.
Inspired by Claude Code's permission modes and Anthropic's four-layer security.

This module provides a unified execution entry-point for all tool calls,
eliminating dual-path semantics between Guardrails and PersonalGuardrails.

Unified tri-state result:
  - allowed          → execute directly, return ToolResult
  - blocked          → reject, return GuardrailResult with reason
  - pending_approval → create ApprovalTicket, return GuardrailResult with ticket_id
"""

from __future__ import annotations

import re
import select
import sys
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from core.models import (
    GuardrailPolicy,
    PermissionMode,
    PersonalGuardrailPolicy,
    RiskLevel,
    ToolResult,
)
from tools.registry import ToolRegistry

if TYPE_CHECKING:
    from control_plane.approval import ApprovalRepository


# =============================================================================
# Unified tri-state result
# =============================================================================


@dataclass
class GuardrailResult:
    """Guardrails evaluation result — three-state decision.

    States:
      * ``allowed``  — Tool execution is approved.
      * ``blocked``  — Tool execution is denied (policy violation, denied list, etc.).
      * ``pending_approval`` — Human approval required; an ApprovalTicket
        should (or has been) created.
    """

    decision: Literal["allowed", "blocked", "pending_approval"]
    reason: str = ""
    ticket_id: str | None = None  # only valid when decision == "pending_approval"

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    @property
    def is_allowed(self) -> bool:
        return self.decision == "allowed"

    @property
    def is_blocked(self) -> bool:
        return self.decision == "blocked"

    @property
    def is_pending(self) -> bool:
        return self.decision == "pending_approval"

    def __repr__(self) -> str:
        if self.ticket_id:
            return (
                f"GuardrailResult(decision={self.decision!r}, "
                f"reason={self.reason!r}, ticket_id={self.ticket_id!r})"
            )
        return f"GuardrailResult(decision={self.decision!r}, reason={self.reason!r})"

    # Backward-compat: ``allowed, reason = guardrails.evaluate(...)``
    def __iter__(self):
        return iter((self.is_allowed, self.reason))


# =============================================================================
# Base Guardrails
# =============================================================================


class Guardrails:
    """
    Defense-in-depth guardrail system with a unified execution entry-point.

    All tool calls flow through :meth:`check_and_execute`, which returns a
    tri-state :class:`GuardrailResult`.  Legacy ``guarded_execute`` is kept
    as a thin backward-compatibility wrapper.

    Tiers:
    - Tier 1: Safe, non-state-modifying (Read, Grep, Glob)  → LOW
    - Tier 2: In-project file operations (Write, Edit, Git)  → MEDIUM
    - Tier 3: Potentially dangerous (Bash, network)          → HIGH
    """

    RISK_MAP: dict[str, RiskLevel] = {
        "read": RiskLevel.LOW,
        "glob": RiskLevel.LOW,
        "grep": RiskLevel.LOW,
        "write": RiskLevel.MEDIUM,
        "edit": RiskLevel.MEDIUM,
        "bash": RiskLevel.HIGH,
        "git": RiskLevel.MEDIUM,
    }

    # -- construction -------------------------------------------------

    def __init__(self, policy: GuardrailPolicy, tool_registry: ToolRegistry) -> None:
        self.policy = policy
        self.tool_registry = tool_registry
        self._pending_approvals: dict[str, str] = {}

    # -- public tri-state evaluation ----------------------------------

    def evaluate(self, tool_name: str, arguments: dict) -> GuardrailResult:
        """Unified evaluation entry-point.  Returns a tri-state GuardrailResult."""
        risk = self.RISK_MAP.get(tool_name, RiskLevel.HIGH)

        # 1. Check deny lists (always enforced, regardless of mode)
        if tool_name in self.policy.denied_tools:
            return GuardrailResult(
                decision="blocked",
                reason=f"Tool '{tool_name}' is explicitly denied",
            )

        if tool_name == "bash":
            cmd = arguments.get("command", "")
            for denied in self.policy.denied_commands:
                if denied in cmd:
                    return GuardrailResult(
                        decision="blocked",
                        reason=f"Command contains denied pattern: '{denied}'",
                    )

        # 2. Mode-based routing
        if self.policy.mode == PermissionMode.DONT_ASK:
            return self._evaluate_dont_ask_mode(tool_name, risk)

        if self.policy.mode == PermissionMode.PLAN:
            return self._evaluate_plan_mode(tool_name, risk)

        if self.policy.mode == PermissionMode.AUTO:
            return self._evaluate_auto_mode(tool_name, risk)

        if self.policy.mode == PermissionMode.ACCEPT_EDITS:
            return self._evaluate_accept_edits_mode(tool_name, risk)

        # 3. DEFAULT mode — allow reads, pending_approval for everything else
        return self._evaluate_default_mode(tool_name, risk)

    # -- mode-specific evaluators (private) ---------------------------

    def _evaluate_dont_ask_mode(self, tool_name: str, risk: RiskLevel) -> GuardrailResult:
        """DONT_ASK: only pre-approved tools are allowed."""
        if tool_name not in self.policy.allowed_tools:
            return GuardrailResult(
                decision="blocked",
                reason=f"Tool '{tool_name}' not in allowed list (dont_ask mode)",
            )
        return GuardrailResult(
            decision="allowed",
            reason="Pre-approved (dont_ask)",
        )

    def _evaluate_plan_mode(self, tool_name: str, risk: RiskLevel) -> GuardrailResult:
        """PLAN: read-only — block anything MEDIUM or higher."""
        if risk.value >= RiskLevel.MEDIUM.value:
            return GuardrailResult(
                decision="blocked",
                reason=f"Tool '{tool_name}' requires write access (plan mode is read-only)",
            )
        return GuardrailResult(decision="allowed", reason="Read-only access granted")

    def _evaluate_auto_mode(self, tool_name: str, risk: RiskLevel) -> GuardrailResult:
        """AUTO: auto-approve LOW; MEDIUM conditional; HIGH/CRITICAL pending."""
        if risk == RiskLevel.LOW:
            return GuardrailResult(decision="allowed", reason="Low risk (auto mode)")

        if risk == RiskLevel.MEDIUM:
            auto_approved_medium_tools = {"write", "edit"}
            if self.policy.auto_approve_read and tool_name in auto_approved_medium_tools:
                return GuardrailResult(
                    decision="allowed", reason="Medium risk (auto_approved)"
                )
            return GuardrailResult(
                decision="pending_approval",
                reason="Medium risk requires approval",
            )

        if risk == RiskLevel.HIGH:
            return GuardrailResult(
                decision="pending_approval",
                reason=f"HIGH risk action '{tool_name}' (auto mode)",
            )

        if risk == RiskLevel.CRITICAL:
            return GuardrailResult(
                decision="pending_approval",
                reason=f"CRITICAL action '{tool_name}' (auto mode)",
            )

        return GuardrailResult(
            decision="blocked", reason=f"Unknown risk for '{tool_name}'"
        )

    def _evaluate_accept_edits_mode(
        self, tool_name: str, risk: RiskLevel
    ) -> GuardrailResult:
        """ACCEPT_EDITS: auto-approve up to MEDIUM; HIGH/CRITICAL pending."""
        if risk.value <= RiskLevel.MEDIUM.value:
            return GuardrailResult(
                decision="allowed", reason="Auto-approved (accept_edits mode)"
            )
        if risk == RiskLevel.HIGH:
            return GuardrailResult(
                decision="pending_approval",
                reason=f"HIGH risk '{tool_name}' (accept_edits mode)",
            )
        return GuardrailResult(
            decision="pending_approval",
            reason=f"CRITICAL action '{tool_name}' (accept_edits mode)",
        )

    def _evaluate_default_mode(
        self, tool_name: str, risk: RiskLevel
    ) -> GuardrailResult:
        """DEFAULT: allow LOW reads; MEDIUM pending; HIGH/CRITICAL pending."""
        if risk == RiskLevel.LOW:
            return GuardrailResult(decision="allowed", reason="Low risk")

        if risk == RiskLevel.MEDIUM:
            return GuardrailResult(
                decision="pending_approval",
                reason=f"Medium risk '{tool_name}' needs approval (default mode)",
            )

        if risk == RiskLevel.HIGH:
            return GuardrailResult(
                decision="pending_approval",
                reason=f"HIGH risk action '{tool_name}' (default mode)",
            )

        if risk == RiskLevel.CRITICAL:
            return GuardrailResult(
                decision="pending_approval",
                reason=f"CRITICAL action '{tool_name}' (default mode)",
            )

        return GuardrailResult(
            decision="blocked", reason=f"Unknown risk for '{tool_name}'"
        )

    # -- unified execution entry-point --------------------------------

    def check_and_execute(
        self,
        tool_name: str,
        arguments: dict,
        job_id: str = "",
        run_id: str | None = None,
        approval_repo: ApprovalRepository | None = None,
    ) -> ToolResult | GuardrailResult:
        """Unified execution entry-point.

        Returns:
            * :class:`ToolResult` when decision is ``allowed`` (tool was executed).
            * :class:`GuardrailResult` when decision is ``blocked`` or
              ``pending_approval``.

        When ``pending_approval`` and *approval_repo* + *job_id* are provided,
        an :class:`~control_plane.approval.ApprovalTicket` is created
        automatically and its ID is attached to the returned
        ``GuardrailResult.ticket_id``.
        """
        result = self.evaluate(tool_name, arguments)

        if result.decision == "allowed":
            return self.tool_registry.execute(tool_name, arguments)

        if result.decision == "pending_approval" and approval_repo is not None and job_id:
            ticket = approval_repo.create_ticket(
                job_id=job_id,
                tool_name=tool_name,
                args=arguments,
                risk_level=self.RISK_MAP.get(tool_name, RiskLevel.HIGH).name.lower(),
                run_id=run_id,
            )
            result.ticket_id = ticket.id
            result.reason += f" (ticket: {ticket.id})"

        return result

    # -- legacy backward-compatibility wrappers -----------------------

    def guarded_execute(
        self,
        tool_name: str,
        arguments: dict,
        *,
        job_id: str = "",
        run_id: str | None = None,
        approval_repo: ApprovalRepository | None = None,
    ) -> ToolResult:
        """DEPRECATED — Use :meth:`check_and_execute` instead.

        Kept for backward compatibility.  Wraps ``check_and_execute`` and
        coerces a :class:`GuardrailResult` back into an error
        :class:`ToolResult`.
        """
        warnings.warn(
            "guarded_execute() is deprecated; use check_and_execute() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        result = self.check_and_execute(
            tool_name,
            arguments,
            job_id=job_id,
            run_id=run_id,
            approval_repo=approval_repo,
        )
        if isinstance(result, GuardrailResult):
            if result.decision == "pending_approval":
                ticket = f" (ticket: {result.ticket_id})" if result.ticket_id else ""
                return ToolResult(
                    tool_call_id="",
                    success=False,
                    error=f"Blocked by guardrails: Pending approval required: {result.reason}{ticket}",
                )
            return ToolResult(
                tool_call_id="",
                success=False,
                error=f"Blocked by guardrails: {result.reason}",
            )
        return result

    # -- session limits -----------------------------------------------

    def check_session_limits(self, iteration: int, errors: list) -> tuple[bool, str]:
        """Check if session has exceeded safety limits."""
        if iteration >= self.policy.max_iterations:
            return False, f"Max iterations ({self.policy.max_iterations}) reached"
        if len(errors) >= 5:
            return False, f"Too many errors ({len(errors)}), stopping for safety"
        return True, "Within limits"

    # -- approval-request formatting ----------------------------------

    def format_approval_request(self, tool_name: str, arguments: dict) -> str:
        """Format a human-readable approval request."""
        args_str = "\n".join(f"  {k}: {v}" for k, v in arguments.items())
        return f"""
APPROVAL REQUIRED
Tool: {tool_name}
Arguments:
{args_str}

Allow this action? (y/n/skip)
"""


# =============================================================================
# Personal Guardrails
# =============================================================================


class PersonalGuardrails(Guardrails):
    """
    Personal-mode guardrail system — unified tri-state evaluation.

    Strategy:
    - LOW     → auto-allow
    - MEDIUM  → auto-allow (reversible ops)
    - HIGH    → whitelist check → allowed; else pending_approval
                 (non-interactive: pending_approval without stdin blocking)
    - CRITICAL → always pending_approval
    """

    def __init__(
        self,
        policy: PersonalGuardrailPolicy,
        tool_registry: ToolRegistry,
        non_interactive: bool = False,
        approval_repo: ApprovalRepository | None = None,
    ) -> None:
        super().__init__(policy, tool_registry)
        self.personal_policy = policy
        self.non_interactive = non_interactive
        self.approval_repo = approval_repo

    # -- tri-state evaluate override ----------------------------------

    def evaluate(self, tool_name: str, arguments: dict) -> GuardrailResult:
        """Personal-mode evaluation: LOW/MEDIUM auto-pass; HIGH check whitelist."""
        risk = self.RISK_MAP.get(tool_name, RiskLevel.HIGH)

        # 1. Whitelist check (command-level)
        if tool_name == "bash" and "command" in arguments:
            cmd = arguments["command"]
            if self._is_whitelisted(cmd):
                return GuardrailResult(
                    decision="allowed", reason="Whitelisted command"
                )

        # 2. Tool-level whitelist
        if tool_name in self.personal_policy.whitelist_commands:
            return GuardrailResult(
                decision="allowed", reason="Whitelisted tool"
            )

        # 3. Risk-level routing
        if risk == RiskLevel.LOW:
            return GuardrailResult(decision="allowed", reason="Low risk")

        if risk == RiskLevel.MEDIUM:
            return GuardrailResult(
                decision="allowed", reason="Medium risk (reversible)"
            )

        if risk == RiskLevel.HIGH:
            if self.personal_policy.auto_approve_high:
                return GuardrailResult(
                    decision="allowed",
                    reason="High risk (auto_approve_high)",
                )
            # Non-interactive: return pending_approval without blocking on stdin
            if self.non_interactive:
                return GuardrailResult(
                    decision="pending_approval",
                    reason=f"HIGH risk '{tool_name}' (non-interactive, awaiting approval)",
                )
            return GuardrailResult(
                decision="pending_approval",
                reason=f"HIGH risk action '{tool_name}' requires confirmation (personal mode)",
            )

        if risk == RiskLevel.CRITICAL:
            return GuardrailResult(
                decision="pending_approval",
                reason=f"CRITICAL action '{tool_name}' requires explicit confirmation",
            )

        return GuardrailResult(decision="blocked", reason="Unknown risk")

    # -- whitelist helper ---------------------------------------------

    def _is_whitelisted(self, command: str) -> bool:
        """Check whether *command* matches any whitelist pattern or command prefix.

        Supports:
        - Prefix matching: ``"git status"``, ``"pytest"``
        - Regex matching: ``r"^git\\s+(status|log|diff)$"``
        """
        for pattern in self.personal_policy.whitelist_patterns:
            try:
                if re.match(pattern, command):
                    return True
            except re.error:
                # Invalid regex → fall back to prefix matching
                if command.startswith(pattern):
                    return True

        for cmd in self.personal_policy.whitelist_commands:
            if command.startswith(cmd):
                return True

        return False

    # -- interactive confirmation (optional CLI flow) -----------------

    def request_confirmation(self, tool_name: str, arguments: dict) -> bool:
        """Request human confirmation via stdin (CLI environments).

        Non-interactive mode: returns ``False`` immediately without reading stdin.
        Returns ``True`` when the user confirms, ``False`` on denial or timeout.
        """
        if self.non_interactive:
            return False  # Non-interactive: no stdin blocking

        print(self.format_approval_request(tool_name, arguments))
        print("Allow this action? (y/n): ", end="", flush=True)

        timeout = self.personal_policy.confirmation_timeout_sec
        ready, _, _ = select.select([sys.stdin], [], [], timeout)

        if ready:
            response = sys.stdin.readline().strip().lower()
            return response in ("y", "yes", "ok", "")

        print(f"\nTimeout ({timeout}s). Action denied.")
        return False

    # -- legacy backward-compatibility wrapper ------------------------

    def guarded_execute_with_confirmation(
        self, tool_name: str, arguments: dict
    ) -> ToolResult:
        """DEPRECATED — Use :meth:`check_and_execute` instead.

        Kept for backward compatibility.  Translates the tri-state result
        back into a :class:`ToolResult`, optionally falling back to the
        interactive confirmation flow when the unified evaluator returns
        ``pending_approval``.
        """
        warnings.warn(
            "guarded_execute_with_confirmation() is deprecated; "
            "use check_and_execute() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        result = self.check_and_execute(tool_name, arguments)

        if isinstance(result, ToolResult):
            return result

        if result.decision == "pending_approval":
            # Legacy: fall back to interactive confirmation
            if self.request_confirmation(tool_name, arguments):
                return self.tool_registry.execute(tool_name, arguments)
            return ToolResult(
                tool_call_id="",
                success=False,
                error=f"Guardrails: Action '{tool_name}' denied by user. Reason: {result.reason}",
            )

        return ToolResult(
            tool_call_id="",
            success=False,
            error=f"Guardrails: {result.reason}",
        )
