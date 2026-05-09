# Unattended Software Development Harness

Based on [Anthropic Managed Agents](https://www.anthropic.com/engineering/managed-agents) architecture, a self-hosted unattended software development workflow Harness.

## Core Design Principles

1. **Artifact-Centric** — All state externalized to event logs and file artifacts, model context is just cache
2. **Minimal by Design** — "dumb loop" as core, add complexity on demand
3. **Defense-in-Depth** — Tool layer + Harness layer + execution layer multi-layer defense
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
|                      Harness Core (Dumb Loop)                     |
|  +-------------+  +-------------+  +---------------------+      |
|  |   Agent     |  |   Tool      |  |     Guardrails      |      |
|  |   Worker    |<--|   Registry  |<--|  (Permission/Risk)  |      |
|  +-------------+  +-------------+  +---------------------+      |
+------------------------------------------------------------------+
|  +-------------+  +-------------+  +---------------------+      |
|  |   Sandbox   |  |   Git       |  |     Reporter        |      |
|  +-------------+  +-------------+  +---------------------+      |
+------------------------------------------------------------------+
```

## M1 Personal Mode Guide

### Quick Start (Up and Running in 10 Minutes)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set API Key
export ANTHROPIC_API_KEY="sk-ant-..."

# 3. Submit a task
python main.py submit "Build a REST API for user authentication"
# Output: {"job_id": "job_abc123", "status": "queued", "message": "Job submitted"}

# 4. Start Worker (auto-executes tasks)
python main.py worker --concurrency 1

# 5. Check task status
python main.py status job_abc123

# 6. List all tasks
python main.py list
python main.py list --status queued
python main.py list --status succeeded
python main.py list --status failed

# 7. Cancel a task
python main.py cancel job_abc123
```

### Command Cheat Sheet

| Command | Description |
|---------|-------------|
| `submit "<requirement>"` | Submit a new task |
| `status <job_id>` | View task status |
| `list [--status STATUS]` | List tasks |
| `cancel <job_id>` | Cancel a task |
| `worker [--concurrency N]` | Start Worker |
| `recover` | Manually recover orphaned tasks |
| `plan "<requirement>"` | Generate execution plan (no execution) |
| `run "<requirement>"` | One-command plan + execute |

### Personal Mode Features

- **Unattended**: Throw tasks in, worker auto-runs to completion
- **Failure Recovery**: timeout/retry/dead-letter mechanism
- **High-Risk Confirmation**: HIGH risk operations require confirmation (or whitelist auto-pass)
- **Metrics & Alerts**: Auto-collect success rate, duration, and other metrics
- **Restart Recovery**: Process interruption can recover to processable state

### Known Limitations (M2 Items)

- Single-user scenario (no multi-tenancy)
- No persistent database (uses JSON files)
- No Web UI (CLI only)
- Single-machine execution (no distribution)

## Quick Start

### 1. Install Dependencies

```bash
cd harness
pip install -r requirements.txt
```

### 2. Set API Key

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
# Or use an OpenAI-compatible model
export HARNESS_MODEL="gpt-4"
```

### 3. Run Workflow

```bash
# One-command plan and execute
python main.py run "Build a REST API for todo items"

# Or plan first, then execute
python main.py plan "Build a REST API for todo items"
python main.py execute ./data/plans/plan_xxx.json
```

## Workflow Orchestration

Harness uses **Intelligent Multi-Agent Orchestration**: LLM dynamically generates DAG execution plans, supports parallel execution and failure adaptation.

```bash
python main.py run "Add OAuth2 support" --project ./my-project --max-parallel 5
```

Agent Types (default):
- **planner** — Architect, responsible for requirements analysis and design
- **generator** — Engineer, responsible for coding and implementation
- **evaluator** — QA, responsible for testing and review

Projects can extend custom Agents via `.harness/agents.yaml`.

## Security Model

Inspired by Anthropic's four-layer security architecture:

| Layer | Component | Function |
|-------|-----------|----------|
| Model Layer | Agent Worker | Constitutional AI, pause when uncertain |
| Tool Layer | Tool Registry | Least privilege, allow/deny lists |
| Harness Layer | Guardrails | Permission modes (plan/default/auto/dontAsk) |
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

## Relationship with Anthropic Managed Agents

This project is a **self-hosted implementation** of the Anthropic Managed Agents concept:

| Feature | Anthropic Managed Agents | This Harness |
|---------|-------------------------|-------------|
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

## License

MIT
