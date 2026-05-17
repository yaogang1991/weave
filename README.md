# Weave

Intelligent Multi-Agent Orchestration for Autonomous Software Development.

Based on [Anthropic Managed Agents](https://www.anthropic.com/engineering/managed-agents) architecture, a self-hosted unattended software development workflow.

> **Weave** — DAG 是织机，Agent 是梭子，编排（orchestrate）本意是编织。多个 Agent 按角色协作，把需求编织成完整的软件。

## Core Design Principles

1. **Artifact-Centric** — All state externalized to event logs and file artifacts, model context is just cache
2. **Minimal by Design** — "dumb loop" as core, add complexity on demand
3. **Defense-in-Depth** — Tool layer + Weave layer + execution layer multi-layer defense
4. **Trust-First** — Audit logs, rollback, monitoring as first-class citizens
5. **Human-on-the-Loop** — Plan-level human supervision, execution-level automated running
6. **Contract-Driven** — Predefined success criteria, automated evaluation

## Architecture

```
+------------------------------------------------------------------+
|                        Orchestrator Layer                         |
|  +-------------+  +-------------+  +---------------------+      |
|  |   Planner   |  |  Generator  |  |     Evaluator       |      |
|  +-------------+  +-------------+  +---------------------+      |
+------------------------------------------------------------------+
|              Session Manager (Append-Only Event Log)              |
+------------------------------------------------------------------+
|                      Weave Core (Dumb Loop)                       |
|  +-------------+  +-------------+  +---------------------+      |
|  |   Agent     |  |   Tool      |  |     Guardrails      |      |
|  |   Worker    |<--|   Registry  |<--|  (Permission/Risk)  |      |
|  +-------------+  +-------------+  +---------------------+      |
+------------------------------------------------------------------+
|  +-------------+  +-------------+  +---------------------+      |
|  |   Sandbox   |  |   Git       |  |     Reporter        |      |
|  +-------------+  +-------------+  +---------------------+      |
+------------------------------------------------------------------+
|  +-------------+  +-------------+  +---------------------+      |
|  |   Memory    |  |  Learning   |  |  Impact Analysis    |      |
|  |  (M3.2)     |  |  (M3.3)     |  |  (M3.5)             |      |
|  +-------------+  +-------------+  +---------------------+      |
+------------------------------------------------------------------+
```

## Quick Start

```bash
# 1. Clone & install
gh repo clone yaogang1991/weave
cd weave
pip install -r requirements.txt

# 2. Set API Key
export ANTHROPIC_API_KEY="sk-ant-..."

# 3. One-command plan + execute
python main.py run "Build a REST API for todo items"

# Or plan first, then execute
python main.py plan "Build a REST API for user authentication"
python main.py execute ./data/plans/plan_xxx.json
```

## M1 Personal Mode Guide

### Worker Mode (Unattended)

```bash
# Terminal 1: Start worker
python main.py worker --concurrency 1

# Terminal 2: Submit task
python main.py submit "Build a REST API for user auth"

# Terminal 3: Monitor
python main.py list --status running
python main.py tickets --status pending
```

### Non-Interactive Mode

```bash
export HARNESS_NON_INTERACTIVE=true
python main.py worker --non-interactive
```

### Command Cheat Sheet

| Command | Description |
|---------|-------------|
| `submit "<requirement>"` | Submit a new task |
| `status <job_id>` | View task status |
| `list [--status STATUS]` | List tasks |
| `cancel <job_id>` | Cancel a task |
| `worker [--concurrency N]` | Start Worker |
| `plan "<requirement>"` | Generate execution plan |
| `run "<requirement>"` | One-command plan + execute |
| `run "..." --template build_api` | Run from template (M3.4) |
| `tickets` / `approve` / `reject` | Approval workflow |
| `memory-search` / `memory-add` | Agent memory (M3.2) |
| `learning-analyze` / `learning-insights` | Self-learning (M3.3) |
| `impact-predict` / `impact-graph` | Impact analysis (M3.5) |

## M3 Features

### Multi-Model Routing (M3.1)

```bash
export HARNESS_PLANNER_MODEL="claude-opus-4-6"
export HARNESS_GENERATOR_MODEL="gpt-4"
```

### Agent Memory (M3.2)

```bash
python main.py memory-search "database schema"
python main.py memory-add "Always use SQLAlchemy async session" --type fact --scope global
```

### Self-Learning (M3.3)

```bash
python main.py learning-analyze
python main.py learning-insights
```

### DAG Templates (M3.4)

```bash
python main.py templates
python main.py run "Build Todo API" --template build_api --var feature=Todo
```

### Impact Analysis (M3.5)

```bash
python main.py impact-predict "Refactor the DAG engine" --project .
python main.py impact-graph --project .
```

## Security Model

| Layer | Component | Function |
|-------|-----------|----------|
| Model Layer | Agent Worker | Constitutional AI, pause when uncertain |
| Tool Layer | Tool Registry | Least privilege, allow/deny lists |
| Weave Layer | Guardrails | Permission modes (plan/default/auto/dontAsk) |
| Execution Layer | Sandbox | Docker isolation, credential proxy |

## Module Reference

| Module | Responsibility |
|--------|---------------|
| `core/` | Pydantic models, configuration management |
| `session/` | Event storage, state recovery, checkpoint |
| `agent/` | LLM API calls, dumb loop |
| `tools/` | Built-in tools + MCP integration |
| `guardrails/` | Risk classification, permission control |
| `evaluator/` | Automated evaluation, test execution |
| `orchestrator/` | Workflow orchestration, Stage transitions |
| `reporter/` | Audit logs, report generation |
| `memory/` | M3.2: Agent memory (store, retrieve, share) |
| `learning/` | M3.3: Execution pattern analysis and optimization |
| `templates/` | M3.4: Reusable DAG templates (YAML + variables) |
| `analysis/` | M3.5: Dependency graph, impact prediction, change verification |
| `visualizer/` | M2.3: Web console (FastAPI + WebSocket) |
| `backend/` | M2: Execution backend abstraction (local/worktree) |
| `control_plane/` | Job queue, worker, approval tickets |

## Relationship with Anthropic Managed Agents

| Feature | Anthropic Managed Agents | Weave |
|---------|-------------------------|-------|
| Running Location | Anthropic Cloud | Local/Self-hosted |
| Pricing | $0.08/session-hour + tokens | Token cost only |
| Session | Managed event log | Local JSONL |
| Sandbox | Managed container | Docker/Local |
| LLM | Claude series | Claude/OpenAI compatible |
| MCP | Native support | Client integration |

Suitable for:
- Enterprises needing full infrastructure control
- Local development/prototyping
- CI/CD integration
- Custom security policies

### Known Limitations

- Single-user scenario (no multi-tenancy)
- No persistent database (uses JSON files)
- Single-machine execution (no distribution)
- Impact analysis only supports Python import resolution

## License

[Apache License 2.0](LICENSE)
