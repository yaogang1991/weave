"""Tests for MCP memory_store tool (#512 P1)."""
from mcp.server import MCPServer


class TestWeaveMemoryStoreTool:
    """Verify weave.memory_store MCP tool."""

    def test_memory_store_tool_registered(self):
        """weave.memory_store tool is registered on the MCP server."""
        server = MCPServer("test")
        from cli.mcp_tools import register_weave_tools
        register_weave_tools(server)
        assert "weave.memory_store" in server._tools

    def test_memory_store_handler_returns_dict(self):
        """weave.memory_store handler returns a dict."""
        server = MCPServer("test")
        from cli.mcp_tools import register_weave_tools
        register_weave_tools(server)
        tool = server._tools["weave.memory_store"]
        result = tool.handler(content="test memory entry")
        assert isinstance(result, dict)
        assert "stored" in result

    def test_memory_store_tool_has_schema(self):
        """weave.memory_store tool has input schema with required content."""
        server = MCPServer("test")
        from cli.mcp_tools import register_weave_tools
        register_weave_tools(server)
        tool = server._tools["weave.memory_store"]
        assert tool.input_schema is not None
        assert "content" in tool.input_schema.get("properties", {})
        assert "content" in tool.input_schema.get("required", [])

    def test_memory_store_tool_has_optional_params(self):
        """weave.memory_store tool has optional agent_type and scope."""
        server = MCPServer("test")
        from cli.mcp_tools import register_weave_tools
        register_weave_tools(server)
        tool = server._tools["weave.memory_store"]
        props = tool.input_schema.get("properties", {})
        assert "agent_type" in props
        assert "scope" in props


class TestAllWeaveToolsWithMemoryStore:
    """Verify all Weave MCP tools including memory_store."""

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
            "weave.memory_store",
            "weave.health",
        }
        actual = set(server._tools.keys())
        assert expected.issubset(actual), (
            f"Missing tools: {expected - actual}"
        )
