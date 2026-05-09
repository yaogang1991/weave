# Tools Registry Module SPEC

## Purpose

Unified tool registry that provides built-in tools (read, write, edit, bash, glob, grep, git) and an extension point for MCP (Model Context Protocol) tools. All tools share a single execution interface: `execute(name, arguments) -> ToolResult`. Enforces path containment when a `base_cwd` is configured, prevents command injection via deny-pattern validation, and applies sensible limits on file sizes and result counts.

Source: `tools/registry.py`

---

## Public Interfaces

### Class: `ToolRegistry`

```python
class ToolRegistry:
    def __init__(
        self,
        sandbox_runner=None,
        base_cwd: str | None = None,
    )
```

**Constructor fields:**
- `sandbox_runner` -- Optional sandbox runner (reserved for future use).
- `base_cwd: Path | None` -- Base working directory. When set, all file paths are resolved relative to this directory and must stay within it (containment enforcement).
- `_tools: dict[str, Callable]` -- Map of tool name to handler function.
- `_schemas: dict[str, dict]` -- Map of tool name to JSON schema.

#### `register(name: str, handler: Callable, schema: dict) -> None`

Register or replace a tool. Schema is keyed by name to prevent duplicates.

#### `get_schema(name: str) -> dict | None`

Return the JSON schema for a tool, or `None` if not found.

#### `schemas -> list[dict]` (property)

Return all registered tool schemas as a list.

#### `execute(name: str, arguments: dict) -> ToolResult`

Execute a tool by name. Returns a `ToolResult` with `duration_ms` populated. If the tool is not found, returns a failed `ToolResult` with `error="Tool '{name}' not found"`. Catches all exceptions from tool handlers and wraps them in a failed `ToolResult`.

---

## Built-in Tools

### `read`

**Schema:**
- `file_path: string` (required) -- Path to file.
- `offset: integer` (default 0) -- Start line (0-based).
- `limit: integer` (default 2000) -- Max lines.

**Behavior:** Reads file content line-by-line. Returns line range `[offset : offset + limit]`. If more lines remain, appends a continuation message. Rejects files over 10 MB (`_MAX_FILE_SIZE`). Rejects binary files (via `UnicodeDecodeError` catch).

### `write`

**Schema:**
- `file_path: string` (required) -- Path to file.
- `content: string` (required) -- Content to write.

**Behavior:** Creates parent directories if needed. Overwrites existing file. Returns `"Written {N} chars to {file_path}"`.

### `edit`

**Schema:**
- `file_path: string` (required)
- `old_string: string` (required) -- Exact text to find.
- `new_string: string` (required) -- Replacement text.

**Behavior:** Reads file, finds first occurrence of `old_string`, replaces it with `new_string`. Returns `"Edited {file_path} (line {N})"`. Fails if file not found or `old_string` not present.

### `bash`

**Schema:**
- `command: string` (required) -- Shell command to execute.
- `timeout: integer` (default 120) -- Seconds before timeout.
- `cwd: string` (optional) -- Working directory (must stay within project root).

**Behavior:** Validates command against deny patterns (`"rm -rf /"`, `"shutdown"`, `"reboot"`, `"mkfs"`, `":(){ :|:& };:"`). Resolves `cwd` within project root. Runs via `subprocess.run` with `shell=True`. Returns combined stdout + stderr. Sets `success=False` on non-zero exit code or `TimeoutExpired`.

### `glob`

**Schema:**
- `pattern: string` (required) -- Glob pattern (e.g., `'**/*.py'`).
- `path: string` (default `"."`) -- Base directory.
- `max_results: integer` (default 1000) -- Maximum matches.

**Behavior:** Uses `pathlib.Path.rglob`. Skips paths under ignored directories (`.git`, `.venv`, `venv`, `node_modules`, `dist`, `build`, `__pycache__`). Returns relative paths, one per line. Truncates at `max_results`.

### `grep`

**Schema:**
- `pattern: string` (required) -- Text pattern to search for.
- `path: string` (default `"."`) -- Base directory.
- `file_pattern: string` (default `"*"`) -- File glob filter (e.g., `'*.py'`).
- `max_results: integer` (default 200) -- Maximum matching lines.

**Behavior:** Recursively walks files matching `file_pattern`. Skips binary files (by extension and first-8KB null-byte check). Skips files over 10 MB. Uses strict `read_text`. Output format: `{file}:{line_num}:{line}`. Reports counts of skipped binary and large files.

### `git`

**Schema:**
- `command: string` (required) -- Git subcommand.
- `args: array<string>` (default `[]`) -- Additional arguments.

**Behavior:** Runs `git {command} {args...}` via `subprocess.run`. 60-second timeout. Uses `base_cwd` as working directory if set.

---

## Internal Methods

