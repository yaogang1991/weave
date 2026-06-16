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
| `WEAVE_MODEL` | `claude-sonnet-4-6` | Default LLM model to use |
| `ANTHROPIC_BASE_URL` | `""` | Custom Anthropic API base URL (for proxies) |

### Backend Configuration (M2)

| Variable | Default | Description |
|----------|---------|-------------|
| `WEAVE_DEFAULT_BACKEND` | `local` | Default execution backend: `local` or `worktree` |
| `WEAVE_BACKEND_BASE_PATH` | `./data/backends` | Base directory for backend data |
| `WEAVE_BACKEND_LOW` | `local` | Backend for LOW risk tasks |
| `WEAVE_BACKEND_MEDIUM` | `local` | Backend for MEDIUM risk tasks |
| `WEAVE_BACKEND_HIGH` | `worktree` | Backend for HIGH risk tasks |
| `WEAVE_BACKEND_CRITICAL` | `worktree` | Backend for CRITICAL risk tasks |

### Non-Interactive Mode (M1.1)

| Variable | Default | Description |
|----------|---------|-------------|
| `WEAVE_NON_INTERACTIVE` | `false` | Set to `true`, `1`, or `yes` to enable non-interactive mode |
| `WEAVE_APPROVAL_TIMEOUT_SEC` | `300` | Seconds before pending approval tickets expire |

### General

