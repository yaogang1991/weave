# Configuration Reference

Complete reference for all environment variables, configuration options, and CLI parameters.

---

## Environment Variables

### LLM Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | *(required)* | Anthropic API key for Claude models |
| `ANTHROPIC_AUTH_TOKEN` | *(fallback)* | Alternative auth token (checked if `ANTHROPIC_API_KEY` not set) |
| `OPENAI_API_KEY` | *(optional)* | OpenAI API key (required when using OpenAI provider) |
| `HARNESS_MODEL` | `claude-sonnet-4-6` | Default LLM model to use |
| `ANTHROPIC_BASE_URL` | `""` | Custom Anthropic API base URL (for proxies) |

### Backend Configuration (M2)

| Variable | Default | Description |
|----------|---------|-------------|
| `HARNESS_DEFAULT_BACKEND` | `local` | Default execution backend: `local` or `worktree` |
| `HARNESS_BACKEND_BASE_PATH` | `./data/backends` | Base directory for backend data |
| `HARNESS_BACKEND_LOW` | `local` | Backend for LOW risk tasks |
| `HARNESS_BACKEND_MEDIUM` | `local` | Backend for MEDIUM risk tasks |
| `HARNESS_BACKEND_HIGH` | `worktree` | Backend for HIGH risk tasks |
| `HARNESS_BACKEND_CRITICAL` | `worktree` | Backend for CRITICAL risk tasks |

### Non-Interactive Mode (M1.1)

| Variable | Default | Description |
|----------|---------|-------------|
| `HARNESS_NON_INTERACTIVE` | `false` | Set to `true`, `1`, or `yes` to enable non-interactive mode |
| `HARNESS_APPROVAL_TIMEOUT_SEC` | `300` | Seconds before pending approval tickets expire |

### General

| Variable | Default | Description |
|----------|---------|-------------|
| `HARNESS_EVENT_STORE` | `./data/events` | Path to event log directory |
| `HARNESS_ARTIFACT_PATH` | `./data/artifacts` | Path to artifacts directory |
| `HARNESS_AGENT_TIMEOUT` | `120` | Timeout per agent execution in seconds |
| `HARNESS_MAX_CONTEXT_TOKENS` | `100000` | Token threshold for context truncation |

### External Config

The system also reads from `~/.claude/settings-kimi.json` as a fallback for `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL`.

---

## Configuration Models (`core/config.py`)

### LLMConfig

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `provider` | `str` | `"anthropic"` | LLM provider: `anthropic` or `openai` |
| `model` | `str` | `"claude-sonnet-4-6"` | Model identifier |
| `api_key` | `str` | *(from env)* | API key |
| `base_url` | `str` | `""` | Custom API base URL |
| `max_tokens` | `int` | `4096` | Max tokens per response |
| `temperature` | `float` | `0.3` | Sampling temperature |
| `timeout` | `int` | `120` | Request timeout in seconds |

### SandboxConfig

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `True` | Enable sandbox |
| `runtime` | `str` | `"docker"` | Runtime: `docker`, `bubblewrap`, `direct` |
| `image` | `str` | `"python:3.11-slim"` | Docker image |
| `network_mode` | `str` | `"none"` | Network mode: `none` or `bridge` |
| `memory_limit` | `str` | `"512m"` | Memory limit |
| `cpu_limit` | `float` | `1.0` | CPU limit |
| `timeout` | `int` | `300` | Sandbox timeout in seconds |
| `credential_proxy` | `bool` | `True` | Enable credential proxy |

### MCPConfig

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `servers` | `list[dict]` | `[]` | MCP server configurations |
| `auto_discover` | `bool` | `False` | Auto-discover MCP servers |