| Method | Signature | Purpose |
|---|---|---|
| `_resolve_path` | `(file_path: str) -> Path` | Resolve path relative to `base_cwd`. Raises `ValueError` if path escapes workspace. |
| `_validate_bash_command` | `(command: str) -> str \| None` | Check against deny patterns. Returns matched pattern or `None`. |
| `_resolve_safe_cwd` | `(requested_cwd: str \| None = None) -> Path` | Validate cwd stays within project root. |
| `_should_skip_path` | `(file_path: Path) -> bool` | True if path is under an ignored directory. |

---

## Module Constants

| Constant | Value | Description |
|---|---|---|
| `_MAX_FILE_SIZE` | `10 * 1024 * 1024` (10 MB) | Maximum file size for read/grep. |
| `_MAX_GLOB_RESULTS` | `1000` | Default max glob matches. |
| `_MAX_GREP_MATCH_LINES` | `200` | Default max grep matching lines. |
| `_DEFAULT_IGNORE_DIRS` | `{".git", ".venv", "venv", "node_modules", "dist", "build", "__pycache__"}` | Directories skipped by glob and grep. |
| `_BINARY_EXTENSIONS` | `frozenset` of ~35 extensions | File extensions treated as binary. |

---

## Data Flow

```
Agent (via Guardrails or direct)
       |
       v
ToolRegistry.execute(tool_name, arguments)
       |
       +---> Lookup tool_name in _tools
       |         |
       |         +---> Not found --> ToolResult(success=False, error="Tool not found")
       |
       +---> Call handler(**arguments)
       |         |
       |         +---> Handler internally:
       |         |       1. _resolve_path(file_path)   --> Path (contained)
       |         |       2. Execute operation
       |         |       3. Return ToolResult
       |         |
       |         +---> Exception --> ToolResult(success=False, error=str(e))
       |
       +---> Set duration_ms on result
       |
       v
ToolResult(success, output, error, duration_ms)
```

---

## Error Codes

All errors are returned as `ToolResult(success=False, error=...)`. No exceptions are raised from `execute()`.

| Tool | Error Condition | Error Message Pattern |
|---|---|---|
| Any | Tool not found | `"Tool '{name}' not found"` |
| Any | Handler exception | `str(exception)` |
| read | File not found | `"File not found: {path}"` |
| read | File too large | `"File too large ({size} bytes, max {max})"` |
| read | Binary file | `"Cannot read file as text (binary?): {path}"` |
| write | Permission/IO error | `str(exception)` |
| edit | File not found | `"File not found: {path}"` |
| edit | String not found | `"old_string not found in file"` |
| bash | Blocked pattern | `"Blocked unsafe command pattern: {pattern}"` |
| bash | Command timed out | `"Command timed out after {timeout}s"` |
| bash | Non-zero exit | stderr content in `error` field |
| glob/grep | Invalid path or permissions | `str(exception)` |
| Path resolution | Path escapes workspace | `ValueError("Path escapes workspace: {path}")` |
| CWD resolution | CWD outside project root | `ValueError("cwd outside project root is not allowed: {path}")` |

---

## Dependencies

| Dependency | Module | Usage |
|---|---|---|
| `ToolResult` | `core.models` | Unified return type for all tool executions. |
| `subprocess` | stdlib | bash and git tool execution. |
| `pathlib` | stdlib | Path resolution and manipulation. |

---

## Configuration

| Parameter | Default | Description |
|---|---|---|
| `sandbox_runner` | `None` | Reserved for future sandbox integration. |
| `base_cwd` | `None` | Base working directory. When set, enforces path containment. |

---

## Extension Points

1. **MCP tools**: Call `register(name, handler, schema)` to add tools from MCP servers at runtime.
2. **Custom tools**: Register new tools with the same interface before agent execution begins.
3. **Tool override**: Re-registering an existing tool name replaces its handler and schema.
4. **Sandbox integration**: Pass a `sandbox_runner` to delegate command execution to a sandboxed environment.
5. **Additional deny patterns**: Extend `_validate_bash_command` or subclass `ToolRegistry` to add custom validation.

---

## Invariants

1. `execute()` never raises exceptions -- all errors are captured in `ToolResult(success=False, ...)`.
2. When `base_cwd` is set, all file operations are constrained to that directory; `_resolve_path` raises `ValueError` on escape attempts.
3. `edit` only replaces the first occurrence of `old_string`.
4. `bash` validates commands against a deny list before execution.
5. `glob` and `grep` always skip directories in `_DEFAULT_IGNORE_DIRS`.
6. Binary files are rejected by extension check and by null-byte content detection (first 8 KB).
7. All tool results include `duration_ms` (wall-clock time of the tool execution).
8. Tool schemas follow the JSON Schema format compatible with LLM function calling.
