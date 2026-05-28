# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Self-hosted unattended software development system based on [Anthropic Managed Agents](https://www.anthropic.com/engineering/managed-agents) architecture. Orchestrates multiple LLM agents (planner, generator, evaluator) to automate the full software dev lifecycle via LLM-driven dynamic DAG generation and execution.

Python 3.11+, Pydantic models, async/await throughout.

**Current version:** M6.4 (cleanup + documentation update). See `docs/roadmap.md` for milestone history.

## Commands

```bash
# Install
pip install -r requirements.txt

# Run (plan + execute in one step)
python main.py run "Build a REST API for todo items"

# Plan only
python main.py plan "Build a REST API for user authentication"

# Execute a saved plan
python main.py execute ./data/plans/plan_xxx.json

# Worker mode (unattended)
python main.py worker --concurrency 1
python main.py worker --non-interactive

# Submit to queue
python main.py submit "Build a REST API for user auth"

# Status / List / Cancel
python main.py status <job_id>
python main.py list --status running
python main.py cancel <job_id>

# Recover orphaned jobs
python main.py recover

# Approval tickets (M1.1)
python main.py tickets
python main.py approve <ticket_id>
python main.py reject <ticket_id>

# Web console (M2.3)
python main.py viz

# DAG templates (M3.4)
python main.py templates
python main.py templates --name build_api
python main.py run "Build API" --template build_api --var feature=Todo --var language=Python
python main.py plan "Fix bug" --template fix_bug --var bug="null pointer"

# Skills (M3.6)
python main.py skills
python main.py skill review_code --var file=src/main.py

# Impact analysis (M3.5)
python main.py impact-predict "Fix bug in DAG engine" --project .
python main.py impact-graph --project .
python main.py impact-history

# With project-specific agents
python main.py run "Add OAuth2 support" --project ./my-project --max-parallel 5

# Tests
python -m pytest -v --tb=short

# Lint
flake8 --max-line-length=100

# Coverage
python -m pytest --cov=. --cov-report=term-missing
```

Environment variables: `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` (required), `WEAVE_MODEL` (optional, default: claude-sonnet-4-6), `WEAVE_DEFAULT_BACKEND` (optional: local|worktree), `WEAVE_NON_INTERACTIVE` (optional: true|false).

## Architecture

Four-layer architecture:

```
Orchestrator Layer (LLM-driven planning, DAG generation)
    ↓
Session Manager (append-only JSONL event log, state replay)
    ↓
Weave Core / Dumb Loop (Agent Worker + Tool Registry + Guardrails)
    ↓
Execution Layer (Backend abstraction, Sandbox, Git, Reporter)
```

**Flow**: User requirement → `IntelligentOrchestrator.plan()` queries `AgentRegistry`, generates a `DAG` → `DAGExecutionEngine` topologically sorts and executes levels in parallel via `AgentPool` → Watchdog monitors heartbeats (M2) → failures go back to orchestrator via `adapt_to_failure()`.

**Key module responsibilities**:

