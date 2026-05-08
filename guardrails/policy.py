"""
Guardrails: permission system and risk classification.
Inspired by Claude Code's permission modes and Anthropic's four-layer security.
"""

from __future__ import annotations

from core.models import (
    RiskLevel,
    PermissionMode,
    GuardrailPolicy,
    ToolResult,
)
from tools.registry import ToolRegistry


class Guardrails:
    """
    Defense-in-depth guardrail system:
    - Tier 1: Safe, non-state-modifying (Read, Grep, Glob)
    - Tier 2: In-project file operations (Write, Edit)
    - Tier 3: Potentially dangerous (Bash, network, external)
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

    def __init__(self, policy: GuardrailPolicy, tool_registry: ToolRegistry):
        self.policy = policy
        self.tool_registry = tool_registry
        self._pending_approvals: dict[str, str] = {}

    def evaluate(self, tool_name: str, arguments: dict) -> tuple[bool, str]:
        """
        Evaluate if a tool call should be allowed.
        Returns (allowed, reason).
        """
        risk = self.RISK_MAP.get(tool_name, RiskLevel.HIGH)

        # Mode-based rules
        if self.policy.mode == PermissionMode.DONT_ASK:
            if tool_name not in self.policy.allowed_tools:
                return False, f"Tool '{tool_name}' not in allowed list (dontAsk mode)"
            return True, "Pre-approved"

        if self.policy.mode == PermissionMode.PLAN:
            if risk.value >= RiskLevel.MEDIUM.value:
                return False, f"Tool '{tool_name}' requires write access (plan mode is read-only)"
            return True, "Read-only access granted"

        # Check deny lists
        if tool_name in self.policy.denied_tools:
            return False, f"Tool '{tool_name}' is explicitly denied"

        if tool_name == "bash":
            cmd = arguments.get("command", "")
            for denied in self.policy.denied_commands:
                if denied in cmd:
                    return False, f"Command contains denied pattern: '{denied}'"

        # Risk-based auto-approval
        if self.policy.mode == PermissionMode.AUTO:
            if risk == RiskLevel.LOW:
                return True, "Auto-approved: low risk"
            if risk == RiskLevel.MEDIUM and self.policy.auto_approve_read:
                if tool_name in ("write", "edit"):
                    return True, "Auto-approved: medium risk in project scope"
            return False, f"High risk action '{tool_name}' requires approval (auto mode)"

        if self.policy.mode == PermissionMode.ACCEPT_EDITS:
            if risk.value <= RiskLevel.MEDIUM.value:
                return True, "Auto-approved (acceptEdits mode)"
            return False, "High risk action requires approval"

        # DEFAULT mode: ask for everything except reads
        if risk == RiskLevel.LOW and self.policy.auto_approve_read:
            return True, "Auto-approved: read operation"

        return False, f"Action '{tool_name}' requires explicit approval (default mode)"

    def guarded_execute(self, tool_name: str, arguments: dict) -> ToolResult:
        """
        Execute a tool call through the guardrail system.

        Checks permissions before delegating to the tool registry.
        This is the single entry point that v2.0 agents should use.
        """
        allowed, reason = self.evaluate(tool_name, arguments)
        if not allowed:
            return ToolResult(
                tool_call_id="",
                success=False,
                error=f"Blocked by guardrails: {reason}",
            )
        return self.tool_registry.execute(tool_name, arguments)

    def check_session_limits(self, iteration: int, errors: list) -> tuple[bool, str]:
        """Check if session has exceeded safety limits."""
        if iteration >= self.policy.max_iterations:
            return False, f"Max iterations ({self.policy.max_iterations}) reached"
        if len(errors) >= 5:
            return False, f"Too many errors ({len(errors)}), stopping for safety"
        return True, "Within limits"

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
