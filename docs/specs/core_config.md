# SPEC: core/config.py

## Purpose

Centralizes all configuration for the Weave. Defines `WeaveConfig` (top-level) and its sub-configs (`LLMConfig`, `SandboxConfig`, `MCPConfig`). Supports loading from YAML files, environment variables, and the `~/.claude/settings-kimi.json` fallback file.

## Public Interfaces

### Module-level Helper

```python
def _load_claude_settings() -> dict[str, str]
```
Loads environment variables from `~/.claude/settings-kimi.json` if present. Returns the `env` key from that JSON, or `{}` on any failure. Cached in module-level `_CLAUDE_ENV`.

### `LLMConfig(BaseModel)`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `provider` | `str` | `"anthropic"` | LLM provider: `"anthropic"` or `"openai"` |
| `model` | `str` | `"claude-sonnet-4-6"` | Model identifier |
| `api_key` | `str` | `os.getenv("ANTHROPIC_API_KEY", os.getenv("ANTHROPIC_AUTH_TOKEN", _CLAUDE_ENV.get("ANTHROPIC_AUTH_TOKEN", "")))` | API key |
| `base_url` | `str` | `os.getenv("ANTHROPIC_BASE_URL", _CLAUDE_ENV.get("ANTHROPIC_BASE_URL", ""))` | API base URL override |
| `max_tokens` | `int` | `4096` | Max response tokens |
| `temperature` | `float` | `0.3` | Sampling temperature |
| `timeout` | `int` | `120` | Request timeout (seconds) |

### `SandboxConfig(BaseModel)`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `True` | Whether sandboxing is active |
| `runtime` | `str` | `"docker"` | Sandbox runtime: `"docker"`, `"bubblewrap"`, `"direct"` |
| `image` | `str` | `"python:3.11-slim"` | Docker image |
| `network_mode` | `str` | `"none"` | Network mode: `"none"` or `"bridge"` |
| `memory_limit` | `str` | `"512m"` | Container memory limit |
| `cpu_limit` | `float` | `1.0` | CPU core limit |
| `timeout` | `int` | `300` | Sandbox execution timeout (seconds) |
| `credential_proxy` | `bool` | `True` | Whether to proxy credentials into sandbox |

### `MCPConfig(BaseModel)`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `servers` | `list[dict[str, Any]]` | `[]` | MCP server definitions |
| `auto_discover` | `bool` | `False` | Whether to auto-discover MCP servers |

### `WeaveConfig(BaseModel)`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `llm` | `LLMConfig` | `LLMConfig()` | LLM configuration |
| `sandbox` | `SandboxConfig` | `SandboxConfig()` | Sandbox configuration |
| `mcp` | `MCPConfig` | `MCPConfig()` | MCP server configuration |
| `event_store_path` | `str` | `"./data/events"` | Event log directory |
| `artifact_path` | `str` | `"./data/artifacts"` | Artifact storage directory |
| `checkpoint_interval` | `int` | `10` | Checkpoint every N events |
| `max_context_messages` | `int` | `50` | Max messages in agent context |
| `agent_timeout` | `int` | `120` | Timeout per agent execution (seconds) |
| `max_context_tokens` | `int` | `100000` | Token threshold for context truncation |
| `log_level` | `str` | `"INFO"` | Logging level |
| `default_backend` | `str` | `os.getenv("WEAVE_DEFAULT_BACKEND", "local")` | Default execution backend |
| `backend_base_path` | `str` | `os.getenv("WEAVE_BACKEND_BASE_PATH", "./data/backends")` | Backend storage path |
| `risk_backend_map` | `dict[str, str]` | `{"low": env-or-"local", "medium": env-or-"local", "high": env-or-"worktree", "critical": env-or-"worktree"}` | Maps risk level to execution backend |
| `non_interactive` | `bool` | `os.getenv("WEAVE_NON_INTERACTIVE") in ("true","1","yes")` | Non-interactive mode |
| `approval_timeout_sec` | `int` | `int(os.getenv("WEAVE_APPROVAL_TIMEOUT_SEC", "300"))` | Approval timeout in non-interactive mode |

