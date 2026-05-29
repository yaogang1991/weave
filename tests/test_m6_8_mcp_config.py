"""Tests for M6.8 MCP config passing to CLI backends."""
import json
import tempfile
from pathlib import Path


from core.config import MCPConfig, MCPServerConfig
from mcp.config_export import MCPConfigExporter


# -- MCPConfigExporter tests --


class TestMCPConfigExporterToMcpJson:
    """Tests for MCPConfigExporter.to_mcp_json."""

    def test_empty_servers_returns_empty_dict(self):
        config = MCPConfig(servers=[])
        result = MCPConfigExporter.to_mcp_json(config)
        assert result == {"mcpServers": {}}

    def test_single_server_with_all_fields(self):
        server = MCPServerConfig(
            name="github",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            env={"GITHUB_TOKEN": "ghp_xxx"},
        )
        config = MCPConfig(servers=[server])
        result = MCPConfigExporter.to_mcp_json(config)
        assert result == {
            "mcpServers": {
                "github": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-github"],
                    "env": {"GITHUB_TOKEN": "ghp_xxx"},
                },
            },
        }

    def test_single_server_command_only(self):
        server = MCPServerConfig(name="simple", command="python")
        config = MCPConfig(servers=[server])
        result = MCPConfigExporter.to_mcp_json(config)
        assert result == {
            "mcpServers": {
                "simple": {"command": "python"},
            },
        }

    def test_multiple_servers(self):
        servers = [
            MCPServerConfig(name="s1", command="cmd1", args=["a"]),
            MCPServerConfig(name="s2", command="cmd2", env={"K": "V"}),
        ]
        config = MCPConfig(servers=servers)
        result = MCPConfigExporter.to_mcp_json(config)
        assert len(result["mcpServers"]) == 2
        assert "s1" in result["mcpServers"]
        assert "s2" in result["mcpServers"]

    def test_server_with_empty_args_and_env_omits_them(self):
        server = MCPServerConfig(name="minimal", command="run", args=[], env={})
        config = MCPConfig(servers=[server])
        result = MCPConfigExporter.to_mcp_json(config)
        entry = result["mcpServers"]["minimal"]
        assert "args" not in entry
        assert "env" not in entry
        assert entry["command"] == "run"

    def test_disabled_servers_are_excluded(self):
        servers = [
            MCPServerConfig(name="active", command="cmd1"),
            MCPServerConfig(name="disabled", command="cmd2", enabled=False),
        ]
        config = MCPConfig(servers=servers)
        result = MCPConfigExporter.to_mcp_json(config)
        assert "active" in result["mcpServers"]
        assert "disabled" not in result["mcpServers"]


class TestMCPConfigExporterWriteConfig:
    """Tests for MCPConfigExporter.write_config."""

    def test_writes_mcp_json_file(self):
        server = MCPServerConfig(
            name="test_server",
            command="npx",
            args=["-y", "some-mcp-server"],
        )
        config = MCPConfig(servers=[server])

        with tempfile.TemporaryDirectory() as tmpdir:
            path = MCPConfigExporter.write_config(config, tmpdir)
            assert path is not None
            assert path.exists()
            assert path.name.startswith(".mcp-weave-")

            data = json.loads(path.read_text(encoding="utf-8"))
            assert "mcpServers" in data
            assert "test_server" in data["mcpServers"]

    def test_returns_none_for_empty_servers(self):
        config = MCPConfig(servers=[])
        with tempfile.TemporaryDirectory() as tmpdir:
            path = MCPConfigExporter.write_config(config, tmpdir)
            assert path is None

    def test_accepts_path_object(self):
        server = MCPServerConfig(name="p", command="c")
        config = MCPConfig(servers=[server])

        with tempfile.TemporaryDirectory() as tmpdir:
            path = MCPConfigExporter.write_config(config, Path(tmpdir))
            assert path is not None

    def test_file_content_is_valid_json(self):
        server = MCPServerConfig(
            name="github",
            command="npx",
            args=["-y", "@mcp/server"],
            env={"TOKEN": "secret"},
        )
        config = MCPConfig(servers=[server])

        with tempfile.TemporaryDirectory() as tmpdir:
            path = MCPConfigExporter.write_config(config, tmpdir)
            assert path is not None
            content = path.read_text(encoding="utf-8")
            parsed = json.loads(content)
            assert parsed["mcpServers"]["github"]["command"] == "npx"


