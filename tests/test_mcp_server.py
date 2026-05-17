"""Tests for MCP Server framework (#512 P0)."""
import json

import pytest

from mcp.server import MCPServer


class TestToolRegistration:
    """Verify tool registration via decorator and programmatic API."""

    def test_decorator_registers_tool(self):
        server = MCPServer("test")

        @server.tool("test.echo", description="Echo input")
        def echo(text: str) -> str:
            return text

        assert "test.echo" in server._tools
        td = server._tools["test.echo"]
        assert td.name == "test.echo"
        assert td.description == "Echo input"
        assert td.handler is echo

    def test_register_tool_programmatic(self):
        server = MCPServer("test")
        server.register_tool(
            "test.status",
            handler=lambda: "ok",
            description="Get status",
        )
        assert "test.status" in server._tools

    def test_tool_default_schema(self):
        server = MCPServer("test")

        @server.tool("test.tool")
        def handler():
            pass

        schema = server._tools["test.tool"].input_schema
        assert schema["type"] == "object"

    def test_tool_custom_schema(self):
        server = MCPServer("test")
        custom_schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }

        @server.tool("test.tool", input_schema=custom_schema)
        def handler(name: str):
            pass

        assert server._tools["test.tool"].input_schema == custom_schema

    def test_multiple_tools(self):
        server = MCPServer("test")

        @server.tool("tool.a")
        def a():
            pass

        @server.tool("tool.b")
        def b():
            pass

        assert len(server._tools) == 2


class TestHandleInitialize:
    """Verify MCP initialize response."""

    @pytest.mark.asyncio
    async def test_initialize_response(self):
        server = MCPServer("weave", version="1.0.0")
        result = await server._handle_initialize({})
        assert result["protocolVersion"] == MCPServer.PROTOCOL_VERSION
        assert result["serverInfo"]["name"] == "weave"
        assert result["serverInfo"]["version"] == "1.0.0"
        assert "tools" in result["capabilities"]


class TestHandleToolsList:
    """Verify tools/list response."""

    @pytest.mark.asyncio
    async def test_empty_tools_list(self):
        server = MCPServer("test")
        result = await server._handle_tools_list({})
        assert result["tools"] == []

    @pytest.mark.asyncio
    async def test_tools_list_includes_registered(self):
        server = MCPServer("test")

        @server.tool("weave.plan", description="Plan a task")
        def plan(task: str):
            pass

        result = await server._handle_tools_list({})
        assert len(result["tools"]) == 1
        tool = result["tools"][0]
        assert tool["name"] == "weave.plan"
        assert tool["description"] == "Plan a task"
        assert "inputSchema" in tool


class TestHandleToolsCall:
    """Verify tools/call request handling."""

    @pytest.mark.asyncio
    async def test_call_existing_tool(self):
        server = MCPServer("test")

        @server.tool("test.echo")
        def echo(text: str) -> str:
            return text

        result = await server._handle_tools_call({
            "name": "test.echo",
            "arguments": {"text": "hello"},
        })
        assert result["content"][0]["text"] == "hello"
        assert "isError" not in result or not result["isError"]

    @pytest.mark.asyncio
    async def test_call_unknown_tool(self):
        server = MCPServer("test")
        result = await server._handle_tools_call({
            "name": "nonexistent",
            "arguments": {},
        })
        assert result["isError"] is True
        assert "Unknown tool" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_call_tool_dict_return(self):
        server = MCPServer("test")

        @server.tool("test.status")
        def status() -> dict:
            return {"content": [{"type": "text", "text": "ok"}]}

        result = await server._handle_tools_call({
            "name": "test.status",
            "arguments": {},
        })
        assert result["content"][0]["text"] == "ok"

    @pytest.mark.asyncio
    async def test_call_tool_auto_wrap_dict(self):
        server = MCPServer("test")

        @server.tool("test.info")
        def info() -> dict:
            return {"key": "value", "count": 42}

        result = await server._handle_tools_call({
            "name": "test.info",
            "arguments": {},
        })
        text = result["content"][0]["text"]
        parsed = json.loads(text)
        assert parsed["key"] == "value"

    @pytest.mark.asyncio
    async def test_call_tool_exception(self):
        server = MCPServer("test")

        @server.tool("test.fail")
        def fail():
            raise ValueError("intentional error")

        result = await server._handle_tools_call({
            "name": "test.fail",
            "arguments": {},
        })
        assert result["isError"] is True
        assert "intentional error" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_call_async_handler(self):
        server = MCPServer("test")

        @server.tool("test.async_echo")
        async def async_echo(text: str) -> str:
            return text

        result = await server._handle_tools_call({
            "name": "test.async_echo",
            "arguments": {"text": "async hello"},
        })
        assert result["content"][0]["text"] == "async hello"


class TestMessageRouting:
    """Verify JSON-RPC message routing."""

    @pytest.mark.asyncio
    async def test_valid_initialize_request(self):
        server = MCPServer("test")
        msg = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {},
        })
        response = await server._handle_message(msg)
        assert response["id"] == 1
        assert "result" in response
        assert "error" not in response

    @pytest.mark.asyncio
    async def test_method_not_found(self):
        server = MCPServer("test")
        msg = json.dumps({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "nonexistent/method",
            "params": {},
        })
        response = await server._handle_message(msg)
        assert response["id"] == 2
        assert response["error"]["code"] == -32601

    @pytest.mark.asyncio
    async def test_invalid_json(self):
        server = MCPServer("test")
        response = await server._handle_message("not json{{{")
        assert response["error"]["code"] == -32700

    @pytest.mark.asyncio
    async def test_notification_no_response(self):
        server = MCPServer("test")
        msg = json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        })
        response = await server._handle_message(msg)
        assert response is None

    @pytest.mark.asyncio
    async def test_tools_call_routing(self):
        server = MCPServer("test")

        @server.tool("test.add")
        def add(a: int, b: int) -> int:
            return a + b

        msg = json.dumps({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "test.add",
                "arguments": {"a": 2, "b": 3},
            },
        })
        response = await server._handle_message(msg)
        assert response["result"]["content"][0]["text"] == "5"


class TestErrorResponse:
    """Verify error response format."""

    def test_error_response_structure(self):
        resp = MCPServer._error_response(42, -32600, "Invalid request")
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 42
        assert resp["error"]["code"] == -32600
        assert resp["error"]["message"] == "Invalid request"

    def test_error_response_null_id(self):
        resp = MCPServer._error_response(None, -32700, "Parse error")
        assert resp["id"] is None


class TestServerStop:
    """Verify server can be stopped."""

    def test_stop_sets_running_false(self):
        server = MCPServer("test")
        server._running = True
        server.stop()
        assert server._running is False
