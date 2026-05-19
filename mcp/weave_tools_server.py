"""Standalone MCP Server exposing Weave analysis tools (M4.3).

Run directly or configure in Claude Code's ``.mcp.json``::

    python -m mcp.weave_tools_server

Or add to your project's ``.mcp.json``::

    {
      "mcpServers": {
        "weave-analysis": {
          "command": "python",
          "args": ["-m", "mcp.weave_tools_server"],
          "cwd": "/path/to/project"
        }
      }
    }
"""

from __future__ import annotations

import asyncio
import sys

from mcp.analysis_tools import register_analysis_tools
from mcp.server import MCPServer


def main() -> None:
    server = MCPServer(name="weave-analysis", version="0.1.0")
    register_analysis_tools(server)

    print(
        f"Weave Analysis MCP Server — {len(server._tools)} tools: "
        f"{', '.join(server._tools.keys())}",
        file=sys.stderr,
    )
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
