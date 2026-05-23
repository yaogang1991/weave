"""Tests for MCP analysis tools (M4.3)."""

from mcp.server import MCPServer


def _server_with_analysis_tools():
    from mcp.analysis_tools import register_analysis_tools

    server = MCPServer("test")
    register_analysis_tools(server)
    return server


class TestDependencyGraphTool:
    """Verify weave.dependency_graph MCP tool."""

    def test_registered(self):
        server = _server_with_analysis_tools()
        assert "weave.dependency_graph" in server._tools

    def test_full_graph_on_self(self):
        server = _server_with_analysis_tools()
        tool = server._tools["weave.dependency_graph"]
        result = tool.handler(project=".")
        assert isinstance(result, dict)
        assert "graph" in result
        assert result["files"] > 0
        assert result["edges"] >= 0

    def test_query_specific_file(self):
        server = _server_with_analysis_tools()
        tool = server._tools["weave.dependency_graph"]
        result = tool.handler(
            project=".",
            file="core/config.py",
            direction="dependencies",
            depth="direct",
        )
        assert isinstance(result, dict)
        assert result["file"] == "core/config.py"
        assert "dependencies" in result

    def test_bad_project_path(self):
        server = _server_with_analysis_tools()
        tool = server._tools["weave.dependency_graph"]
        result = tool.handler(project="/nonexistent/path")
        assert "error" in result
        assert result.get("isError") is True

    def test_invalid_direction(self):
        server = _server_with_analysis_tools()
        tool = server._tools["weave.dependency_graph"]
        result = tool.handler(project=".", direction="invalid")
        assert "error" in result
        assert result.get("isError") is True
        assert "Invalid direction" in result["error"]

    def test_invalid_depth(self):
        server = _server_with_analysis_tools()
        tool = server._tools["weave.dependency_graph"]
        result = tool.handler(project=".", depth="invalid")
        assert "error" in result
        assert result.get("isError") is True
        assert "Invalid depth" in result["error"]

    def test_path_traversal_rejected(self):
        server = _server_with_analysis_tools()
        tool = server._tools["weave.dependency_graph"]
        result = tool.handler(project="/etc")
        assert "error" in result
        assert result.get("isError") is True
        assert "not allowed" in result["error"]

    def test_has_input_schema(self):
        server = _server_with_analysis_tools()
        tool = server._tools["weave.dependency_graph"]
        assert "project" in tool.input_schema["properties"]
        assert "file" in tool.input_schema["properties"]


class TestImpactPredictTool:
    """Verify weave.impact_predict MCP tool."""

    def test_registered(self):
        server = _server_with_analysis_tools()
        assert "weave.impact_predict" in server._tools

    def test_predict_on_self(self):
        server = _server_with_analysis_tools()
        tool = server._tools["weave.impact_predict"]
        result = tool.handler(
            requirement="fix bug in DAG engine",
            project=".",
        )
        assert isinstance(result, dict)
        assert "predicted_files" in result
        assert "risk_level" in result
        assert "confidence" in result
        assert result["risk_level"] in ("low", "medium", "high", "critical")

    def test_bad_project_path(self):
        server = _server_with_analysis_tools()
        tool = server._tools["weave.impact_predict"]
        result = tool.handler(
            requirement="fix bug",
            project="/nonexistent/path",
        )
        assert "error" in result
        assert result.get("isError") is True

    def test_has_input_schema(self):
        server = _server_with_analysis_tools()
        tool = server._tools["weave.impact_predict"]
        assert "requirement" in tool.input_schema["properties"]
        assert "requirement" in tool.input_schema.get("required", [])


class TestImpactGraphTool:
    """Verify weave.impact_graph MCP tool."""

    def test_registered(self):
        server = _server_with_analysis_tools()
        assert "weave.impact_graph" in server._tools

    def test_snapshot_on_self(self):
        server = _server_with_analysis_tools()
        tool = server._tools["weave.impact_graph"]
        result = tool.handler(project=".")
        assert isinstance(result, dict)
        # Large repos may exceed the file limit and return a truncation message
        if "truncated" in result and result["truncated"]:
            assert "message" in result
            assert result["tracked_files"] > 0
        else:
            assert "files" in result
            assert result["tracked_files"] > 0
            # Each file entry has path, mtime, size
            first_file = result["files"][0]
            assert "path" in first_file
            assert "mtime" in first_file
            assert "size" in first_file

    def test_bad_project_path(self):
        server = _server_with_analysis_tools()
        tool = server._tools["weave.impact_graph"]
        result = tool.handler(project="/nonexistent/path")
        assert "error" in result
        assert result.get("isError") is True

    def test_has_input_schema(self):
        server = _server_with_analysis_tools()
        tool = server._tools["weave.impact_graph"]
        assert "project" in tool.input_schema["properties"]


class TestAnalysisToolsIntegration:
    """Verify analysis tools are also registered via full Weave server."""

    def test_analysis_tools_in_full_server(self):
        from cli.mcp_tools import register_weave_tools

        server = MCPServer("test")
        register_weave_tools(server)

        analysis_tools = {
            "weave.dependency_graph",
            "weave.impact_predict",
            "weave.impact_graph",
        }
        actual = set(server._tools.keys())
        assert analysis_tools.issubset(actual), (
            f"Missing analysis tools: {analysis_tools - actual}"
        )

    def test_standalone_server_registers_tools(self):
        from mcp.analysis_tools import register_analysis_tools

        server = MCPServer("weave-analysis")
        register_analysis_tools(server)

        assert len(server._tools) == 3
        expected = {
            "weave.dependency_graph",
            "weave.impact_predict",
            "weave.impact_graph",
        }
        assert set(server._tools.keys()) == expected
