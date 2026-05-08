"""
Tool Registry: built-in tools + MCP integration.
All tools share a unified interface: execute(name, input) -> ToolResult
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Callable

from core.models import ToolResult

# Maximum file size to read/search (10 MB)
_MAX_FILE_SIZE = 10 * 1024 * 1024

# File extensions that are typically binary
_BINARY_EXTENSIONS = frozenset({
    ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".wav", ".flac",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".sqlite", ".db", ".woff", ".woff2", ".ttf", ".eot",
    ".class", ".jar", ".o", ".a",
})


class ToolRegistry:
    """
    Registry for all available tools.
    Built-in tools: read, write, edit, bash, glob, grep, git
    MCP tools: dynamically loaded from MCP servers
    """

    def __init__(self, sandbox_runner=None):
        self._tools: dict[str, Callable] = {}
        self._schemas: dict[str, dict] = {}
        self.sandbox_runner = sandbox_runner
        self._register_builtin_tools()

    def _register_builtin_tools(self):
        self.register("read", self._tool_read, {
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
        })

        self.register("write", self._tool_write, {
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
        })

        self.register("edit", self._tool_edit, {
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
        })

        self.register("bash", self._tool_bash, {
            "name": "bash",
            "description": "Execute a bash command.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command to execute"},
                    "timeout": {"type": "integer", "default": 120},
                },
                "required": ["command"],
            },
        })

        self.register("glob", self._tool_glob, {
            "name": "glob",
            "description": "Find files matching a pattern.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern like '**/*.py'"},
                    "path": {"type": "string", "description": "Base directory"},
                },
                "required": ["pattern"],
            },
        })

        self.register("grep", self._tool_grep, {
            "name": "grep",
            "description": "Search for text in files. Skips binary and large files.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "file_pattern": {"type": "string", "description": "e.g. '*.py'"},
                },
                "required": ["pattern"],
            },
        })

        self.register("git", self._tool_git, {
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
        })

    def register(self, name: str, handler: Callable, schema: dict):
        """Register or replace a tool. Schema keyed by name prevents duplicates."""
        self._tools[name] = handler
        self._schemas[name] = schema

    def get_schema(self, name: str) -> dict | None:
        return self._schemas.get(name)

    @property
    def schemas(self) -> list[dict]:
        return list(self._schemas.values())

    def execute(self, name: str, arguments: dict) -> ToolResult:
        if name not in self._tools:
            return ToolResult(
                tool_call_id="",
                success=False,
                error=f"Tool '{name}' not found",
            )

        start = time.time()
        try:
            result = self._tools[name](**arguments)
            duration = int((time.time() - start) * 1000)
            if isinstance(result, ToolResult):
                result.duration_ms = duration
                return result
            return ToolResult(
                tool_call_id="",
                success=True,
                output=str(result),
                duration_ms=duration,
            )
        except Exception as e:
            return ToolResult(
                tool_call_id="",
                success=False,
                error=str(e),
                duration_ms=int((time.time() - start) * 1000),
            )

    # --- Built-in tool implementations ---

    def _tool_read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ToolResult:
        try:
            path = Path(file_path)
            if not path.exists():
                return ToolResult(tool_call_id="", success=False, error=f"File not found: {file_path}")

            if path.stat().st_size > _MAX_FILE_SIZE:
                return ToolResult(
                    tool_call_id="",
                    success=False,
                    error=f"File too large ({path.stat().st_size} bytes, max {_MAX_FILE_SIZE})",
                )

            with open(path, "r") as f:
                lines = f.readlines()

            total_lines = len(lines)
            selected = lines[offset:offset + limit]
            content = "".join(selected)

            if offset + limit < total_lines:
                content += f"\n... ({total_lines - offset - limit} more lines, use offset/limit to read further)"

            return ToolResult(tool_call_id="", success=True, output=content)
        except UnicodeDecodeError:
            return ToolResult(tool_call_id="", success=False, error=f"Cannot read file as text (binary?): {file_path}")
        except Exception as e:
            return ToolResult(tool_call_id="", success=False, error=str(e))

    def _tool_write(self, file_path: str, content: str) -> ToolResult:
        try:
            path = Path(file_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
            return ToolResult(tool_call_id="", success=True, output=f"Written {len(content)} chars to {file_path}")
        except Exception as e:
            return ToolResult(tool_call_id="", success=False, error=str(e))

    def _tool_edit(self, file_path: str, old_string: str, new_string: str) -> ToolResult:
        try:
            path = Path(file_path)
            if not path.exists():
                return ToolResult(tool_call_id="", success=False, error=f"File not found: {file_path}")

            content = path.read_text()
            idx = content.find(old_string)
            if idx == -1:
                return ToolResult(tool_call_id="", success=False, error="old_string not found in file")

            line_num = content[:idx].count("\n") + 1
            content = content[:idx] + new_string + content[idx + len(old_string):]
            path.write_text(content)
            return ToolResult(
                tool_call_id="",
                success=True,
                output=f"Edited {file_path} (line {line_num})",
            )
        except Exception as e:
            return ToolResult(tool_call_id="", success=False, error=str(e))

    def _tool_bash(self, command: str, timeout: int = 120) -> ToolResult:
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = result.stdout
            if result.stderr:
                output += "\n" + result.stderr
            return ToolResult(
                tool_call_id="",
                success=result.returncode == 0,
                output=output,
                error=result.stderr if result.returncode != 0 else "",
            )
        except subprocess.TimeoutExpired:
            return ToolResult(tool_call_id="", success=False, error=f"Command timed out after {timeout}s")
        except Exception as e:
            return ToolResult(tool_call_id="", success=False, error=str(e))

    def _tool_glob(self, pattern: str, path: str = ".") -> ToolResult:
        try:
            base = Path(path)
            matches = list(base.rglob(pattern))
            output = "\n".join(str(m.relative_to(base)) for m in matches)
            return ToolResult(tool_call_id="", success=True, output=output or "No matches")
        except Exception as e:
            return ToolResult(tool_call_id="", success=False, error=str(e))

    def _tool_grep(self, pattern: str, path: str = ".", file_pattern: str = "*") -> ToolResult:
        try:
            base = Path(path)
            matches = []
            skipped_binary = 0
            skipped_large = 0

            for file_path in base.rglob(file_pattern):
                if not file_path.is_file():
                    continue

                # Fast reject by extension
                if file_path.suffix.lower() in _BINARY_EXTENSIONS:
                    skipped_binary += 1
                    continue

                # Size check
                try:
                    if file_path.stat().st_size > _MAX_FILE_SIZE:
                        skipped_large += 1
                        continue
                except OSError:
                    continue

                # Content-based binary detection: check first 8KB for null bytes
                try:
                    with open(file_path, "rb") as f:
                        chunk = f.read(8192)
                        if b"\x00" in chunk:
                            skipped_binary += 1
                            continue
                except Exception:
                    continue

                try:
                    content = file_path.read_text(errors="strict")
                    if pattern in content:
                        lines = [
                            f"{file_path}:{i+1}:{line}"
                            for i, line in enumerate(content.split("\n"))
                            if pattern in line
                        ]
                        matches.extend(lines)
                except (UnicodeDecodeError, UnicodeError):
                    skipped_binary += 1
                    continue
                except Exception:
                    continue

            output = "\n".join(matches[:100]) or "No matches"
            if skipped_binary:
                output += f"\n({skipped_binary} binary files skipped)"
            if skipped_large:
                output += f"\n({skipped_large} large files skipped)"

            return ToolResult(tool_call_id="", success=True, output=output)
        except Exception as e:
            return ToolResult(tool_call_id="", success=False, error=str(e))

    def _tool_git(self, command: str, args: list | None = None) -> ToolResult:
        args = args or []
        full_cmd = ["git", command] + args
        try:
            result = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )
            output = result.stdout
            if result.stderr:
                output += "\n" + result.stderr
            return ToolResult(
                tool_call_id="",
                success=result.returncode == 0,
                output=output,
                error=result.stderr if result.returncode != 0 else "",
            )
        except Exception as e:
            return ToolResult(tool_call_id="", success=False, error=str(e))
