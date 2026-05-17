"""
Tool Registry: built-in tools + MCP integration.
All tools share a unified interface: execute(name, input) -> ToolResult
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from core.models import ToolResult
from tools.command_runner import ToolCommandRunner

# Maximum file size to read/search (10 MB)
_MAX_FILE_SIZE = 10 * 1024 * 1024
_MAX_GLOB_RESULTS = 1000
_MAX_GREP_MATCH_LINES = 200

# For Python files larger than this (bytes), the read tool returns only
# class/function signatures and docstrings instead of the full content (#349).
# 50 KB ≈ 1200-1500 lines, well within the API 2 MB limit for a single file
# but would consume most of the context budget when combined with conversation
# history.
_PYTHON_TRUNCATE_THRESHOLD = 50 * 1024

_DEFAULT_IGNORE_DIRS = {".git", ".venv", "venv", "node_modules", "dist", "build", "__pycache__"}

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

    def __init__(
        self, sandbox_runner: ToolCommandRunner | None = None, base_cwd: str | None = None,
    ):
        self._tools: dict[str, Callable] = {}
        self._schemas: dict[str, dict] = {}
        self.sandbox_runner: ToolCommandRunner | None = sandbox_runner
        self.base_cwd = Path(base_cwd).resolve() if base_cwd else None
        self._ownership_context: dict[str, list[str]] | None = None  # owned/forbidden/shared
        self._register_builtin_tools()

    def _resolve_path(self, file_path: str) -> Path:
        """Resolve path relative to configured base working directory.

        When base_cwd is set, enforces containment: resolved path must
        stay under base_cwd to prevent escape from the backend workspace.
        """
        path = Path(file_path)
        if self.base_cwd is None:
            return path if path.is_absolute() else path.resolve()
        resolved = (self.base_cwd / path).resolve()
        if not (resolved == self.base_cwd or self.base_cwd in resolved.parents):
            raise ValueError(f"Path escapes workspace: {file_path}")
        return resolved

    def set_ownership_context(self, context: dict[str, list[str]] | None) -> None:
        """Set file ownership context for current node execution (#272).

        When set, write/edit operations check against forbidden files
        before allowing modifications.
        """
        self._ownership_context = context

    def _check_write_allowed(self, file_path: str) -> str | None:
        """Return error message if write is forbidden, None if allowed (#272)."""
        if self._ownership_context is None:
            return None  # No contract → allow (auto-serialization fallback)
        forbidden = self._ownership_context.get("forbidden", [])
        if not forbidden:
            return None
        # Normalize both paths for comparison
        try:
            resolved = self._resolve_path(file_path)
        except ValueError:
            return f"Path resolution failed for '{file_path}'"
        resolved_str = str(resolved)
        for fb in forbidden:
            # Check suffix match (handles relative vs absolute differences)
            if resolved_str.endswith(fb) or resolved_str.endswith("/" + fb):
                return (
                    f"File '{file_path}' is owned by another parallel node "
                    f"and is forbidden for this node (#272)."
                )
        return None

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
        })

        self.register("glob", self._tool_glob, {
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
                    "max_results": {"type": "integer", "default": 200},
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

    def register_mcp_tools(self, mcp_client: Any, discovered_tools: list) -> int:
        """Register MCP-discovered tools in the registry.

        Returns the number of tools registered.
        """
        count = 0
        for tool_info in discovered_tools:
            handler = _make_mcp_handler(mcp_client, tool_info.prefixed_name)
            schema = {
                "name": tool_info.prefixed_name,
                "description": f"[MCP:{tool_info.server_name}] {tool_info.description}",
                "input_schema": tool_info.input_schema,
            }
            self.register(tool_info.prefixed_name, handler, schema)
            count += 1
        return count

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

    def _validate_bash_command(self, command: str) -> str | None:
        """Validate bash command against deny patterns (#493).

        Checks for dangerous operations including destructive filesystem
        commands, network exfiltration, reverse shells, and privilege
        escalation. Uses regex for robust matching against obfuscation.
        """
        import re

        normalized = command.lower().strip()

        # Remove common obfuscation: quotes, backslashes, $'' syntax
        cleaned = re.sub(r"[\'\"\\]", "", normalized)
        # Collapse whitespace
        cleaned = re.sub(r"\s+", " ", cleaned)

        deny_patterns = [
            # Destructive filesystem
            r"rm\s+(-[a-z]*f[a-z]*\s+/|-[a-z]*f[a-z]*\s+/*)",
            r"rm\s+-[a-z]*[rf][a-z]*\s+/",
            r"rm\s+(-[rf]\s+)+.*/(etc|usr|var|home|boot|sys|proc)",
            r"r\s*m\s+.*-[a-z]*r[a-z]*f",  # quoted obfuscation
            r"chmod\s+-[a-z]*r\s+(777|a+x|u\+s)",
            r"chown\s+.*:.*\s+/",
            r"dd\s+.*of=/dev/",
            r"mkfs",
            r"shred\s+/",
            r">\s*/dev/sd",
            # System control
            r"\bshutdown\b",
            r"\breboot\b",
            r"\binit\s+[06]",
            r"\b(systemctl|service)\s+(stop|disable|mask)\s+",
            # Fork bomb
            r":\(\)\{.*:\|:&",
            # Reverse shells / network exfiltration
            r"/dev/tcp/",
            r"/dev/udp/",
            r"nc\s+.*-[a-z]*e[a-z]*\s",
            r"ncat\s+.*-[a-z]*e[a-z]*\s",
            r"bash\s+-i\s+",
            r"\b(curl|wget)\s+.*\|\s*(ba)?sh\b",
            r"\b(curl|wget)\s+.*-d\s+@",
            r"\b(curl|wget)\s+.*--data\b.*@\.?",
            # Credential / secret access
            r"/etc/shadow",
            r"/etc/passwd",
            r"\.ssh/id_[rd]sa",
            r"\.ssh/id_ed25519",
            r"\.aws/credentials",
            r"\.aws/config",
            r"\.env\b",
            r"\.gitconfig",
            r"\.netrc",
            # Environment variable dump
            r"\b(print)?env\b(?!\s+PATH\b)(?!\s+HOME\b)",
            r"\bexport\b.*>\s",
            # Privilege escalation
            r"\bsudo\s+",
            r"\bsu\s+",
            r"\bpkexec\b",
            # Package installation (supply chain risk)
            r"\bpip\s+install\s+.*(--user|-e)\b",
            r"\bnpm\s+install\s+-g\b",
            r"\bcargo\s+install\b",
        ]

        for pattern in deny_patterns:
            if re.search(pattern, cleaned):
                return pattern
        return None

    def _resolve_safe_cwd(self, requested_cwd: str | None = None) -> Path:
        """Resolve and validate cwd within project root."""
        project_root = self.base_cwd.resolve() if self.base_cwd else Path.cwd().resolve()
        if requested_cwd:
            # Resolve relative paths against project_root, not process cwd
            req = Path(requested_cwd).expanduser()
            target = (project_root / req).resolve() if not req.is_absolute() else req.resolve()
        else:
            target = project_root
        if target != project_root and project_root not in target.parents:
            raise ValueError(f"cwd outside project root is not allowed: {target}")
        return target

    def _should_skip_path(self, file_path: Path) -> bool:
        """Return True when file path is under ignored directories."""
        return any(part in _DEFAULT_IGNORE_DIRS for part in file_path.parts)

    # --- Built-in tool implementations ---

    def _tool_read(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ToolResult:
        try:
            if not file_path or not file_path.strip():
                return ToolResult(
                    tool_call_id="", success=False,
                    error="file_path is required and cannot be empty",
                )
            path = self._resolve_path(file_path)
            if not path.exists():
                return ToolResult(
                    tool_call_id="", success=False,
                    error=f"File not found: {file_path}",
                )

            file_size = path.stat().st_size
            if file_size > _MAX_FILE_SIZE:
                return ToolResult(
                    tool_call_id="",
                    success=False,
                    error=(
                        f"File too large ({file_size} bytes, "
                        f"max {_MAX_FILE_SIZE})"
                    ),
                )

            # For large Python files, extract signatures instead of
            # returning full content (#349).  Prevents API 2 MB context
            # overflow when a test-generator reads a big source file.
            is_py = file_path.endswith(".py")
            if (
                is_py
                and file_size > _PYTHON_TRUNCATE_THRESHOLD
                and offset == 0
                and limit >= 2000
            ):
                return ToolResult(
                    tool_call_id="",
                    success=True,
                    output=self._extract_python_signatures(path),
                )

            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            total_lines = len(lines)
            selected = lines[offset:offset + limit]
            content = "".join(selected)

            if offset + limit < total_lines:
                content += (
                    f"\n... ({total_lines - offset - limit} more lines, "
                    f"use offset/limit to read further)"
                )

            return ToolResult(
                tool_call_id="", success=True, output=content,
            )
        except UnicodeDecodeError:
            return ToolResult(
                tool_call_id="", success=False,
                error=f"Cannot read file as text (binary?): {file_path}",
            )
        except Exception as e:
            return ToolResult(
                tool_call_id="", success=False, error=str(e),
            )

    @staticmethod
    def _extract_python_signatures(path: Path) -> str:
        """Extract class/function signatures and docstrings from a Python file.

        Returns a condensed version suitable for understanding the API without
        reading every implementation detail.  For large files this prevents
        API context overflow (#349).
        """
        import ast

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

    def _tool_write(self, file_path: str, content: str) -> ToolResult:
        try:
            if not file_path or not file_path.strip():
                return ToolResult(
                    tool_call_id="", success=False,
                    error="file_path is required and cannot be empty",
                )

            # Syntax validation for Python files before writing (#321).
            # Catches unterminated strings, invalid syntax, etc. before
            # the file hits disk, preventing import failures downstream.
            if file_path.endswith(".py") and content.strip():
                try:
                    compile(content, file_path, "exec")
                except SyntaxError as e:
                    return ToolResult(
                        tool_call_id="", success=False,
                        error=(
                            f"SyntaxError in {file_path}: line {e.lineno}: {e.msg}. "
                            f"Fix the syntax error before writing. "
                            f"Common causes: unterminated string literal, "
                            f"missing colon, mismatched brackets."
                        ),
                    )

            # Ownership enforcement (#272)
            write_error = self._check_write_allowed(file_path)
            if write_error:
                return ToolResult(tool_call_id="", success=False, error=write_error)
            path = self._resolve_path(file_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

            msg = f"Written {len(content)} chars to {file_path}"

            # Warn about long lines in Python files (E501 will fail lint).
            if file_path.endswith(".py"):
                long_lines = [
                    (i, len(line))
                    for i, line in enumerate(content.split("\n"), 1)
                    if len(line) > 100
                ]
                if long_lines:
                    preview = ", ".join(
                        f"L{i}:{n}c" for i, n in long_lines[:5]
                    )
                    suffix = f" (+{len(long_lines) - 5} more)" if len(long_lines) > 5 else ""
                    msg += (
                        f"\nWARNING: {len(long_lines)} line(s) over 100 chars "
                        f"({preview}{suffix}). Fix before finishing."
                    )

            return ToolResult(tool_call_id="", success=True, output=msg)
        except Exception as e:
            return ToolResult(tool_call_id="", success=False, error=str(e))

    def _tool_edit(self, file_path: str, old_string: str, new_string: str) -> ToolResult:
        try:
            if not file_path or not file_path.strip():
                return ToolResult(
                    tool_call_id="", success=False,
                    error="file_path is required and cannot be empty",
                )
            # Ownership enforcement (#272)
            write_error = self._check_write_allowed(file_path)
            if write_error:
                return ToolResult(tool_call_id="", success=False, error=write_error)
            path = self._resolve_path(file_path)
            if not path.exists():
                return ToolResult(
                    tool_call_id="", success=False,
                    error=f"File not found: {file_path}",
                )

            content = path.read_text(encoding="utf-8", errors="replace")
            idx = content.find(old_string)
            if idx == -1:
                return ToolResult(
                    tool_call_id="", success=False,
                    error="old_string not found in file",
                )

            line_num = content[:idx].count("\n") + 1
            content = content[:idx] + new_string + content[idx + len(old_string):]
            path.write_text(content, encoding="utf-8")

            # Build before/after context so agent trusts the result (#153)
            old_lines = old_string.splitlines()
            new_lines = new_string.splitlines()
            max_ctx = 5  # show up to 5 lines of before/after
            before_snippet = "\n".join(old_lines[:max_ctx])
            after_snippet = "\n".join(new_lines[:max_ctx])
            trunc_old = f"\n... ({len(old_lines)} lines total)" if len(old_lines) > max_ctx else ""
            trunc_new = f"\n... ({len(new_lines)} lines total)" if len(new_lines) > max_ctx else ""

            output = (
                f"Edited {file_path} (line {line_num})\n"
                f"Before:\n{before_snippet}{trunc_old}\n"
                f"After:\n{after_snippet}{trunc_new}\n"
                f"Edit successful. No need to re-read the file."
            )
            return ToolResult(
                tool_call_id="",
                success=True,
                output=output,
            )
        except Exception as e:
            return ToolResult(tool_call_id="", success=False, error=str(e))

    def _tool_bash(self, command: str, timeout: int = 120, cwd: str | None = None) -> ToolResult:
        denied = self._validate_bash_command(command)
        if denied:
            return ToolResult(
                tool_call_id="",
                success=False,
                error=f"Blocked unsafe command pattern: {denied}",
            )

        try:
            run_cwd = self._resolve_safe_cwd(cwd)
            if self.sandbox_runner is not None:
                result = self.sandbox_runner.run_command(
                    command,
                    cwd=str(run_cwd),
                    timeout=timeout,
                )
                returncode = result.returncode
                stdout = result.stdout
                stderr = result.stderr
                if result.timed_out:
                    return ToolResult(
                        tool_call_id="",
                        success=False,
                        error=f"Command timed out after {timeout}s",
                    )
            else:
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    cwd=str(run_cwd),
                )
                returncode = result.returncode
                stdout = result.stdout
                stderr = result.stderr
            output = f"[cwd] {run_cwd}\n{stdout}"
            if stderr:
                output += "\n" + stderr
            return ToolResult(
                tool_call_id="",
                success=returncode == 0,
                output=output,
                error=stderr if returncode != 0 else "",
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                tool_call_id="", success=False,
                error=f"Command timed out after {timeout}s",
            )
        except Exception as e:
            return ToolResult(tool_call_id="", success=False, error=str(e))

    def _tool_glob(
        self, pattern: str, path: str = ".",
        max_results: int = _MAX_GLOB_RESULTS,
    ) -> ToolResult:
        try:
            base = self._resolve_path(path)
            matches: list[Path] = []
            for match in base.rglob(pattern):
                if self._should_skip_path(match):
                    continue
                matches.append(match)
                if len(matches) >= max_results:
                    break

            output = "\n".join(str(m.relative_to(base)) for m in matches)
            if len(matches) >= max_results:
                output += f"\n... (truncated to {max_results} results)"
            return ToolResult(tool_call_id="", success=True, output=output or "No matches")
        except Exception as e:
            return ToolResult(tool_call_id="", success=False, error=str(e))

    def _tool_grep(
        self, pattern: str, path: str = ".", file_pattern: str = "*",
        max_results: int = _MAX_GREP_MATCH_LINES,
    ) -> ToolResult:
        try:
            base = self._resolve_path(path)
            matches = []
            skipped_binary = 0
            skipped_large = 0

            for file_path in base.rglob(file_pattern):
                if not file_path.is_file() or self._should_skip_path(file_path):
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
                    content = file_path.read_text(encoding="utf-8", errors="replace")
                    if pattern in content:
                        lines = [
                            f"{file_path}:{i+1}:{line}"
                            for i, line in enumerate(content.split("\n"))
                            if pattern in line
                        ]
                        matches.extend(lines)
                        if len(matches) >= max_results:
                            break
                except (UnicodeDecodeError, UnicodeError):
                    skipped_binary += 1
                    continue
                except Exception:
                    continue

            output = "\n".join(matches[:max_results]) or "No matches"
            if len(matches) > max_results:
                output += f"\n... (truncated to {max_results} matching lines)"
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
                encoding="utf-8",
                errors="replace",
                timeout=60,
                cwd=str(self.base_cwd) if self.base_cwd else None,
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


def _make_mcp_handler(mcp_client: Any, prefixed_name: str) -> Callable:
    """Create a tool handler function that routes to MCP server."""
    def handler(**kwargs) -> ToolResult:
        start = time.time()
        try:
            result = mcp_client.call_tool_sync(prefixed_name, kwargs)
            duration = int((time.time() - start) * 1000)
            if result.get("is_error"):
                return ToolResult(
                    tool_call_id="",
                    success=False,
                    error=str(result.get("content", "MCP tool error")),
                    duration_ms=duration,
                )
            return ToolResult(
                tool_call_id="",
                success=True,
                output=str(result.get("content", "")),
                duration_ms=duration,
            )
        except Exception as e:
            return ToolResult(
                tool_call_id="",
                success=False,
                error=f"MCP tool error ({prefixed_name}): {e}",
                duration_ms=int((time.time() - start) * 1000),
            )
    return handler
