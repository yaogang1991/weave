"""Tests for MCP learning analysis tools (#512 P2)."""
from mcp.server import MCPServer


class TestWeaveAnalyzeTool:
    """Verify weave.analyze MCP tool."""

    def test_analyze_tool_registered(self):
        """weave.analyze tool is registered on the MCP server."""
        server = MCPServer("test")
        # Import to trigger registration side-effect
        # (tools are registered in register_weave_tools)
        # We test the handler directly
        from cli.mcp_tools import register_weave_tools
        register_weave_tools(server)
        assert "weave.analyze" in server._tools

    def test_analyze_handler_returns_dict(self):
        """weave.analyze handler returns a dict with insights key."""
        server = MCPServer("test")
        from cli.mcp_tools import register_weave_tools
        register_weave_tools(server)
        tool = server._tools["weave.analyze"]
        # The handler may fail without full config, but should return a dict
        result = tool.handler()
        assert isinstance(result, dict)
        assert "insights" in result

    def test_analyze_tool_has_schema(self):
        """weave.analyze tool has input schema."""
        server = MCPServer("test")
        from cli.mcp_tools import register_weave_tools
        register_weave_tools(server)
        tool = server._tools["weave.analyze"]
        assert tool.input_schema is not None
        assert "min_confidence" in tool.input_schema.get("properties", {})


class TestWeaveInsightsTool:
    """Verify weave.insights MCP tool."""

    def test_insights_tool_registered(self):
        """weave.insights tool is registered on the MCP server."""
        server = MCPServer("test")
        from cli.mcp_tools import register_weave_tools
        register_weave_tools(server)
        assert "weave.insights" in server._tools

    def test_insights_handler_returns_dict(self):
        """weave.insights handler returns a dict with hints key."""
        server = MCPServer("test")
        from cli.mcp_tools import register_weave_tools
        register_weave_tools(server)
        tool = server._tools["weave.insights"]
        result = tool.handler()
        assert isinstance(result, dict)
        assert "hints" in result

    def test_insights_tool_has_schema(self):
        """weave.insights tool has input schema."""
        server = MCPServer("test")
        from cli.mcp_tools import register_weave_tools
        register_weave_tools(server)
        tool = server._tools["weave.insights"]
        assert tool.input_schema is not None
        assert "requirement" in tool.input_schema.get("properties", {})


class TestAllWeaveToolsRegistered:
    """Verify all Weave MCP tools are present."""

    def test_expected_tools_registered(self):
        """All expected Weave tools are registered."""
        server = MCPServer("test")
        from cli.mcp_tools import register_weave_tools
        register_weave_tools(server)
        expected = {
            "weave.plan",
            "weave.run",
            "weave.status",
            "weave.list",
            "weave.memory_query",
            "weave.health",
            "weave.analyze",
            "weave.insights",
        }
        actual = set(server._tools.keys())
        assert expected.issubset(actual), (
            f"Missing tools: {expected - actual}"
        )