class TestMCPConfigExporterCleanupConfig:
    """Tests for MCPConfigExporter.cleanup_config."""

    def test_removes_existing_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / ".mcp.json"
            f.write_text("{}", encoding="utf-8")
            assert f.exists()

            MCPConfigExporter.cleanup_config(f)
            assert not f.exists()

    def test_handles_none_path(self):
        # Should not raise
        MCPConfigExporter.cleanup_config(None)

    def test_handles_nonexistent_path(self):
        # Should not raise
        MCPConfigExporter.cleanup_config(Path("/nonexistent/.mcp.json"))

    def test_handles_unlink_failure_gracefully(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "readonly" / ".mcp.json"
            # Parent dir doesn't exist, so unlink would fail
            # Should not raise
            MCPConfigExporter.cleanup_config(f)


# -- ClaudeCodeRuntimeConfig mcp_config tests --


class TestClaudeCodeRuntimeConfigMCP:
    """Tests for mcp_config support in ClaudeCodeRuntimeConfig."""

    def test_default_mcp_config_is_none(self):
        from agent.backends.claude_code import ClaudeCodeRuntimeConfig
        cfg = ClaudeCodeRuntimeConfig()
        assert cfg.mcp_config is None

    def test_mcp_config_stored(self):
        from agent.backends.claude_code import ClaudeCodeRuntimeConfig
        mcp = MCPConfig(servers=[
            MCPServerConfig(name="s", command="c"),
        ])
        cfg = ClaudeCodeRuntimeConfig(mcp_config=mcp)
        assert cfg.mcp_config is mcp

    def test_from_core_config_without_mcp_config(self):
        from agent.backends.claude_code import ClaudeCodeRuntimeConfig
        from core.config import ClaudeCodeConfig as CoreConfig
        core = CoreConfig()
        cfg = ClaudeCodeRuntimeConfig.from_core_config(core)
        assert cfg.mcp_config is None


# -- _build_cli_command MCP config tests --


class TestBuildCLICommandMCPConfig:
    """Tests for --mcp-config flag in _build_cli_command."""

    def _make_backend(self, **kwargs):
        from agent.backends.claude_code import (
            ClaudeCodeBackend,
            ClaudeCodeRuntimeConfig,
        )
        cfg = ClaudeCodeRuntimeConfig(**kwargs)
        return ClaudeCodeBackend(config=cfg)

    def _make_context(self, session_id: str = ""):
        from core.backend_models import BackendContext
        from core.dag_models import DAGNode
        node = DAGNode(
            id="n1", agent_type="generator", task_description="test",
        )
        return BackendContext(
            node=node, session_id=session_id, workspace_path="/tmp",
        )

    def test_no_mcp_config_flag_when_path_is_none(self):
        backend = self._make_backend()
        ctx = self._make_context()
        cmd = backend._build_cli_command(ctx, "hello")
        assert "--mcp-config" not in cmd

    def test_mcp_config_flag_included_when_path_provided(self):
        backend = self._make_backend()
        ctx = self._make_context()
        cmd = backend._build_cli_command(ctx, "hello", mcp_config_path="/tmp/.mcp.json")
        assert "--mcp-config" in cmd
        idx = cmd.index("--mcp-config")
        assert cmd[idx + 1] == "/tmp/.mcp.json"

    def test_mcp_config_flag_with_empty_path_not_included(self):
        backend = self._make_backend()
        ctx = self._make_context()
        cmd = backend._build_cli_command(ctx, "hello", mcp_config_path="")
        assert "--mcp-config" not in cmd

    def test_mcp_config_flag_position_after_session_id(self):
        backend = self._make_backend()
        ctx = self._make_context(session_id="sess_abc")
        cmd = backend._build_cli_command(ctx, "hello", mcp_config_path="/path/.mcp.json")
        sid_idx = cmd.index("--session-id")
        mcp_idx = cmd.index("--mcp-config")
        assert mcp_idx > sid_idx
