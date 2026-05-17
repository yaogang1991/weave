"""Tests for path traversal prevention in ToolRegistry (#500)."""
import pytest

from tools.registry import ToolRegistry


class TestPathTraversalPrevention:
    """Verify _resolve_path always enforces workspace boundary (#500)."""

    def test_default_base_cwd_is_cwd(self):
        """When base_cwd is None, defaults to cwd instead of unrestricted."""
        from pathlib import Path

        registry = ToolRegistry()
        assert registry.base_cwd == Path.cwd()

    def test_explicit_base_cwd(self, tmp_path):
        registry = ToolRegistry(base_cwd=str(tmp_path))
        assert registry.base_cwd == tmp_path.resolve()

    def test_blocks_absolute_path_escape(self, tmp_path):
        """Absolute path outside workspace is blocked."""
        registry = ToolRegistry(base_cwd=str(tmp_path))
        with pytest.raises(ValueError, match="Path escapes workspace"):
            registry._resolve_path("/etc/shadow")

    def test_blocks_relative_path_escape(self, tmp_path):
        """../../../etc/passwd is blocked."""
        registry = ToolRegistry(base_cwd=str(tmp_path))
        with pytest.raises(ValueError, match="Path escapes workspace"):
            registry._resolve_path("../../../etc/passwd")

    def test_allows_path_within_workspace(self, tmp_path):
        """Paths within workspace are resolved correctly."""
        registry = ToolRegistry(base_cwd=str(tmp_path))
        result = registry._resolve_path("src/main.py")
        assert str(result).startswith(str(tmp_path.resolve()))

    def test_allows_exact_workspace(self, tmp_path):
        """The workspace root itself is allowed."""
        registry = ToolRegistry(base_cwd=str(tmp_path))
        result = registry._resolve_path(".")
        assert result == tmp_path.resolve()

    def test_blocks_symlink_escape(self, tmp_path):
        """Symlink pointing outside workspace is blocked."""
        link = tmp_path / "escape"
        link.symlink_to("/etc")
        registry = ToolRegistry(base_cwd=str(tmp_path))
        with pytest.raises(ValueError, match="Path escapes workspace"):
            registry._resolve_path("escape/shadow")

    def test_no_base_cwd_still_blocks_escape(self):
        """Even with default cwd, path traversal is blocked."""
        registry = ToolRegistry()
        with pytest.raises(ValueError, match="Path escapes workspace"):
            registry._resolve_path("../../../etc/shadow")