### HarnessConfig

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `llm` | `LLMConfig` | *(see above)* | LLM configuration |
| `sandbox` | `SandboxConfig` | *(see above)* | Sandbox configuration |
| `mcp` | `MCPConfig` | *(see above)* | MCP configuration |
| `event_store_path` | `str` | `"./data/events"` | Event log directory |
| `artifact_path` | `str` | `"./data/artifacts"` | Artifacts directory |
| `checkpoint_interval` | `int` | `10` | Events between checkpoints |
| `max_context_messages` | `int` | `50` | Max messages in agent context |
| `agent_timeout` | `int` | `120` | Per-agent timeout (seconds) |
| `max_context_tokens` | `int` | `100000` | Context truncation threshold |
| `log_level` | `str` | `"INFO"` | Logging level |
| `default_backend` | `str` | `"local"` | Default execution backend |
| `backend_base_path` | `str` | `"./data/backends"` | Backend data directory |
| `risk_backend_map` | `dict` | *(see env vars)* | Risk level → backend mapping |
| `non_interactive` | `bool` | `False` | Non-interactive mode |
| `approval_timeout_sec` | `int` | `300` | Approval ticket timeout |

### Config Loading

```python
# From environment variables
config = HarnessConfig.from_env()

# From YAML file
config = HarnessConfig.from_yaml("config.yaml")

# Programmatic
config = HarnessConfig(llm=LLMConfig(model="gpt-4", provider="openai"))
```

---

## CLI Commands (`main.py`)

### Global Options

| Argument | Default | Description |
|----------|---------|-------------|
| `--project PATH` | *(none)* | Project directory (loads `.harness/agents.yaml`) |
| `--max-parallel N` | `3` | Max parallel DAG node executions |
| `--max-iterations N` | `50` | Max iterations per agent |

### Core Commands

| Command | Arguments | Options | Description |
|---------|-----------|---------|-------------|
| `plan` | `requirement` | `--project` | Generate execution plan (no execution) |
| `execute` | `plan_file` | `--viz`, `--visualize`, `--no-browser` | Execute a saved plan |
| `run` | `requirement` | `--project`, `--viz`, `--visualize`, `--no-browser` | Plan + execute in one step |
| `viz` | — | `--host`, `--port`, `--no-browser` | Launch web visualizer |

### Control Plane Commands

| Command | Arguments | Options | Description |
|---------|-----------|---------|-------------|
| `submit` | `requirement` | `--project`, `--timeout`, `--max-attempts` | Submit job to queue |
| `status` | `job_id` | — | Query job status |
| `list` | — | `--status` | List jobs |
| `cancel` | `job_id` | — | Cancel a job |
| `worker` | — | `--concurrency`, `--poll-interval`, `--non-interactive` | Start worker process |
| `recover` | — | — | Recover orphaned jobs |
| `console` | — | `--host`, `--port` | Launch web console |

### Approval Commands (M1.1)

| Command | Arguments | Options | Description |
|---------|-----------|---------|-------------|
| `tickets` | — | `--status`, `--job` | List approval tickets |
| `approve` | `ticket_id` | `--reason` | Approve a ticket |
| `reject` | `ticket_id` | `--reason` | Reject a ticket |

---

## Project Configuration Files

### `.harness/agents.yaml`

Register project-specific agent types:

```yaml
agents:
  - id: ui_designer
    name: UI Designer
    skills: [ui_design, react_component_dev, tailwind_css]
    constraints: [Only modifies frontend/src/]
```

### `config.yaml` (optional)

Full configuration file (loaded via `HarnessConfig.from_yaml()`):

```yaml
llm:
  provider: anthropic
  model: claude-sonnet-4-6
  max_tokens: 4096
  temperature: 0.3
sandbox:
  enabled: true
  runtime: docker
event_store_path: ./data/events
default_backend: worktree
non_interactive: false
```

---

## Runtime Data Paths

| Path | Description |
|------|-------------|
| `./data/events/` | JSONL event logs (one per session) |
| `./data/plans/` | Generated DAG plans (JSON) |
| `./data/artifacts/` | Session artifacts |
| `./data/reports/` | Markdown reports |
| `./data/jobs/` | Job storage (all statuses including dead_letter) |
| `./data/backends/` | Backend data (worktrees, etc.) |
