"""Tests for path traversal prevention in ToolRegistry (#500)."""
import pytest

from tools.registry import ToolRegistry


class TestPathTraversalPrevention:
    """Verify _resolve_path enforces workspace boundary when configured (#500)."""

    def test_default_base_cwd_is_none(self):
        """When base_cwd is not provided, it stays None (no workspace boundary)."""
        registry = ToolRegistry()
        assert registry.base_cwd is None
        assert registry._base_cwd_explicit is False

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
        # Use a directory that exists on both Unix and Windows (#581)
        outside = tmp_path.parent / "_outside_target"
        outside.mkdir(exist_ok=True)
        link.symlink_to(str(outside))
        registry = ToolRegistry(base_cwd=str(tmp_path))
        with pytest.raises(ValueError, match="Path escapes workspace"):
            registry._resolve_path("escape/shadow")

    def test_no_base_cwd_allows_absolute_paths(self):
        """Without explicit base_cwd, absolute paths are resolved freely (#529)."""
        from pathlib import Path

        registry = ToolRegistry()
        # No ValueError — no workspace boundary enforced
        result = registry._resolve_path("/tmp/test.py")
        assert result == Path("/tmp/test.py").resolve()

    def test_no_base_cwd_resolves_relative_to_cwd(self):
        """Without explicit base_cwd, relative paths resolve against cwd."""
        from pathlib import Path

        registry = ToolRegistry()
        result = registry._resolve_path("src/main.py")
        assert result == Path.cwd().resolve() / "src/main.py"