Class methods:
- `from_yaml(path: str | Path) -> WeaveConfig` -- Load from YAML file.
- `from_env() -> WeaveConfig` -- Create from environment variables with `settings-kimi.json` fallback.

## Data Flow

```
~/.claude/settings-kimi.json (fallback)
  + Environment variables (ANTHROPIC_API_KEY, WEAVE_MODEL, etc.)
  + YAML config file (optional)
  -> WeaveConfig.from_env() / WeaveConfig.from_yaml()
  -> LLMConfig consumed by LLMClient
  -> SandboxConfig consumed by execution layer
  -> WeaveConfig consumed by Orchestrator, SessionManager, DAGExecutionEngine
```

## Error Codes

No numeric error codes. Errors are standard Python exceptions:
- `yaml.YAMLError` -- Malformed YAML in `from_yaml()`.
- `FileNotFoundError` -- Missing YAML config file.
- `pydantic.ValidationError` -- Invalid field values during construction.

## Dependencies

- `pydantic` (`BaseModel`, `Field`)
- `pyyaml` (`yaml.safe_load`)
- Python stdlib: `json`, `os`, `pathlib`, `typing`

## Configuration

### Environment Variables

| Variable | Field | Default |
|----------|-------|---------|
| `ANTHROPIC_API_KEY` | `LLMConfig.api_key` | Falls back to `ANTHROPIC_AUTH_TOKEN`, then `settings-kimi.json` |
| `ANTHROPIC_AUTH_TOKEN` | `LLMConfig.api_key` | Falls back to `settings-kimi.json` |
| `ANTHROPIC_BASE_URL` | `LLMConfig.base_url` | Falls back to `settings-kimi.json` |
| `WEAVE_MODEL` | `LLMConfig.model` | Falls back to `ANTHROPIC_DEFAULT_SONNET_MODEL`, then `"claude-sonnet-4-6"` |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | `LLMConfig.model` | Falls back to `settings-kimi.json` |
| `WEAVE_EVENT_STORE` | `WeaveConfig.event_store_path` | `"./data/events"` |
| `WEAVE_ARTIFACT_PATH` | `WeaveConfig.artifact_path` | `"./data/artifacts"` |
| `WEAVE_AGENT_TIMEOUT` | `WeaveConfig.agent_timeout` | `"120"` |
| `WEAVE_MAX_CONTEXT_TOKENS` | `WeaveConfig.max_context_tokens` | `"100000"` |
| `WEAVE_DEFAULT_BACKEND` | `WeaveConfig.default_backend` | `"local"` |
| `WEAVE_BACKEND_BASE_PATH` | `WeaveConfig.backend_base_path` | `"./data/backends"` |
| `WEAVE_BACKEND_LOW` | `risk_backend_map["low"]` | `"local"` |
| `WEAVE_BACKEND_MEDIUM` | `risk_backend_map["medium"]` | `"local"` |
| `WEAVE_BACKEND_HIGH` | `risk_backend_map["high"]` | `"worktree"` |
| `WEAVE_BACKEND_CRITICAL` | `risk_backend_map["critical"]` | `"worktree"` |
| `WEAVE_NON_INTERACTIVE` | `WeaveConfig.non_interactive` | `""` (false) |
| `WEAVE_APPROVAL_TIMEOUT_SEC` | `WeaveConfig.approval_timeout_sec` | `"300"` |

### Fallback File: `~/.claude/settings-kimi.json`

```json
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "...",
    "ANTHROPIC_BASE_URL": "...",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "..."
  }
}
```

## Extension Points

- **New config sections**: Add a new `BaseModel` subclass and include it as a field in `WeaveConfig`.
- **New environment variable overrides**: Add `os.getenv(...)` to `from_env()` or field `default_factory`.
- **Alternative config sources**: Add a new classmethod to `WeaveConfig` (e.g., `from_toml()`).

## Invariants

1. All config fields have sensible defaults -- the Weave runs with zero configuration.
2. `LLMConfig.api_key` defaults to empty string; callers must validate before LLM calls.
3. `_CLAUDE_ENV` is loaded once at module import time (cached).
4. `risk_backend_map` always contains exactly four keys: `"low"`, `"medium"`, `"high"`, `"critical"`.
5. `from_env()` takes precedence over field-level `default_factory` values for explicitly set env vars.
