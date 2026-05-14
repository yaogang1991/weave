"""Tests for #349: large Python file read truncation.

When a .py file exceeds _PYTHON_TRUNCATE_THRESHOLD (50 KB), the read
tool returns only class/function signatures and docstrings instead of
full content, preventing API 2 MB context overflow.
"""

from __future__ import annotations

from pathlib import Path

from tools.registry import ToolRegistry, _PYTHON_TRUNCATE_THRESHOLD


def _make_registry(tmp_path):
    """Create a ToolRegistry with base_cwd set to tmp_path."""
    return ToolRegistry(base_cwd=tmp_path)


def _write_large_py(tmp_path, lines=2000):
    """Write a .py file with many lines to exceed the threshold."""
    content_lines = [
        '"""Module docstring."""',
        "import os",
        "import sys",
        "",
    ]
    for i in range(lines):
        content_lines.append(
            f"def func_{i}(self, x: int, y: str = '') -> bool:\n"
            f'    """Function {i}."""\n'
            f"    return x > 0"
        )
    content = "\n".join(content_lines)
    p = tmp_path / "large.py"
    p.write_text(content, encoding="utf-8")
    return p


def _write_large_class_py(tmp_path):
    """Write a .py file with large classes."""
    lines = [
        '"""Module with big classes."""',
        "from typing import Optional",
        "",
        "",
        "class BigClass:",
        '    """A big class with many methods."""',
        "",
        "    def __init__(self, name: str):",
        "        self.name = name",
        "",
    ]
    for i in range(50):
        lines.append(f"    def method_{i}(self, x: int) -> str:")
        lines.append(f'        """Method {i}."""')
        lines.append(f'        return str(x + {i})')
        lines.append("")

    content = "\n".join(lines)
    # Repeat to make it large
    p = tmp_path / "bigclass.py"
    p.write_text(content * 30, encoding="utf-8")
    return p


def test_large_py_returns_signatures(tmp_path):
    """A .py file > threshold should return signatures, not full content."""
    p = _write_large_py(tmp_path)
    assert p.stat().st_size > _PYTHON_TRUNCATE_THRESHOLD

    reg = _make_registry(tmp_path)
    result = reg.execute("read", {"file_path": "large.py"})
    assert result.success
    assert "signatures only" in result.output
    # Should NOT contain the full implementation of every function
    assert "return x > 0" not in result.output
    # Should contain the function signatures
    assert "func_0" in result.output


def test_small_py_returns_full_content(tmp_path):
    """A .py file < threshold should return full content."""
    p = tmp_path / "small.py"
    p.write_text("x = 1\ny = 2\n")

    reg = _make_registry(tmp_path)
    result = reg.execute("read", {"file_path": "small.py"})
    assert result.success
    assert "x = 1" in result.output
    assert "signatures only" not in result.output


def test_large_py_with_offset_returns_full(tmp_path):
    """Using offset should bypass signature extraction."""
    p = _write_large_py(tmp_path)
    assert p.stat().st_size > _PYTHON_TRUNCATE_THRESHOLD

    reg = _make_registry(tmp_path)
    result = reg.execute(
        "read", {"file_path": "large.py", "offset": 10, "limit": 50},
    )
    assert result.success
    # With offset, should return raw lines (no signature extraction)
    assert "signatures only" not in result.output


def test_large_py_with_small_limit_returns_full(tmp_path):
    """Using a small limit should bypass signature extraction."""
    _write_large_py(tmp_path)

    reg = _make_registry(tmp_path)
    result = reg.execute(
        "read", {"file_path": "large.py", "offset": 0, "limit": 10},
    )
    assert result.success
    assert "signatures only" not in result.output


def test_non_py_large_file_returns_full(tmp_path):
    """Non-.py files should always return full content regardless of size."""
    txt = tmp_path / "large.txt"
    txt.write_text("x\n" * 50000)  # ~100KB
    assert txt.stat().st_size > _PYTHON_TRUNCATE_THRESHOLD

    reg = _make_registry(tmp_path)
    result = reg.execute("read", {"file_path": "large.txt"})
    assert result.success
    assert "signatures only" not in result.output


def test_class_signatures_extracted(tmp_path):
    """Signature extraction should include class methods."""
    _write_large_class_py(tmp_path)
    reg = _make_registry(tmp_path)
    result = reg.execute("read", {"file_path": "bigclass.py"})
    assert result.success
    assert "BigClass" in result.output
    assert "__init__" in result.output
    assert "method_0" in result.output


def test_threshold_constant():
    """_PYTHON_TRUNCATE_THRESHOLD should be 50KB."""
    assert _PYTHON_TRUNCATE_THRESHOLD == 50 * 1024


def test_extract_python_signatures_syntax_error(tmp_path):
    """Files with syntax errors should fall back to first 100 lines."""
    p = tmp_path / "bad.py"
    p.write_text("def foo(\n" * 5000)
    result = ToolRegistry._extract_python_signatures(p)
    assert "syntax errors" in result.lower() or "first 100 lines" in result.lower()


def test_extract_python_signatures_truncation():
    """Signature extraction itself should be capped at 4096 chars."""
    # Create a file with many classes, each with long signatures
    lines = ["import os", ""]
    for i in range(200):
        lines.append(f"class Class{i}:")
        lines.append('    """' + "x" * 200 + '"""')
        lines.append("    def method(self, x: int) -> str:")
        lines.append('        """' + "y" * 200 + '"""')
        lines.append("        pass")
        lines.append("")
    p = Path("/tmp/test_truncation_signatures.py")
    p.write_text("\n".join(lines), encoding="utf-8")
    try:
        result = ToolRegistry._extract_python_signatures(p)
        assert len(result) <= 4200  # 4096 + some slack for truncation msg
    finally:
        p.unlink(missing_ok=True)