### core/ — Models, configuration, engine
- `core/models.py` — Base re-exports; domain models live in split files below
- `core/dag_models.py` — DAG, DAGNode, DAGEdge, DAGTemplate models
- `core/event_models.py` — Event types, session state, EventType enum
- `core/guardrail_models.py` — RiskLevel, PermissionMode, GuardrailConfig
- `core/memory_models.py` — MemoryEntry, MemoryScope, MemoryType
- `core/analysis_models.py` — ImpactRiskLevel, ImpactScope, VerificationResult
- `core/eval_models.py` — SuccessCriterion, EvaluationResult
- `core/tool_models.py` — ToolInfo, ToolResult, MCPToolInfo
- `core/mcp_models.py` — MCPToolInfo, MCPServerStatus
- `core/artifact_handoff.py` — HandoffArtifact, artifact collection/transfer
- `core/exceptions.py` — Custom exception hierarchy
- `core/config.py` — WeaveConfig, LLMConfig, SandboxConfig, MemoryConfig, LearningConfig, ImpactConfig, NodeTimeoutConfig (stall_timeout with dynamic complexity scaling)
- `core/agent_registry.py` — Agent capability registry (defaults: planner/generator/evaluator; extensible via `.weave/agents.yaml`)
- `core/dag_engine.py` — Topological sort, parallel execution with `asyncio.gather`, failure callback
- `core/node_executor.py` — 3-stage node execution pipeline: prepare → execute → evaluate (ADR-0015)
- `core/evaluation_pipeline.py` — Post-execution evaluation: token recording, artifacts, zero-output, evaluator, quality gate (ADR-0015)
- `core/quality_gate.py` — Post-node quality checks (extracted from dag_engine)
- `core/retry_policy.py` — Retry/backoff logic (extracted from dag_engine)
- `core/progress.py` — ProgressReport, ProgressTracker (Filter/Observer pub/sub), StallDetector, AnomalyDetector, AuditLogger (M4.5)
- `core/subprocess_runner.py` — SubprocessResult, run_with_progress: universal subprocess execution with progress reporting (M4.5)
- `core/backend_models.py` — BackendContext, BackendResult, BackendStatus: models for Brain/Hands separation (M6.1)
- `core/node_guardrails.py` — Node-level guardrail checks (M6.2)
- `core/activity_detector.py` — Detect meaningful events in backend output (M6.5)
- `core/watchdog.py` — Watchdog coroutine for heartbeat monitoring (M2)
- `core/llm_client.py` — Unified LLM client (Anthropic/OpenAI)
- `core/llm_router.py` — M3.1: Multi-model routing per agent type
- `core/project_config.py` — `.weave/config.yaml` loader (runtime parameters, hooks, guardrails)

### cli/ — CLI command handlers (split from main.py)
- `cli/__init__.py` — Exports all cmd_* functions
- `cli/execution.py` — plan, execute, run, viz commands
- `cli/jobs.py` — submit, status, list, cancel, worker, recover, console commands
- `cli/approval.py` — tickets, approve, reject commands
- `cli/memory.py` — memory-search/list/stats/add/cleanup commands
- `cli/learning.py` — learning-analyze/insights/status commands
- `cli/impact.py` — impact-predict/graph/history commands
- `cli/skills.py` — skills, skill, templates commands
- `cli/utils.py` — Shared CLI helpers

