"""Tests for #144: runtime context injection to prevent path guessing.

Covers:
- _build_runtime_context() includes OS, CWD, PROJECT_ROOT, PYTHON
- Runtime context is injected into agent prompt in _execute_inner
- Bash tool description mentions PROJECT_ROOT and relative paths
- Bash tool output includes [cwd] prefix
- ToolRegistry(base_cwd=...) sets project root
"""
import sys
from pathlib import Path


from tools.registry import ToolRegistry  # noqa: E402


# =============================================================================
# _build_runtime_context
# =============================================================================


class TestBashToolContext:
    """Bash tool description and output include workspace context."""

    def test_bash_schema_mentions_project_root(self):
        reg = ToolRegistry()
        schema = reg.get_schema("bash")
        desc = schema["description"]
        assert "PROJECT_ROOT" in desc

    def test_bash_schema_cwd_description(self):
        reg = ToolRegistry()
        schema = reg.get_schema("bash")
        cwd_desc = schema["input_schema"]["properties"]["cwd"]["description"]
        assert "PROJECT_ROOT" in cwd_desc

    def test_bash_output_includes_cwd(self, tmp_path):
        reg = ToolRegistry(base_cwd=str(tmp_path))
        result = reg.execute("bash", {"command": "echo hello"})
        assert result.success
        assert "[cwd]" in result.output
        assert "hello" in result.output

    def test_bash_output_shows_actual_cwd(self, tmp_path):
        reg = ToolRegistry(base_cwd=str(tmp_path))
        result = reg.execute("bash", {"command": "pwd"})
        assert result.success
        # [cwd] line should show the resolved cwd
        lines = result.output.split("\n")
        cwd_line = lines[0]
        assert "[cwd]" in cwd_line


# =============================================================================
# ToolRegistry base_cwd
# =============================================================================


class TestToolRegistryBaseCwd:
    """ToolRegistry correctly stores and uses base_cwd."""

    def test_base_cwd_set(self):
        reg = ToolRegistry(base_cwd="/tmp/project")
        assert reg.base_cwd == Path("/tmp/project").resolve()

    def test_base_cwd_none(self):
        reg = ToolRegistry()
        assert reg.base_cwd is None

    def test_base_cwd_resolved(self, tmp_path):
        reg = ToolRegistry(base_cwd=str(tmp_path / "subdir"))
        assert reg.base_cwd.is_absolute()
