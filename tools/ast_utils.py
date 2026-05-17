"""AST signature extraction for Python files (#515).

Extracted from ToolRegistry._extract_python_signatures for maintainability.
"""
from __future__ import annotations

import ast
from pathlib import Path


def extract_python_signatures(path: Path) -> str:
    """Extract class/function signatures and docstrings from a Python file.

    Returns a condensed version suitable for understanding the API without
    reading every implementation detail.  For large files this prevents
    API context overflow (#349).
    """
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return f"(could not read {path.name})"

    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Fall back to first 100 lines if AST parsing fails
        lines = source.splitlines()[:100]
        return (
            f"# {path.name} (first 100 lines — "
            f"file has syntax errors)\n"
            + "\n".join(lines)
        )

    src_lines = source.splitlines()
    total_lines = len(src_lines)
    parts = [
        f"# {path.name} — signatures only "
        f"(file is {total_lines} lines, "
        f"{path.stat().st_size} bytes; "
        f"use offset/limit to read specific sections)\n",
    ]

    def _get_def_lines(node):
        """Extract just the def/class signature lines (up to colon)."""
        start = node.lineno - 1
        # end_lineno points to last line of the entire block;
        # we want just the signature (lines up to and including ':')
        end = min(
            getattr(node, "end_lineno", start + 10) - 1,
            start + 10,  # cap at 10 lines for signature
        )
        raw = src_lines[start:end]
        # Find the line with the closing ')' + ':'
        sig = []
        for line in raw:
            sig.append(line)
            stripped = line.rstrip()
            if stripped.endswith(":") and "(" in "".join(sig):
                break
        return "\n".join(sig)

    def _get_docstring(node):
        """Extract first-line docstring if present."""
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            return node.body[0].value.value[:200]
        return None

    for node in ast.iter_child_nodes(tree):
        if isinstance(
            node,
            (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            sig = _get_def_lines(node)

            if isinstance(node, ast.ClassDef):
                parts.append(f"\n{sig}")
                doc = _get_docstring(node)
                if doc:
                    parts.append(f'    """{doc}"""')
                for item in node.body:
                    if isinstance(
                        item,
                        (ast.FunctionDef, ast.AsyncFunctionDef),
                    ):
                        method_sig = _get_def_lines(item)
                        parts.append(f"    {method_sig}")
                        mdoc = _get_docstring(item)
                        if mdoc:
                            parts.append(
                                f'        """{mdoc}"""'
                            )
            else:
                parts.append(f"\n{sig}")
                doc = _get_docstring(node)
                if doc:
                    parts.append(f'    """{doc}"""')

        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            line = src_lines[node.lineno - 1]
            parts.append(line)

    result = "\n".join(parts)
    # Safety cap: if signature extraction is still huge, truncate
    max_sig = 4096
    if len(result) > max_sig:
        result = result[:max_sig] + "\n... (truncated signatures)"
    return result
