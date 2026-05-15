"""Tests for generator file path enforcement and ownership checks (#291, #297)."""
import os
import tempfile

import pytest

from tools.registry import ToolRegistry
from core.models import DAGNode, ToolResult


class TestOwnershipEnforcement:
    """Verify tools enforce forbidden file writes."""

    @pytest.fixture
    def workspace(self, tmp_path):
        """Create a temp workspace with base_cwd."""
        return tmp_path / "project"

    @pytest.fixture
    def registry(self, workspace):
        workspace.mkdir(parents=True, exist_ok=True)
        return ToolRegistry(base_cwd=str(workspace))

    def test_write_to_owned_file_succeeds(self, registry, workspace):
        """Write to a file in owned_files succeeds."""
        registry.set_ownership_context({
            "owned": ["src/a.py"],
            "forbidden": [],
            "shared": [],
        })
        result = registry.execute("write", {"file_path": "src/a.py", "content": "hello"})
        assert result.success

    def test_write_to_forbidden_file_blocked(self, registry, workspace):
        """Write to a file in forbidden_files returns error."""
        registry.set_ownership_context({
            "owned": ["src/a.py"],
            "forbidden": ["src/b.py"],
            "shared": [],
        })
        result = registry.execute("write", {"file_path": "src/b.py", "content": "hello"})
        assert not result.success
        assert "forbidden" in result.error

    def test_write_with_no_contract_allowed(self, registry, workspace):
        """Write with no ownership context succeeds (serialization fallback)."""
        registry.set_ownership_context(None)
        result = registry.execute("write", {"file_path": "src/free.py", "content": "hello"})
        assert result.success

    def test_edit_to_forbidden_file_blocked(self, registry, workspace):
        """Edit tool also respects forbidden files."""
        # Create the file first
        (workspace / "src").mkdir(parents=True, exist_ok=True)
        (workspace / "src" / "b.py").write_text("original")
        registry.set_ownership_context({
            "owned": ["src/a.py"],
            "forbidden": ["src/b.py"],
            "shared": [],
        })
        result = registry.execute("edit", {
            "file_path": "src/b.py",
            "old_string": "original",
            "new_string": "modified",
        })
        assert not result.success
        assert "forbidden" in result.error

    def test_edit_to_owned_file_succeeds(self, registry, workspace):
        """Edit to owned file succeeds."""
        (workspace / "src").mkdir(parents=True, exist_ok=True)
        (workspace / "src" / "a.py").write_text("original")
        registry.set_ownership_context({
            "owned": ["src/a.py"],
            "forbidden": ["src/b.py"],
            "shared": [],
        })
        result = registry.execute("edit", {
            "file_path": "src/a.py",
            "old_string": "original",
            "new_string": "modified",
        })
        assert result.success

    def test_read_not_blocked_by_ownership(self, registry, workspace):
        """Read is never blocked by ownership (only write/edit)."""
        (workspace / "src").mkdir(parents=True, exist_ok=True)
        (workspace / "src" / "b.py").write_text("content")
        registry.set_ownership_context({
            "owned": ["src/a.py"],
            "forbidden": ["src/b.py"],
            "shared": [],
        })
        result = registry.execute("read", {"file_path": "src/b.py"})
        assert result.success

    def test_write_to_unrelated_file_succeeds(self, registry, workspace):
        """Write to a file not in forbidden list succeeds."""
        registry.set_ownership_context({
            "owned": ["src/a.py"],
            "forbidden": ["src/b.py"],
            "shared": [],
        })
        result = registry.execute("write", {"file_path": "src/c.py", "content": "hello"})
        assert result.success


class TestDAGNodeOwnershipContext:
    """Verify DAGNode ownership fields work with agent pool."""

    def test_node_with_owned_files(self):
        node = DAGNode(
            id="g1",
            agent_type="generator",
            task_description="impl A",
            owned_files=["src/a.py", "src/__init__.py"],
        )
        assert node.owned_files == ["src/a.py", "src/__init__.py"]

    def test_node_without_owned_files(self):
        node = DAGNode(
            id="g1",
            agent_type="generator",
            task_description="impl A",
        )
        assert node.owned_files == []
