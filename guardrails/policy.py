"""
Guardrails: permission system and risk classification.
Inspired by Claude Code's permission modes and Anthropic's four-layer security.
"""

from __future__ import annotations

from core.models import (
    RiskLevel,
    PermissionMode,
    GuardrailPolicy,
    PersonalGuardrailPolicy,
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
        This is the single entry point that agents should use.
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


class PersonalGuardrails(Guardrails):
    """
    个人模式护栏系统。

    策略：
    - LOW: 自动通过
    - MEDIUM: 自动通过（文件编辑等可逆操作）
    - HIGH:
      - 命中白名单 → 自动通过
      - 未命中 → 进入确认流程（需要人类确认）
    - CRITICAL: 始终需要确认（不可逆操作）

    拒绝时返回 ToolResult.error，供 orchestrator 决策。
    """

    def __init__(self, policy: PersonalGuardrailPolicy, tool_registry: ToolRegistry):
        super().__init__(policy, tool_registry)
        self.personal_policy = policy

    def evaluate(self, tool_name: str, arguments: dict) -> tuple[bool, str]:
        """个人模式评估逻辑。"""
        risk = self.RISK_MAP.get(tool_name, RiskLevel.HIGH)

        # 检查白名单（命令级别）
        if tool_name == "bash" and "command" in arguments:
            cmd = arguments["command"]
            if self._is_whitelisted(cmd):
                return True, "Auto-approved: command in whitelist"

        # 检查工具级白名单
        if tool_name in self.personal_policy.whitelist_commands:
            return True, "Auto-approved: tool in whitelist"

        # 按风险级别处理
        if risk == RiskLevel.LOW:
            return True, "Auto-approved: low risk"

        if risk == RiskLevel.MEDIUM:
            return True, "Auto-approved: medium risk (reversible)"

        if risk == RiskLevel.HIGH:
            if self.personal_policy.auto_approve_high:
                return True, "Auto-approved: high risk (auto_approve_high enabled)"
            return False, f"HIGH risk action '{tool_name}' requires confirmation (personal mode)"

        if risk == RiskLevel.CRITICAL:
            return False, f"CRITICAL action '{tool_name}' requires explicit confirmation"

        return False, f"Unknown risk level for '{tool_name}'"

    def _is_whitelisted(self, command: str) -> bool:
        """检查命令是否命中白名单。

        白名单支持：
        - 前缀匹配: "git status", "pytest"
        - 正则匹配: "^git\\s+(status|log|diff)$"
        """
        for pattern in self.personal_policy.whitelist_patterns:
            try:
                import re
                if re.match(pattern, command):
                    return True
            except re.error:
                # 不是正则，当作前缀匹配
                if command.startswith(pattern):
                    return True

        # 检查 whitelist_commands
        for cmd in self.personal_policy.whitelist_commands:
            if command.startswith(cmd):
                return True

        return False

    def request_confirmation(self, tool_name: str, arguments: dict) -> bool:
        """
        请求人类确认。

        在 CLI 环境中打印确认请求，等待用户输入。
        返回 True（确认）或 False（拒绝）。
        """
        print(self.format_approval_request(tool_name, arguments))
        print("Allow this action? (y/n): ", end="", flush=True)

        import select
        import sys

        timeout = self.personal_policy.confirmation_timeout_sec
        ready, _, _ = select.select([sys.stdin], [], [], timeout)

        if ready:
            response = sys.stdin.readline().strip().lower()
            return response in ("y", "yes", "ok", "")

        print(f"\nTimeout ({timeout}s). Action denied.")
        return False

    def guarded_execute_with_confirmation(self, tool_name: str, arguments: dict) -> ToolResult:
        """
        带确认流程的执行。

        1. 先 evaluate
        2. 如需要确认，调用 request_confirmation
        3. 确认通过则执行，拒绝则返回 ToolResult.error
        """
        allowed, reason = self.evaluate(tool_name, arguments)

        if allowed:
            return self.tool_registry.execute(tool_name, arguments)

        # 需要确认
        if self.request_confirmation(tool_name, arguments):
            return self.tool_registry.execute(tool_name, arguments)

        # 被拒绝
        return ToolResult(
            tool_call_id="",
            success=False,
            error=f"Guardrails: Action '{tool_name}' denied by user. Reason: {reason}",
        )
