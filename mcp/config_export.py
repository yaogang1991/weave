"""MCP configuration export for CLI backends.

Exports MCP server configurations to .mcp.json format for use with
Claude Code CLI --mcp-config flag.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from core.config import MCPConfig, MCPServerConfig

logger = logging.getLogger(__name__)

__all__ = ["MCPConfigExporter"]


class MCPConfigExporter:
    """Export MCP server configurations to .mcp.json format."""

    @staticmethod
    def to_mcp_json(config: MCPConfig) -> dict[str, Any]:
        """Convert MCPConfig to .mcp.json format.

        The .mcp.json format expected by Claude Code CLI:
        {
            "mcpServers": {
                "server_name": {
                    "command": "...",
                    "args": [...],
                    "env": {...}
                }
            }
        }
        """
        servers: dict[str, Any] = {}
        for server in config.servers:
            server_config: dict[str, Any] = {
                "command": server.command,
            }
            if server.args:
                server_config["args"] = server.args
            if server.env:
                server_config["env"] = server.env
            servers[server.name] = server_config
        return {"mcpServers": servers}

    @staticmethod
    def write_config(config: MCPConfig, target_dir: str | Path) -> Path | None:
        """Write .mcp.json to the target directory.

        Returns the path to the written file, or None if no servers configured.
        """
        mcp_json = MCPConfigExporter.to_mcp_json(config)
        if not mcp_json.get("mcpServers"):
            return None

        target = Path(target_dir)
        config_path = target / ".mcp.json"
        config_path.write_text(json.dumps(mcp_json, indent=2), encoding="utf-8")
        logger.debug("Wrote MCP config to %s", config_path)
        return config_path

    @staticmethod
    def cleanup_config(config_path: Path | None) -> None:
        """Remove temporary .mcp.json file."""
        if config_path and config_path.exists():
            try:
                config_path.unlink()
                logger.debug("Cleaned up MCP config: %s", config_path)
            except OSError:
                logger.debug("Failed to cleanup MCP config: %s", config_path)
