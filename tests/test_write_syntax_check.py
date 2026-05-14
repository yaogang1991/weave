"""
Tests for #321: syntax validation before writing Python files.

The write tool validates Python syntax via compile() before writing to disk.
If the content has a syntax error (unterminated string, bad brackets, etc.),
the write is rejected with a clear error message instead of creating a broken file.
"""
import pytest

from tools.registry import ToolRegistry


@pytest.fixture
def registry(tmp_path):
    return ToolRegistry(base_cwd=str(tmp_path))


class TestWriteSyntaxCheck:
    def test_valid_python_writes_normally(self, registry, tmp_path):
        """Valid Python code is written without error."""
        result = registry._tool_write(
            str(tmp_path / "good.py"),
            "def hello():\n    return 'world'\n",
        )
        assert result.success
        assert (tmp_path / "good.py").exists()

    def test_unterminated_string_rejected(self, registry, tmp_path):
        """Unterminated string literal is caught before writing."""
        result = registry._tool_write(
            str(tmp_path / "bad.py"),
            'x = """unterminated\n',
        )
        assert not result.success
        assert "SyntaxError" in result.error
        assert "unterminated" not in (tmp_path / "bad.py").read_text(
            encoding="utf-8"
        ) if (tmp_path / "bad.py").exists() else True

    def test_mismatched_brackets_rejected(self, registry, tmp_path):
        """Mismatched brackets are caught before writing."""
        result = registry._tool_write(
            str(tmp_path / "brackets.py"),
            "def foo():\n    return [1, 2, 3\n",
        )
        assert not result.success
        assert "SyntaxError" in result.error

    def test_non_python_file_no_check(self, registry, tmp_path):
        """Non-.py files skip syntax validation."""
        result = registry._tool_write(
            str(tmp_path / "data.json"),
            '{"key": "value"\n',  # Invalid JSON but not checked
        )
        assert result.success

    def test_empty_content_no_check(self, registry, tmp_path):
        """Empty/whitespace-only content skips validation."""
        result = registry._tool_write(
            str(tmp_path / "empty.py"),
            "",
        )
        assert result.success

    def test_syntax_error_includes_filename(self, registry, tmp_path):
        """Error message includes the file path for clarity."""
        result = registry._tool_write(
            str(tmp_path / "module.py"),
            "def f():\n    x =\n",
        )
        assert not result.success
        assert "module.py" in result.error

    def test_syntax_error_message_actionable(self, registry, tmp_path):
        """Error message includes helpful guidance."""
        result = registry._tool_write(
            str(tmp_path / "bad.py"),
            "if True\n    pass\n",
        )
        assert not result.success
        assert "Fix the syntax error" in result.error
