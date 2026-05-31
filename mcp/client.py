"""
MCPClient: connect to MCP servers, discover tools, execute tool calls.

Uses the official mcp Python SDK as a client with stdio transport.
Each MCP server is spawned as a subprocess and communicates via
JSON-RPC over stdin/stdout.

Lifecycle: connect -> discover -> register -> execute -> disconnect
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import AsyncExitStack
from typing import Any

from core.config import MCPServerConfig
from core.exceptions import MCPError
from core.models import MCPToolInfo, MCPServerStatus
from monitoring.otel import inject_trace_context

logger = logging.getLogger(__name__)

_PREFIX_SEP = "__"


class MCPServerConnection:
    """Manages a single MCP server connection via stdio transport."""

    def __init__(self, config: MCPServerConfig, timeout: int = 30) -> None:
        self.config = config
        self.timeout = timeout
        self.status = MCPServerStatus.DISCONNECTED
        self._session: Any = None
        self._cm_stack: AsyncExitStack | None = None
        self._discovered_tools: list[MCPToolInfo] = []

    async def connect(self) -> None:
        """Spawn MCP server process and establish JSON-RPC connection."""
        if self.status == MCPServerStatus.CONNECTED:
            return

        self.status = MCPServerStatus.CONNECTING
        try:
            from mcp.client.stdio import stdio_client, StdioServerParameters
            from mcp import ClientSession

            # Build minimal env: PATH + explicit config vars only (#413 review).
            # Avoids leaking secrets (ANTHROPIC_API_KEY etc.) to subprocesses.
            base_env = {
                k: v
                for k, v in os.environ.items()
                if k in ("PATH", "HOME", "LANG", "TERM", "SHELL", "USER")
            }
            env = {**base_env, **self.config.env} if self.config.env else base_env
            # #939: Inject W3C traceparent for distributed trace correlation
            env = inject_trace_context(env)
            server_params = StdioServerParameters(
                command=self.config.command,
                args=self.config.args,
                env=env,
            )

            self._cm_stack = AsyncExitStack()
            read_stream, write_stream = await self._cm_stack.enter_async_context(
                stdio_client(server_params)
            )

            session = ClientSession(read_stream, write_stream)
            self._session = await self._cm_stack.enter_async_context(session)
            await self._session.initialize()

            self.status = MCPServerStatus.CONNECTED
            logger.info(
                "MCP server '%s' connected (command: %s %s)",
                self.config.name,
                self.config.command,
                " ".join(self.config.args),
            )
        except Exception as e:
            self.status = MCPServerStatus.ERROR
            logger.error("MCP server '%s' connection failed: %s", self.config.name, e)
            raise MCPError(self.config.name, "connect") from e

    async def discover_tools(self) -> list[MCPToolInfo]:
        """List tools available on this server."""
        if self.status != MCPServerStatus.CONNECTED or self._session is None:
            logger.warning(
                "Cannot discover tools from '%s': not connected",
                self.config.name,
            )
            return []

        try:
            result = await asyncio.wait_for(
                self._session.list_tools(),
                timeout=self.timeout,
            )
            self._discovered_tools = []
            for tool in result.tools:
                prefixed = make_prefixed_name(self.config.name, tool.name)
                info = MCPToolInfo(
                    prefixed_name=prefixed,
                    original_name=tool.name,
                    server_name=self.config.name,
                    description=tool.description or "",
                    input_schema=(
                        tool.inputSchema if hasattr(tool, "inputSchema") else {}
                    ),
                )
                self._discovered_tools.append(info)
                logger.info("Discovered MCP tool: %s", prefixed)
            return list(self._discovered_tools)
        except Exception as e:
            logger.error(
                "Failed to discover tools from '%s': %s", self.config.name, e
            )
            return []

    async def call_tool(self, tool_name: str, arguments: dict) -> dict[str, Any]:
        """Execute a tool call on this server."""
        if self.status != MCPServerStatus.CONNECTED or self._session is None:
            return {
                "is_error": True,
                "content": f"Server '{self.config.name}' not connected",
            }

        try:
            result = await asyncio.wait_for(
                self._session.call_tool(tool_name, arguments),
                timeout=self.timeout,
            )
            content_parts: list[str] = []
            for block in result.content if hasattr(result, "content") else []:
                if hasattr(block, "text"):
                    content_parts.append(block.text)
                else:
                    content_parts.append(str(block))

            return {
                "is_error": result.isError if hasattr(result, "isError") else False,
                "content": "\n".join(content_parts) if content_parts else str(result),
            }
        except Exception as e:
            self.status = MCPServerStatus.ERROR
            logger.error(
                "MCP tool call '%s' on '%s' failed: %s",
                tool_name,
                self.config.name,
                e,
            )
            return {"is_error": True, "content": str(e)}

    async def disconnect(self) -> None:
        """Gracefully shut down the server connection."""
        if self._cm_stack is not None:
            try:
                await self._cm_stack.aclose()
            except Exception as e:
                logger.warning(
                    "Error disconnecting MCP server '%s': %s",
                    self.config.name,
                    e,
                )
        self._session = None
        self._cm_stack = None
        self.status = MCPServerStatus.DISCONNECTED
        logger.info("MCP server '%s' disconnected", self.config.name)


class MCPClient:
    """Facade for managing multiple MCP server connections.

    Usage::

        client = MCPClient(config.mcp)
        await client.connect_all()
        tools = client.discover_all_tools()
        # register tools in ToolRegistry...
        await client.disconnect_all()
    """

    def __init__(self, config: Any) -> None:
        self._config = config
        self._connections: dict[str, MCPServerConnection] = {}

    async def connect_all(self) -> int:
        """Connect to all enabled MCP servers. Returns count connected."""
        connected = 0
        for server_cfg in self._config.servers:
            if not server_cfg.enabled:
                logger.info("MCP server '%s' is disabled, skipping", server_cfg.name)
                continue
            conn = MCPServerConnection(
                server_cfg, timeout=self._config.connection_timeout
            )
            try:
                await conn.connect()
                self._connections[server_cfg.name] = conn
                connected += 1
            except Exception as e:
                logger.error("Skipping MCP server '%s': %s", server_cfg.name, e)
        return connected

    async def discover_all_tools(self) -> list[MCPToolInfo]:
        """Return discovered tools from all connected servers."""
        all_tools: list[MCPToolInfo] = []
        for name, conn in self._connections.items():
            if conn.status == MCPServerStatus.CONNECTED:
                try:
                    tools = await conn.discover_tools()
                    all_tools.extend(tools)
                except Exception as e:
                    logger.error(
                        "Failed to discover tools from '%s': %s", name, e
                    )
        return all_tools

    async def call_tool(self, prefixed_name: str, arguments: dict) -> dict[str, Any]:
        """Route a prefixed tool name to the correct server."""
        parsed = parse_prefixed_name(prefixed_name)
        if parsed is None:
            return {
                "is_error": True,
                "content": f"Invalid MCP tool name: {prefixed_name}",
            }
        server_name, tool_name = parsed
        conn = self._connections.get(server_name)
        if conn is None:
            return {
                "is_error": True,
                "content": f"MCP server '{server_name}' not found",
            }
        return await conn.call_tool(tool_name, arguments)

    def call_tool_sync(self, prefixed_name: str, arguments: dict) -> dict[str, Any]:
        """Synchronous wrapper for call_tool."""
        return _run_async(self.call_tool(prefixed_name, arguments))

    async def disconnect_all(self) -> None:
        """Disconnect from all servers."""
        for conn in self._connections.values():
            await conn.disconnect()
        self._connections.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_prefixed_name(server_name: str, tool_name: str) -> str:
    """Generate prefixed tool name: mcp__servername__toolname."""
    return f"mcp{_PREFIX_SEP}{server_name}{_PREFIX_SEP}{tool_name}"


def parse_prefixed_name(prefixed: str) -> tuple[str, str] | None:
    """Parse 'mcp__server__tool' into (server, tool). Returns None if not MCP."""
    if not prefixed.startswith(f"mcp{_PREFIX_SEP}"):
        return None
    rest = prefixed[len("mcp") + len(_PREFIX_SEP):]
    parts = rest.split(_PREFIX_SEP, 1)
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def _run_async(coro: Any) -> Any:
    """Run an async coroutine from sync context."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result(timeout=120)
    else:
        return asyncio.run(coro)
