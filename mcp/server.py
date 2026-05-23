"""
MCP Server: expose Weave capabilities as MCP tools (#512 P0).

Implements a lightweight MCP Server using JSON-RPC over stdio transport.
No external MCP SDK dependency — uses the protocol directly.

Tool registration pattern::

    server = MCPServer("weave")

    @server.tool("weave.plan", description="Plan a DAG from requirements")
    def weave_plan(task_description: str, project: str | None = None) -> dict:
        ...

    asyncio.run(server.run())

Protocol:
- Receives JSON-RPC requests on stdin
- Sends JSON-RPC responses on stdout
- Handles: initialize, tools/list, tools/call
- Logs to stderr (never stdout — that's for JSON-RPC)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class ToolDefinition:
    """Registered MCP tool."""

    name: str
    description: str
    handler: Callable[..., Any]
    input_schema: dict[str, Any] = field(default_factory=dict)


class MCPServer:
    """Lightweight MCP Server using JSON-RPC over stdio (#512 P0).

    Usage::

        server = MCPServer("weave")

        @server.tool("weave.echo", description="Echo input")
        def echo(text: str) -> dict:
            return {"content": [{"type": "text", "text": text}]}

        asyncio.run(server.run())

    The server reads JSON-RPC from stdin, routes to handlers,
    and writes responses to stdout. All logging goes to stderr.
    """

    PROTOCOL_VERSION = "2024-11-05"

    def __init__(self, name: str = "weave", version: str = "0.1.0") -> None:
        self._name = name
        self._version = version
        self._tools: dict[str, ToolDefinition] = {}
        self._running = False

    def tool(
        self,
        name: str,
        description: str = "",
        input_schema: dict[str, Any] | None = None,
    ) -> Callable:
        """Decorator to register an MCP tool handler.

        Args:
            name: Tool name (e.g. "weave.plan").
            description: Human-readable description.
            input_schema: JSON Schema for tool input parameters.
        """
        schema = input_schema or {
            "type": "object",
            "properties": {},
            "required": [],
        }

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self._tools[name] = ToolDefinition(
                name=name,
                description=description,
                handler=func,
                input_schema=schema,
            )
            logger.info("Registered MCP tool: %s", name)
            return func

        return decorator

    def register_tool(
        self,
        name: str,
        handler: Callable[..., Any],
        description: str = "",
        input_schema: dict[str, Any] | None = None,
    ) -> None:
        """Programmatically register an MCP tool (non-decorator API)."""
        schema = input_schema or {
            "type": "object",
            "properties": {},
            "required": [],
        }
        self._tools[name] = ToolDefinition(
            name=name,
            description=description,
            handler=handler,
            input_schema=schema,
        )

    async def run(self) -> None:
        """Start the MCP server: read JSON-RPC from stdin, respond on stdout."""
        self._running = True
        logger.info(
            "MCP Server '%s' v%s starting (stdio transport)",
            self._name,
            self._version,
        )

        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_running_loop().connect_read_pipe(
            lambda: protocol,
            sys.stdin,
        )

        while self._running:
            try:
                line = await reader.readline()
                if not line:
                    logger.info("MCP Server: stdin closed, shutting down")
                    break

                text = line.decode("utf-8").strip()
                if not text:
                    continue

                response = await self._handle_message(text)
                if response is not None:
                    self._write_response(response)

            except Exception as exc:
                logger.error("MCP Server error: %s", exc, exc_info=True)

    async def _handle_message(self, text: str) -> dict | None:
        """Parse and route a JSON-RPC message."""
        try:
            message = json.loads(text)
        except json.JSONDecodeError as exc:
            return self._error_response(None, -32700, f"Parse error: {exc}")

        method = message.get("method")
        params = message.get("params", {})
        msg_id = message.get("id")

        # Notifications (no id) — no response needed
        if method == "notifications/cancelled":
            logger.debug("Received cancellation notification")
            return None

        if method == "notifications/initialized":
            logger.debug("Client initialized notification received")
            return None

        # Request/response methods
        handlers = {
            "initialize": self._handle_initialize,
            "tools/list": self._handle_tools_list,
            "tools/call": self._handle_tools_call,
        }

        handler = handlers.get(method)
        if handler is None:
            return self._error_response(
                msg_id, -32601, f"Method not found: {method}",
            )

        try:
            result = await handler(params)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": result,
            }
        except Exception as exc:
            logger.error("Handler error for %s: %s", method, exc, exc_info=True)
            return self._error_response(
                msg_id, -32603, f"Internal error: {exc}",
            )

    async def _handle_initialize(self, params: dict) -> dict:
        """Handle MCP initialize request."""
        return {
            "protocolVersion": self.PROTOCOL_VERSION,
            "capabilities": {
                "tools": {"listChanged": False},
            },
            "serverInfo": {
                "name": self._name,
                "version": self._version,
            },
        }

    async def _handle_tools_list(self, params: dict) -> dict:
        """Handle tools/list request."""
        tools = []
        for td in self._tools.values():
            tools.append({
                "name": td.name,
                "description": td.description,
                "inputSchema": td.input_schema,
            })
        return {"tools": tools}

    async def _handle_tools_call(self, params: dict) -> dict:
        """Handle tools/call request."""
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        td = self._tools.get(tool_name)
        if td is None:
            return {
                "content": [
                    {"type": "text", "text": f"Unknown tool: {tool_name}"},
                ],
                "isError": True,
            }

        try:
            result = td.handler(**arguments)

            # Handler may return dict or coroutine
            if asyncio.iscoroutine(result):
                result = await result

            # Normalize result to MCP content format
            if isinstance(result, dict) and "content" in result:
                return result

            # Auto-wrap string/dict results
            if isinstance(result, str):
                return {
                    "content": [{"type": "text", "text": result}],
                }
            if isinstance(result, dict):
                return {
                    "content": [
                        {"type": "text", "text": json.dumps(result, default=str)},
                    ],
                }

            return {
                "content": [{"type": "text", "text": str(result)}],
            }
        except Exception as exc:
            logger.error("Tool %s error: %s", tool_name, exc, exc_info=True)
            return {
                "content": [
                    {"type": "text", "text": f"Tool error: {exc}"},
                ],
                "isError": True,
            }

    def _write_response(self, response: dict) -> None:
        """Write a JSON-RPC response to stdout."""
        line = json.dumps(response, default=str) + "\n"
        sys.stdout.write(line)
        sys.stdout.flush()

    @staticmethod
    def _error_response(
        msg_id: str | int | None,
        code: int,
        message: str,
    ) -> dict:
        """Build a JSON-RPC error response."""
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": code, "message": message},
        }

    def stop(self) -> None:
        """Signal the server to stop."""
        self._running = False
