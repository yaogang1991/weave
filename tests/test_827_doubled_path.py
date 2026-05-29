"""Tests for #827: strip doubled project directory prefix in _resolve_path."""

from pathlib import Path

from tools.registry import ToolRegistry


class TestDoubledProjectPath:
    def test_strips_project_dir_prefix(self, tmp_path):
        project = tmp_path / "demo-baseconv"
        project.mkdir()
        reg = ToolRegistry(base_cwd=str(project))
        resolved = reg._resolve_path("demo-baseconv/baseconv/converter.py")
        assert resolved == project / "baseconv" / "converter.py"

    def test_normal_relative_path_unchanged(self, tmp_path):
        project = tmp_path / "myproject"
        project.mkdir()
        reg = ToolRegistry(base_cwd=str(project))
        resolved = reg._resolve_path("src/main.py")
        assert resolved == project / "src" / "main.py"

    def test_absolute_path_within_workspace(self, tmp_path):
        project = tmp_path / "myproject"
        project.mkdir()
        (project / "other").mkdir()
        reg = ToolRegistry(base_cwd=str(project))
        resolved = reg._resolve_path(str(project / "other" / "file.py"))
        assert resolved == (project / "other" / "file.py").resolve()

    def test_no_base_cwd_no_strip(self):
        reg = ToolRegistry()
        resolved = reg._resolve_path("src/main.py")
        assert resolved == Path.cwd().resolve() / "src" / "main.py"
