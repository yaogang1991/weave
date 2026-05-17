"""Tests for MCP guardrails risk mapping (M3.6)."""


from core.models import MCPToolInfo, GuardrailPolicy, RiskLevel, PermissionMode
from guardrails.policy import Guardrails
from tools.registry import ToolRegistry


class TestMCPRiskMapping:
    def _make_tool_info(self, name="mcp_tool", server="test"):
        return MCPToolInfo(
            prefixed_name=f"mcp__{server}__{name}",
            original_name=name,
            server_name=server,
            description="Test",
        )

    def test_default_medium_risk(self):
        registry = ToolRegistry()
        policy = GuardrailPolicy(mode=PermissionMode.ACCEPT_EDITS)
        guardrails = Guardrails(policy, registry)

        tools = [self._make_tool_info()]
        guardrails.register_mcp_risk_map(tools)

        assert guardrails.RISK_MAP["mcp__test__mcp_tool"] == RiskLevel.MEDIUM

    def test_custom_risk_level(self):
        registry = ToolRegistry()
        policy = GuardrailPolicy(mode=PermissionMode.ACCEPT_EDITS)
        guardrails = Guardrails(policy, registry)

        tools = [self._make_tool_info()]
        guardrails.register_mcp_risk_map(tools, default_risk=RiskLevel.LOW)

        assert guardrails.RISK_MAP["mcp__test__mcp_tool"] == RiskLevel.LOW

    def test_multiple_tools_registered(self):
        registry = ToolRegistry()
        policy = GuardrailPolicy(mode=PermissionMode.ACCEPT_EDITS)
        guardrails = Guardrails(policy, registry)

        tools = [
            self._make_tool_info("tool_a", "srv1"),
            self._make_tool_info("tool_b", "srv2"),
        ]
        guardrails.register_mcp_risk_map(tools)

        assert guardrails.RISK_MAP["mcp__srv1__tool_a"] == RiskLevel.MEDIUM
        assert guardrails.RISK_MAP["mcp__srv2__tool_b"] == RiskLevel.MEDIUM

    def test_unknown_mcp_tool_defaults_to_high(self):
        registry = ToolRegistry()
        policy = GuardrailPolicy(mode=PermissionMode.ACCEPT_EDITS)
        guardrails = Guardrails(policy, registry)

        result = guardrails.evaluate("mcp__unknown__tool", {})
        assert result.decision == "pending_approval"

    def test_mcp_tool_accepted_in_accept_edits_mode(self):
        registry = ToolRegistry()
        policy = GuardrailPolicy(mode=PermissionMode.ACCEPT_EDITS)
        guardrails = Guardrails(policy, registry)

        tools = [self._make_tool_info()]
        guardrails.register_mcp_risk_map(tools)

        result = guardrails.evaluate("mcp__test__mcp_tool", {})
        assert result.decision == "allowed"

    def test_empty_tools_list_no_error(self):
        registry = ToolRegistry()
        policy = GuardrailPolicy(mode=PermissionMode.ACCEPT_EDITS)
        guardrails = Guardrails(policy, registry)

        guardrails.register_mcp_risk_map([])
        assert "mcp__test__tool" not in guardrails.RISK_MAP

    def test_risk_map_instance_isolation(self):
        """RISK_MAP mutations must not leak across Guardrails instances (#413)."""
        policy = GuardrailPolicy(mode="accept_edits")
        registry = ToolRegistry()
        g1 = Guardrails(policy, registry)
        g2 = Guardrails(policy, registry)

        mcp_tool = self._make_tool_info("unique_tool")
        g1.register_mcp_risk_map([mcp_tool])

        assert "mcp__test__unique_tool" in g1.RISK_MAP
        assert "mcp__test__unique_tool" not in g2.RISK_MAP
