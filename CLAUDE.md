# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Self-hosted unattended software development harness based on [Anthropic Managed Agents](https://www.anthropic.com/engineering/managed-agents) architecture. Orchestrates multiple LLM agents (planner, generator, evaluator) to automate the full software dev lifecycle via LLM-driven dynamic DAG generation and execution.

Python 3.11+, Pydantic models, async/await throughout.

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

# With project-specific agents
python main.py run "Add OAuth2 support" --project ./my-project --max-parallel 5

# Tests
python -m pytest -v --tb=short

# Lint
flake8 --max-line-length=100

# Coverage
python -m pytest --cov=. --cov-report=term-missing
```

Environment variables: `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` (required), `HARNESS_MODEL` (optional, default: claude-sonnet-4-6).

## Architecture

Four-layer architecture:

```
Orchestrator Layer (LLM-driven planning, DAG generation)
    ↓
Session Manager (append-only JSONL event log, state replay)
    ↓
Harness Core / Dumb Loop (Agent Worker + Tool Registry + Guardrails)
    ↓
Execution Layer (Sandbox, Git, Reporter)
```

**Flow**: User requirement → `IntelligentOrchestrator.plan()` queries `AgentRegistry`, generates a `DAG` → `DAGExecutionEngine` topologically sorts and executes levels in parallel via `AgentPool` → failures go back to orchestrator via `adapt_to_failure()`.

**Key module responsibilities**:
- `core/models.py` — All data models: DAG, DAGNode, AgentCapability, HandoffArtifact, events, session state, guardrails
- `core/config.py` — HarnessConfig, LLMConfig, SandboxConfig
- `core/agent_registry.py` — Agent capability registry (defaults: planner/generator/evaluator; extensible via `.harness/agents.yaml`)
- `core/dag_engine.py` — Topological sort, parallel execution with `asyncio.gather`, failure callback
- `core/llm_client.py` — Unified LLM client (Anthropic/OpenAI)
- `orchestrator/intelligent_orchestrator.py` — LLM-driven planning and failure adaptation
- `agent/agent_pool.py` — Worker instance pool with independent contexts
- `agent/worker.py` — Single agent LLM call loop
- `tools/registry.py` — Built-in tools (read/write/edit/bash/glob/grep/git) + MCP extension point
- `guardrails/policy.py` — Four-layer defense: RiskLevel, PermissionMode (plan/default/accept_edits/auto/dont_ask)
- `session/store.py` — Append-only JSONL event storage, state recovery via replay
- `evaluator/engine.py` — Automated success criteria checking (pytest, flake8, coverage)

## Conventions

- **Language**: Docstrings and code comments in English. User-facing docs (README, ARCHITECTURE) in Chinese.
- **Type annotations**: Use Python 3.10+ syntax (`str | None`, `list[dict[str, Any]]`).
- **Data models**: All must use `pydantic.BaseModel` with `model_dump()` serialization. Defined in `core/models.py`.
- **Event naming**: `{domain}.{action}` convention (e.g., `workflow.stage_start`, `agent.tool_use`).
- **Error handling**: Tools return `ToolResult` wrapper (success/failure), never throw exceptions that break the main loop. DAG engine catches exceptions via `traceback.format_exc()` and writes to node `error` field.
- **No circular imports**: Modules layered by responsibility (`core/` → `agent/` → `orchestrator/` → `tools/`).

## When Modifying Code

- **Adding a tool**: Register in `tools/registry.py`, add risk level in `guardrails/policy.py` `RISK_MAP`.
- **Adding a default agent type**: Add to `core/agent_registry.py` `_register_defaults()`, update prompt template in `orchestrator/intelligent_orchestrator.py`.
- **Data model changes**: Edit `core/models.py` — it is the single source of truth for all models.
- **State is externalized**: All runtime state lives in `./data/events/` (JSONL) and `./data/artifacts/`. Agent context windows are just cache.

## Runtime Data

- `./data/events/` — Session event logs (JSONL)
- `./data/plans/` — Generated DAG plans (JSON)
- `./data/artifacts/` — Session artifacts
- `./data/reports/` — Markdown reports
