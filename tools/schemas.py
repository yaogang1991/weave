"""Built-in tool JSON schema definitions (#515).

Extracted from ToolRegistry._register_builtin_tools for maintainability.
"""
from __future__ import annotations

from typing import Any


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "read": {
        "name": "read",
        "description": (
            "Read file contents. Returns text line-by-line. "
            "Default limit is 2000 lines starting from offset."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to file"},
                "offset": {"type": "integer", "description": "Start line (0-based, default 0)"},
                "limit": {"type": "integer", "description": "Max lines (default 2000)"},
            },
            "required": ["file_path"],
        },
    },
    "write": {
        "name": "write",
        "description": "Create or overwrite a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["file_path", "content"],
        },
    },
    "edit": {
        "name": "edit",
        "description": (
            "Replace old_string with new_string in a file. "
            "Only replaces the first occurrence. "
            "Returns the line number where the replacement was made."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
    "bash": {
        "name": "bash",
        "description": (
            "Execute a bash command in the project workspace. "
            "Commands run with cwd=PROJECT_ROOT by default. "
            "Use relative paths from PROJECT_ROOT (e.g. 'python -m pytest tests/test_x.py'). "
            "Do NOT guess absolute paths or cd into unknown directories."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Command to execute (runs in PROJECT_ROOT)",
                },
                "timeout": {"type": "integer", "default": 120},
                "cwd": {
                    "type": "string",
                    "description": (
                        "Optional cwd relative to PROJECT_ROOT. "
                        "Must stay within project root. "
                        "Usually leave empty."
                    ),
                },
            },
            "required": ["command"],
        },
    },
    "glob": {
        "name": "glob",
        "description": "Find files matching a pattern.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern like '**/*.py'"},
                "path": {"type": "string", "description": "Base directory"},
                "max_results": {"type": "integer", "default": 1000},
            },
            "required": ["pattern"],
        },
    },
    "grep": {
        "name": "grep",
        "description": "Search for text in files. Skips binary and large files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "file_pattern": {"type": "string", "description": "e.g. '*.py'"},
                "max_results": {"type": "integer", "default": 200},
            },
            "required": ["pattern"],
        },
    },
    "git": {
        "name": "git",
        "description": "Execute git commands (status, diff, commit, branch, etc.)",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Git subcommand"},
                "args": {"type": "array", "items": {"type": "string"}, "default": []},
            },
            "required": ["command"],
        },
    },
}
