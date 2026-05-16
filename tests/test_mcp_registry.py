"""Tests for MCP tool registration in ToolRegistry (M3.6)."""

import pytest
from unittest.mock import MagicMock

from core.models import MCPToolInfo, ToolResult
from tools.registry import ToolRegistry


class TestRegisterMCPTools:
    def _make_tool_info(self, name="test_tool", server="test_server"):
        return MCPToolInfo(
            prefixed_name=f"mcp__{server}__{name}",
            original_name=name,
            server_name=server,
            description=f"Test tool {name}",
            input_schema={"type": "object", "properties": {"arg1": {"type": "string"}}},
        )

    def test_register_single_mcp_tool(self):
        registry = ToolRegistry()
        client = MagicMock()
        client.call_tool_sync.return_value = {"is_error": False, "content": "ok"}

        tools = [self._make_tool_info()]
        count = registry.register_mcp_tools(client, tools)
        assert count == 1
        assert "mcp__test_server__test_tool" in registry._tools

    def test_register_multiple_mcp_tools(self):
        registry = ToolRegistry()
        client = MagicMock()
        client.call_tool_sync.return_value = {"is_error": False, "content": "ok"}

        tools = [
            self._make_tool_info("tool_a", "server1"),
            self._make_tool_info("tool_b", "server1"),
            self._make_tool_info("tool_c", "server2"),
        ]
        count = registry.register_mcp_tools(client, tools)
        assert count == 3

    def test_mcp_tool_execution_success(self):
        registry = ToolRegistry()
        client = MagicMock()
        client.call_tool_sync.return_value = {"is_error": False, "content": "tool output"}

        tools = [self._make_tool_info()]
        registry.register_mcp_tools(client, tools)

        result = registry.execute("mcp__test_server__test_tool", {"arg1": "value"})
        assert isinstance(result, ToolResult)
        assert result.success is True
        assert "tool output" in result.output

    def test_mcp_tool_execution_error(self):
        registry = ToolRegistry()
        client = MagicMock()
        client.call_tool_sync.return_value = {"is_error": True, "content": "something went wrong"}

        tools = [self._make_tool_info()]
        registry.register_mcp_tools(client, tools)

        result = registry.execute("mcp__test_server__test_tool", {"arg1": "value"})
        assert isinstance(result, ToolResult)
        assert result.success is False
        assert "something went wrong" in result.error

    def test_mcp_tool_execution_exception(self):
        registry = ToolRegistry()
        client = MagicMock()
        client.call_tool_sync.side_effect = ConnectionError("server down")

        tools = [self._make_tool_info()]
        registry.register_mcp_tools(client, tools)

        result = registry.execute("mcp__test_server__test_tool", {"arg1": "value"})
        assert isinstance(result, ToolResult)
        assert result.success is False
        assert "server down" in result.error

    def test_mcp_tool_schema_registered(self):
        registry = ToolRegistry()
        client = MagicMock()
        client.call_tool_sync.return_value = {"is_error": False, "content": "ok"}

        tools = [self._make_tool_info()]
        registry.register_mcp_tools(client, tools)

        schema = registry.get_schema("mcp__test_server__test_tool")
        assert schema is not None
        assert schema["name"] == "mcp__test_server__test_tool"
        assert "[MCP:test_server]" in schema["description"]

    def test_mcp_tool_not_found(self):
        registry = ToolRegistry()
        result = registry.execute("mcp__nonexistent__tool", {})
        assert result.success is False
        assert "not found" in result.error

    def test_register_empty_tools_list(self):
        registry = ToolRegistry()
        client = MagicMock()
        count = registry.register_mcp_tools(client, [])
        assert count == 0