| Variable | Default | Description |
|----------|---------|-------------|
| `WEAVE_EVENT_STORE` | `./data/events` | Path to event log directory |
| `WEAVE_ARTIFACT_PATH` | `./data/artifacts` | Path to artifacts directory |
| `WEAVE_AGENT_TIMEOUT` | `300` | Timeout per agent/node execution in seconds (legacy; prefer `WEAVE_NODE_TIMEOUT`) |
| `WEAVE_NODE_TIMEOUT` | `300` | Default node execution timeout in seconds |
| `WEAVE_LLM_TIMEOUT` | `120` | Per-call LLM HTTP timeout in seconds (#1121) |
| `WEAVE_CLI_MAX_CONCURRENT` | `1` | Max concurrent Claude CLI processes (#1127) |
| `WEAVE_MAX_CONTEXT_TOKENS` | `100000` | Token threshold for context truncation |

### Multi-Model Routing (M3.1)

| Variable | Default | Description |
|----------|---------|-------------|
| `WEAVE_PLANNER_MODEL` | *(default model)* | Model for planner agent type |
| `WEAVE_GENERATOR_MODEL` | *(default model)* | Model for generator agent type |
| `WEAVE_EVALUATOR_MODEL` | *(default model)* | Model for evaluator agent type |
| `WEAVE_ORCHESTRATOR_MODEL` | *(default model)* | Model for orchestrator agent type |
| `WEAVE_MODEL_FALLBACK` | `claude-sonnet-4-6` | Comma-separated fallback chain |

### Agent Memory (M3.2)

| Variable | Default | Description |
|----------|---------|-------------|
| `WEAVE_MEMORY_ENABLED` | `true` | Enable/disable memory system |
| `WEAVE_MEMORY_PATH` | `./data/memory` | Memory storage directory |
| `WEAVE_MEMORY_MAX_ENTRIES` | `500` | Max entries per agent |
| `WEAVE_MEMORY_MAX_LENGTH` | `1000` | Max characters per entry |
| `WEAVE_MEMORY_TTL_DAYS` | `90` | Default entry expiry in days |
| `WEAVE_MEMORY_RETRIEVAL_LIMIT` | `10` | Max memories injected per prompt |
| `WEAVE_MEMORY_DECAY_DAYS` | `30` | Relevance decay half-life in days |

### Self-Learning (M3.3)

| Variable | Default | Description |
|----------|---------|-------------|
| `WEAVE_LEARNING_PATH` | `./data/learning` | Learning data directory |

### Impact Analysis (M3.5)

| Variable | Default | Description |
|----------|---------|-------------|
| `WEAVE_IMPACT_ENABLED` | `true` | Enable/disable impact analysis |
| `WEAVE_IMPACT_PATH` | `./data/impact` | Impact analysis data directory |

### External Config

The system also reads from a local Claude settings file (if present) as a fallback for `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL`.

---

## Configuration Models (`core/config/` package)

> Configuration was split from a single `core/config.py` into the `core/config/` package (#917): `__init__.py` (re-exports), `env.py`, `llm.py`, `timeout.py`, `domains.py`, `root.py`. `from core.config import X` continues to work unchanged.

### LLMConfig

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `provider` | `str` | `"anthropic"` | LLM provider: `anthropic` or `openai` |
| `model` | `str` | `"claude-sonnet-4-6"` | Model identifier |
| `api_key` | `str` | *(from env)* | API key |
| `base_url` | `str` | `""` | Custom API base URL |
| `max_tokens` | `int` | `4096` | Max tokens per response |
| `temperature` | `float` | `0.3` | Sampling temperature |
| `timeout` | `int` | `120` | Request timeout in seconds (`WEAVE_LLM_TIMEOUT`, #1121) |
| `max_concurrent_api` | `int` | `0` | Max concurrent API calls across parallel nodes (`WEAVE_MAX_CONCURRENT_API`; 0 = unlimited) |

### SandboxConfig

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `True` | Enable sandbox |
| `runtime` | `str` | `"local"` | Runtime: `local` or `docker` (docker not yet implemented) |
| `image` | `str` | `"python:3.11-slim"` | Docker image |
| `network_mode` | `str` | `"none"` | Network mode: `none` or `bridge` |
| `memory_limit` | `str` | `"512m"` | Memory limit |
| `cpu_limit` | `float` | `1.0` | CPU limit |
| `timeout` | `int` | `300` | Sandbox timeout in seconds |
| `credential_proxy` | `bool` | `True` | Enable credential proxy |

### MCPConfig

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `servers` | `list[MCPServerConfig]` | `[]` | MCP server configurations |
| `auto_discover` | `bool` | `False` | Auto-discover MCP servers (`WEAVE_MCP_AUTO_DISCOVER`) |
| `connection_timeout` | `int` | `30` | Seconds to wait for server startup (`WEAVE_MCP_CONNECTION_TIMEOUT`) |

### WeaveConfig

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
| `cleanup_policy` | `str` | `"on_success"` | Worktree cleanup: `on_success`/`always`/`never` (`WEAVE_CLEANUP_POLICY`) |
| `model_routing` | `ModelRoutingConfig` | *(see below)* | M3.1: Multi-model routing |
| `memory` | `MemoryConfig` | *(see below)* | M3.2: Agent memory |
| `learning` | `LearningConfig` | *(see below)* | M3.3: Self-learning |
| `impact` | `ImpactConfig` | *(see below)* | M3.5: Impact analysis |
| `node_timeout` | `NodeTimeoutConfig` | *(see below)* | Per-agent-type node/stall/activity timeouts |
| `budget` | `BudgetConfig` | *(see below)* | M4.2: Token budget control |
| `token_estimation` | `TokenEstimationConfig` | *(see below)* | M4.6: Pre-execution token estimation |
| `codex` | `CodexBackendConfig` | *(see below)* | M4.4: Codex CLI backend |
| `claude_code` | `ClaudeCodeConfig` | *(see below)* | M4.1: Claude Code backend |
| `observability` | `ObservabilityConfig` | *(see below)* | M5.1: OTel tracing |
| `watchdog` | `WatchdogConfig` | *(see below)* | M2.0: Heartbeat watchdog |
| `default_agent_backend` | `str` | `"claude_code"` | Default agent backend (`WEAVE_DEFAULT_AGENT_BACKEND`) |
| `pass_threshold` | `float` | `7.0` | Default evaluation pass threshold (`WEAVE_PASS_THRESHOLD`, 0–10) |
| `run_timeout_sec` | `int` | `1800` | Per-run wall-clock timeout (`WEAVE_RUN_TIMEOUT_SEC`) |
| `auto_format_before_eval` | `bool` | `False` | Auto-format before lint (`WEAVE_AUTO_FORMAT_BEFORE_EVAL`) |

### ModelRoutingConfig (M3.1)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `routing` | `dict[str, ModelRoute]` | `{}` | Per-agent-type model assignment |
| `fallback_chain` | `list[str]` | `["claude-sonnet-4-6"]` | Model fallback chain |

**ModelRoute** fields: `provider` (str), `model` (str), `temperature` (float | None), `max_tokens` (int | None)

```yaml
model_routing:
  routing:
    planner:
      provider: anthropic
      model: claude-opus-4-6
    generator:
      provider: anthropic
      model: claude-sonnet-4-6
  fallback_chain:
    - claude-sonnet-4-6
```

### MemoryConfig (M3.2)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `True` | Enable memory system |
| `base_path` | `str` | `"./data/memory"` | Storage directory |
| `max_entries_per_agent` | `int` | `500` | Max entries per agent (≥1) |
| `max_content_length` | `int` | `1000` | Max chars per entry (≥100) |
| `default_ttl_days` | `int` | `90` | Default entry expiry (≥1) |
| `retrieval_limit` | `int` | `10` | Max memories per prompt (≥1) |
| `decay_half_life_days` | `float` | `30.0` | Relevance decay half-life (≥1.0) |
| `auto_store` | `bool` | `True` | Auto-extract learnings after task |
| `embedding_provider` | `str` | `"local"` | Embedding provider: `local` or `openai` (#508) |
| `semantic_search_enabled` | `bool` | `True` | Use semantic search for retrieval (#508) |

### LearningConfig (M3.3)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `True` | Enable self-learning |
| `analysis_interval_hours` | `float` | `6.0` | Min hours between analyses (≥0.1) |
| `min_samples` | `int` | `5` | Min executions before analysis (≥1) |
| `max_insights` | `int` | `100` | Max insights per analysis (≥1) |
| `confidence_threshold` | `float` | `0.7` | Min confidence to store (0.0–1.0) |
| `base_path` | `str` | `"./data/learning"` | Learning data directory |

### ImpactConfig (M3.5)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `True` | Enable impact analysis |
| `coverage_threshold` | `float` | `0.7` | Pass threshold for verification (0.0–1.0) |
| `max_predicted_files` | `int` | `50` | Max predicted files (≥1) |
| `confidence_threshold` | `float` | `0.5` | Min confidence threshold (0.0–1.0) |
| `base_path` | `str` | `"./data/impact"` | Analysis data directory |

### NodeTimeoutConfig (M6.2 / M4.5)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `default_timeout` | `int` | `300` | Default node timeout in seconds (`WEAVE_NODE_TIMEOUT`, falls back to `WEAVE_AGENT_TIMEOUT`) |
| `overrides` | `dict[str, int]` | `{generator: 600, evaluator: 480}` | Per-agent-type timeout (`WEAVE_NODE_TIMEOUT_GENERATOR` / `WEAVE_NODE_TIMEOUT_EVALUATOR`) |
| `stall_timeout` | `int` | `120` | Kill node if no progress for N seconds (`WEAVE_STALL_TIMEOUT`) |
| `activity_timeout` | `int` | `600` | Semantic inactivity timeout for CLI backends (`WEAVE_ACTIVITY_TIMEOUT`) |
| `backend_stall_multipliers` | `dict[str, float]` | `{claude_code: 2.5}` | Per-backend stall multiplier for slow third-party APIs (`WEAVE_BACKEND_STALL_MULTIPLIER_CLAUDE_CODE`, must be ≥1.0, #1106) |
| `eval_scale` | `EvalTimeoutScaleConfig` | *(see code)* | Dynamic evaluator timeout scaling (`WEAVE_EVAL_TIMEOUT_*`) |

### WatchdogConfig (M2.0)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `True` | Enable watchdog (`WEAVE_WATCHDOG_ENABLED`) |
| `heartbeat_interval_sec` | `float` | `30.0` | Heartbeat interval (`WEAVE_WATCHDOG_INTERVAL`) |
| `heartbeat_miss_threshold` | `int` | `12` | Misses before unhealthy (`WEAVE_WATCHDOG_THRESHOLD`) |
| `alert_threshold_fraction` | `float` | `0.5` | Fraction at which `heartbeat_missed` fires (`WEAVE_WATCHDOG_ALERT_FRACTION`) |

### BudgetConfig (M4.2)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `True` | Enable budget control (`WEAVE_BUDGET_ENABLED`) |
| `total_tokens` | `int` | `0` | Total token budget per run; 0 = unlimited (`WEAVE_BUDGET_TOKENS`) |
| `warning_threshold` | `float` | `0.8` | Emit warning at this fraction (`WEAVE_BUDGET_WARNING_THRESHOLD`) |
| `per_node_token_limit` | `int` | `0` | Per-node token limit; 0 = unlimited (`WEAVE_BUDGET_PER_NODE_TOKENS`) |

### ClaudeCodeConfig (M4.1 / M6.1)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `False` | Enable Claude Code backend (`WEAVE_CLAUDE_CODE_ENABLED`) |
| `cli_path` | `str` | `"claude"` | Path to claude CLI binary (`WEAVE_CLAUDE_CODE_PATH`) |
| `permission_mode` | `str` | `"default"` | `default` / `plan` / `bypassPermissions` |
| `max_budget_usd` | `float` | `0.0` | Max USD budget per node (0 = unlimited) |
| `timeout_override` | `int` | `0` | Per-node timeout override in seconds (0 = use node_timeout) |

### CodexBackendConfig (M4.4)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `False` | Enable Codex backend (`WEAVE_CODEX_ENABLED`) |
| `binary_path` | `str` | `"codex"` | Path to codex binary (`WEAVE_CODEX_BINARY_PATH`) |
| `model` | `str` | `"codex-mini"` | Codex model (`WEAVE_CODEX_MODEL`) |
| `sandbox_mode` | `str` | `"workspace-write"` | Sandbox mode (`WEAVE_CODEX_SANDBOX`) |
| `timeout` | `int` | `600` | Per-invocation timeout in seconds |

### ObservabilityConfig (M5.1)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `True` | Enable OTel tracing (`WEAVE_OBSERVABILITY_ENABLED`) |
| `otlp_endpoint` | `str \| None` | `None` | OTLP collector endpoint (`WEAVE_OTLP_ENDPOINT`) |

### TokenEstimationConfig (M4.6)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `True` | Use Anthropic `count_tokens()` API for estimation |
| `fallback_to_heuristic` | `bool` | `True` | Fall back to char/3.5 heuristic on API failure |
| `target_budget` | `int` | `8192` | Default token budget per node |

### Config Loading

```python
# From environment variables
config = WeaveConfig.from_env()

# From YAML file
config = WeaveConfig.from_yaml("config.yaml")

# Programmatic
config = WeaveConfig(llm=LLMConfig(model="gpt-4", provider="openai"))
```

---

## CLI Commands (`main.py`)

### Global Options

| Argument | Default | Description |
|----------|---------|-------------|
| `--project PATH` | *(none)* | Project directory (loads `.weave/agents.yaml`) |
| `--max-parallel N` | `3` | Max parallel DAG node executions |
| `--max-iterations N` | `50` | Max iterations per agent |

### Core Commands

| Command | Arguments | Options | Description |
|---------|-----------|---------|-------------|
| `plan` | `requirement` | `--project` | Generate execution plan (no execution) |
| `execute` | `plan_file` | `--viz`, `--visualize`, `--no-browser` | Execute a saved plan |
| `run` | `requirement` | `--project`, `--viz`, `--visualize`, `--no-browser` | Plan + execute in one step |
| `viz` | — | `--host`, `--port`, `--no-browser` | Launch web visualizer |
| `serve` | — | — | Start MCP server (stdio transport, #512) |

### Control Plane Commands

| Command | Arguments | Options | Description |
|---------|-----------|---------|-------------|
| `submit` | `requirement` | `--project`, `--timeout`, `--max-attempts` | Submit job to queue |
| `status` | `job_id` | — | Query job status |
| `list` | — | `--status` | List jobs |
| `cancel` | `job_id` | — | Cancel a job |
| `worker` | — | `--concurrency`, `--poll-interval`, `--non-interactive` | Start worker process |
| `recover` | — | — | Recover orphaned jobs after restart |
| `console` | — | `--host`, `--port` | Launch web console |

### Approval Commands (M1.1)

| Command | Arguments | Options | Description |
|---------|-----------|---------|-------------|
| `tickets` | — | `--status`, `--job` | List approval tickets |
| `approve` | `ticket_id` | `--reason` | Approve a ticket |
| `reject` | `ticket_id` | `--reason` | Reject a ticket |

### Memory Commands (M3.2)

| Command | Arguments | Options | Description |
|---------|-----------|---------|-------------|
| `memory-search` | `query` | `--agent`, `--scope`, `--type`, `--limit` | Search agent memory |
| `memory-list` | — | `--agent`, `--scope`, `--type` | List memory entries |
| `memory-stats` | — | — | Show memory system statistics |
| `memory-add` | `content` | `--type`, `--scope`, `--agent`, `--keywords` | Add manual memory entry |
| `memory-cleanup` | — | — | Run memory maintenance |

### Learning Commands (M3.3)

| Command | Arguments | Options | Description |
|---------|-----------|---------|-------------|
| `learning-analyze` | — | — | Trigger a learning analysis run |
| `learning-insights` | — | `--limit` | List stored learning insights |
| `learning-status` | — | — | Show learning system status |

### Template Commands (M3.4)

| Command | Arguments | Options | Description |
|---------|-----------|---------|-------------|
| `templates` | — | `--name` | List templates or show template details |

Template variables are passed via the `plan` or `run` command:

```bash
python main.py run "Build API" --template build_api --var feature=Todo --var language=Python
python main.py plan "Fix bug" --template fix_bug --var bug="null pointer"
```

### Impact Analysis Commands (M3.5)

| Command | Arguments | Options | Description |
|---------|-----------|---------|-------------|
| `impact-predict` | `requirement` | `--project` | Predict impact of a requirement |
| `impact-graph` | — | `--project` | Show project dependency graph |
| `impact-history` | — | — | List past impact predictions |

### Skills Commands (M3.6)

| Command | Arguments | Options | Description |
|---------|-----------|---------|-------------|
| `skills` | — | `--agent`, `--project` | List available skills |
| `skill` | `name` | `--var`, `--project`, `--max-parallel`, `--max-iterations` | Invoke a skill (creates plan+run) |

---

## Project Configuration Files

### `.weave/agents.yaml`

Register project-specific agent types:

```yaml
agents:
  - id: ui_designer
    name: UI Designer
    skills: [ui_design, react_component_dev, tailwind_css]
    constraints: [Only modifies frontend/src/]
```

### `config.yaml` (optional)

Full configuration file (loaded via `WeaveConfig.from_yaml()`):

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
# M3.1: Multi-model routing
model_routing:
  routing:
    planner:
      provider: anthropic
      model: claude-opus-4-6
    generator:
      provider: anthropic
      model: claude-sonnet-4-6
  fallback_chain:
    - claude-sonnet-4-6
# M3.2: Agent memory
memory:
  enabled: true
  base_path: ./data/memory
  max_entries_per_agent: 500
  max_content_length: 1000
  default_ttl_days: 90
  retrieval_limit: 10
  decay_half_life_days: 30.0
  auto_store: true
# M3.3: Self-learning
learning:
  enabled: true
  analysis_interval_hours: 6.0
  min_samples: 5
  max_insights: 100
  confidence_threshold: 0.7
  base_path: ./data/learning
# M3.5: Impact analysis
impact:
  enabled: true
  coverage_threshold: 0.7
  max_predicted_files: 50
  confidence_threshold: 0.5
  base_path: ./data/impact
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
| `./data/memory/` | M3.2: Agent memory entries |
| `./data/memory/global/` | Cross-session global memories |
| `./data/memory/agents/{type}/` | Per-agent private memories |
| `./data/memory/sessions/{id}/` | Per-session shared memories |
| `./data/learning/` | M3.3: Learning analysis state |
| `./data/impact/` | M3.5: Impact analysis data |
