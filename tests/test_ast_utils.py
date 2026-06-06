"""Tests for tools/ast_utils.py — Python signature extraction."""
from __future__ import annotations

import pytest
from pathlib import Path

from tools.ast_utils import extract_python_signatures


@pytest.fixture
def write_py(tmp_path):
    """Helper to write a Python file and return its path."""
    def _write(name: str, content: str) -> Path:
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        return p
    return _write


class TestFunctionExtraction:
    def test_simple_function_with_docstring(self, write_py):
        p = write_py("mod.py", 'def hello():\n    """Say hello."""\n    return "hi"\n')
        result = extract_python_signatures(p)
        assert "def hello():" in result
        assert "Say hello." in result

    def test_function_with_args(self, write_py):
        p = write_py("mod.py", "def add(a: int, b: int) -> int:\n    return a + b\n")
        result = extract_python_signatures(p)
        assert "def add" in result

    def test_async_function(self, write_py):
        p = write_py("mod.py", "async def fetch(url: str) -> bytes:\n    pass\n")
        result = extract_python_signatures(p)
        assert "async def fetch" in result

    def test_function_no_docstring(self, write_py):
        p = write_py("mod.py", "def compute(x):\n    return x * 2\n")
        result = extract_python_signatures(p)
        assert "def compute" in result


class TestClassExtraction:
    def test_class_with_methods(self, write_py):
        p = write_py("mod.py", (
            "class Foo:\n"
            '    """A foo."""\n'
            "    def __init__(self, x):\n"
            "        self.x = x\n"
            "\n"
            "    def bar(self) -> int:\n"
            '        """Bar method."""\n'
            "        return self.x\n"
        ))
        result = extract_python_signatures(p)
        assert "class Foo:" in result
        assert "A foo." in result
        assert "def __init__" in result
        assert "def bar" in result
        assert "Bar method." in result

    def test_class_no_docstring(self, write_py):
        p = write_py("mod.py", "class Bar:\n    def method(self):\n        pass\n")
        result = extract_python_signatures(p)
        assert "class Bar:" in result
        assert "def method" in result


class TestImports:
    def test_import_included(self, write_py):
        p = write_py("mod.py", "import os\nimport sys\n\ndef f():\n    pass\n")
        result = extract_python_signatures(p)
        assert "import os" in result
        assert "import sys" in result

    def test_from_import_included(self, write_py):
        p = write_py("mod.py", "from pathlib import Path\n\ndef f():\n    pass\n")
        result = extract_python_signatures(p)
        assert "from pathlib import Path" in result


class TestEdgeCases:
    def test_syntax_error_fallback(self, write_py):
        p = write_py("bad.py", "def broken(\n    pass\n" + "\n" * 50)
        result = extract_python_signatures(p)
        assert "syntax errors" in result

    def test_nonexistent_file(self):
        p = Path("/nonexistent/file.py")
        result = extract_python_signatures(p)
        assert "could not read" in result

    def test_empty_file(self, write_py):
        p = write_py("empty.py", "")
        result = extract_python_signatures(p)
        assert "empty.py" in result

    def test_large_file_truncation(self, write_py):
        code = "def f():\n    pass\n\n" + "x = 1\n" * 2000
        p = write_py("big.py", code)
        result = extract_python_signatures(p)
        assert len(result) <= 4100

    def test_file_header_contains_name(self, write_py):
        p = write_py("mymod.py", "def f():\n    pass\n")
        result = extract_python_signatures(p)
        assert "mymod.py" in result

    def test_unicode_content(self, write_py):
        p = write_py("uni.py", '# -*- coding: utf-8 -*-\ndef f():\n    """Chinese doc"""\n    pass\n')
        result = extract_python_signatures(p)
        assert "Chinese doc" in result