### control_plane/ — Job lifecycle management
- `control_plane/models.py` — Job/Run data models, status enums
- `control_plane/repository.py` — Persistent storage with atomic writes
- `control_plane/service.py` — RunService: execution orchestration (submit/run/resume)
- `control_plane/hooks.py` — Execution hooks: MemoryHook, LearningHook, ImpactHook (lifecycle callbacks)
- `control_plane/execution_factory.py` — Factory for creating orchestrator + engine per run
- `control_plane/job_lifecycle.py` — Job state transitions and lifecycle management
- `control_plane/run_lifecycle.py` — Run state transitions
- `control_plane/service.py` — `_write_job_result()` module-level function for job result artifact generation (inlined from deleted `job_result.py` in #572)
- `control_plane/backend_lifecycle.py` — Backend setup/cleanup integration
- `control_plane/worker.py` — Worker queue consumer with lease mechanism
- `control_plane/worker_executor.py` — Single job execution within worker (extracted from worker)
- `control_plane/worker_recovery.py` — Orphaned job recovery logic (extracted from worker)
- `control_plane/approval.py` — Approval ticket system (M1.1): ApprovalTicket, ApprovalRepository

### orchestrator/ — LLM-driven planning
- `orchestrator/intelligent_orchestrator.py` — LLM-driven planning and failure adaptation
- `orchestrator/plan_validator.py` — DAG structural validation and auto-fix
- `orchestrator/llm_utils.py` — LLM helper utilities (token counting, message pruning)
- `orchestrator/prompts/` — Prompt templates (planning.md, adaptation.md, replan.md)

### agent/ — LLM agent layer
- `agent/agent_pool.py` — Worker instance pool with independent contexts, memory injection (deprecated M6.3, retained for BuiltinBackend)
- `agent/worker.py` — Single agent LLM call loop (deprecated M6.3, retained for BuiltinBackend)
- `agent/prompts.py` — Agent system prompts (retained for BuiltinBackend backward compat)
- `agent/backends/base.py` — AgentBackend abstract interface (M6.3)
- `agent/backends/builtin.py` — BuiltinBackend: wraps LightweightLLMCaller or AgentPool for node execution (M6.3)
- `agent/backends/claude_code.py` — ClaudeCodeBackend: delegates node execution to Claude Code (M6.1, M6.5)
- `agent/backends/codex.py` — CodexBackend: delegates node execution to Codex CLI (M6.1)
- `agent/backends/registry.py` — BackendRegistry: manages AgentBackend instances with fallback (M6.3)
- `agent/backends/stderr_tail.py` — StderrTail: tails stderr for progress events (M6.6)
- `agent/backends/stream_parser.py` — StreamParser: parses streaming JSON events from CLI backends (M6.5)
- `agent/backends/bidirectional.py` — Bidirectional comms protocol for session resume (M6.7)

### evaluator/ — Automated evaluation
- `evaluator/engine.py` — Evaluation orchestration
- `evaluator/runner.py` — Test/lint execution runner
- `evaluator/models.py` — Evaluator-specific models
- `evaluator/artifact.py` — Artifact evaluation
- `evaluator/compat.py` — Backward compatibility shims
- `evaluator/checkers/` — Criterion checkers (base, file_exists, bugfix_patterns)
- `evaluator/lint/` — Lint result parsing

### tools/ — Built-in tools (retained for BuiltinBackend and impact analysis)
- `tools/registry.py` — Tool registration (read/write/edit/bash/glob/grep/git) + MCP extension point (M6.4: write/edit/bash retained for BuiltinBackend compat)
- `tools/command_runner.py` — Shell command execution with sandbox support

### backend/ — Execution environment abstraction
- `backend/base.py` — ExecutionBackend abstract interface, WorkspaceIsolation, ExecutionSandbox enums
- `backend/local.py` — Local execution backend
- `backend/worktree.py` — Git worktree isolation backend
- `backend/docker_stub.py` — Docker backend stub (reserved)
- `backend/sandbox.py` — SandboxProvider / LocalSandbox / DockerSandbox (orthogonal to workspace)
- `backend/lifecycle.py` — BackendManager: config-driven selection, risk mapping, auto-fallback

### mcp/ — Model Context Protocol
- `mcp/client.py` — MCP server connection, tool discovery, and execution via stdio transport
- `mcp/server.py` — Lightweight MCP Server framework (JSON-RPC over stdio)
- `mcp/analysis_tools.py` — M4.3: Analysis tools (dependency_graph, impact_predict, impact_graph) for external workers
- `mcp/weave_tools_server.py` — M4.3: Standalone MCP server entry point for analysis tools only
- `mcp/config_export.py` — M6.8: MCP config exporter for passing server config to external backends

### skills/ — YAML skill definitions
- `skills/registry.py` — SkillRegistry: discover, load, instantiate YAML skills with variable substitution

### Other modules
- `guardrails/policy.py` — Four-layer defense: RiskLevel, PermissionMode (plan/default/accept_edits/auto/dont_ask), unified 3-state entry (M1.1)
- `session/store.py` — Append-only JSONL event storage, state recovery via replay, session snapshots
- `monitoring/metrics.py` — Metrics aggregation
- `monitoring/alerts.py` — Alerting system (failure, heartbeat, approval)
- `memory/store.py` — M3.2: Persistent memory store with atomic writes (file-per-entry)
- `memory/manager.py` — M3.2: High-level memory operations (store/retrieve/inject/extract)
- `memory/sharing.py` — M3.2: Cross-agent memory sharing (PRIVATE→SESSION→GLOBAL promotion)
- `learning/analyzer.py` — M3.3: Execution pattern analysis (failure/success/agent/planning)
- `learning/optimizer.py` — M3.3: Insight → memory conversion, planning hints for orchestrator
- `learning/scheduler.py` — M3.3: Periodic analysis trigger and state management
- `templates/library.py` — M3.4: TemplateRegistry for reusable YAML DAG templates with variable substitution
- `analysis/dependency_graph.py` — M3.5: File-level dependency graph using Python ast import parsing
- `analysis/impact_predictor.py` — M3.5: Impact prediction engine (keyword matching + dependency expansion)
- `analysis/change_verifier.py` — M3.5: Post-execution change verification with coverage metrics
- `visualizer/server.py` — FastAPI web console (M2.3)
- `visualizer/cli_renderer.py` — CLI DAG visualization
- `visualizer/event_bridge.py` — WebSocket event bridge
- `reporter/logger.py` — Session report generation

## Conventions

- **Language**: Docstrings and code comments in English. User-facing docs (README, ARCHITECTURE) in Chinese.
- **Type annotations**: Use Python 3.10+ syntax (`str | None`, `list[dict[str, Any]]`).
- **Data models**: Use `pydantic.BaseModel` with `model_dump()` serialization. Domain models in `core/*_models.py` files, re-exported via `core/models.py`.
- **Event naming**: `{domain}.{action}` convention (e.g., `workflow.stage_start`, `agent.tool_use`, `node.heartbeat`).
- **Error handling**: Tools return `ToolResult` wrapper (success/failure), never throw exceptions that break the main loop. DAG engine catches exceptions via `traceback.format_exc()` and writes to node `error` field.
- **No circular imports**: Modules layered by responsibility (`core/` → `agent/` → `orchestrator/` → `tools/`).
- **Immutability**: All state mutations create new objects. Never mutate shared state in-place.

## When Modifying Code

- **Adding a tool**: Register in `tools/registry.py`, add risk level in `guardrails/policy.py` `RISK_MAP`.
- **Adding a default agent type**: Add to `core/agent_registry.py` `_register_defaults()`, update prompt in `agent/prompts.py`, update orchestrator prompt in `orchestrator/prompts/planning.md`.
- **Data model changes**: Add to the appropriate `core/*_models.py` file. Re-export from `core/models.py` for backward compatibility.
- **Adding an execution backend**: Extend `backend/base.py` `ExecutionBackend`, register in `backend/lifecycle.py` `BackendManager`.
- **Adding an execution hook**: Extend `ExecutionHook` in `control_plane/hooks.py`, register in `control_plane/service.py` `_register_hooks()`.
- **Adding a CLI command**: Add handler in `cli/` subdirectory, register subparser in `main.py`.
- **State is externalized**: All runtime state lives in `./data/events/` (JSONL) and `./data/artifacts/`. Agent context windows are just cache.
- **Memory system**: `memory/store.py` handles persistence (atomic writes), `memory/manager.py` is the primary API. Memory is injected into agent system prompts via `memory_manager.get_context_for_agent()` + `format_memory_prompt()` in `agent/agent_pool.py`.

## Runtime Data

- `./data/events/` — Session event logs (JSONL)
- `./data/plans/` — Generated DAG plans (JSON)
- `./data/artifacts/` — Session artifacts
- `./data/reports/` — Markdown reports
- `./data/queue/` — Job queue (pending/leased/dead)
- `./data/memory/` — M3.2: Agent memory entries (global/agents/{type}/sessions/{id}/)
- `./data/impact/` — M3.5: Impact analysis data
- `./data/backends/` — M2: Backend data (worktrees, etc.)
- `./data/learning/` — M3.3: Learning analysis state
