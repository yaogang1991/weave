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
|  +-------------+  +-------------+  +---------------------+      |
|  |   Memory    |  |  Learning   |  |  Impact Analysis    |      |
|  |  (M3.2)     |  |  (M3.3)     |  |  (M3.5)             |      |
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
| `worker [--concurrency N] [--non-interactive]` | Start Worker |
| `recover` | Manually recover orphaned tasks |
| `tickets [--status STATUS] [--job JOB_ID]` | List approval tickets |
| `approve <ticket_id> [--reason ...]` | Approve ticket |
| `reject <ticket_id> [--reason ...]` | Reject ticket |
| `plan "<requirement>"` | Generate execution plan (no execution) |
| `run "<requirement>"` | One-command plan + execute |
| `run "..." --template build_api --var feature=Todo` | Run from template (M3.4) |
| `templates [--name NAME]` | List/show DAG templates (M3.4) |
| `memory-search "query"` | Search agent memory (M3.2) |
| `memory-list [--agent TYPE]` | List memory entries (M3.2) |
| `memory-stats` | Memory system statistics (M3.2) |
| `memory-add "content" --type fact --scope global` | Add memory manually (M3.2) |
| `memory-cleanup` | Run memory maintenance (M3.2) |
| `learning-analyze` | Trigger learning analysis (M3.3) |
| `learning-insights [--limit N]` | List learning insights (M3.3) |
| `learning-status` | Learning system status (M3.3) |
| `impact-predict "requirement" --project .` | Predict impact (M3.5) |
| `impact-graph --project .` | Show dependency graph (M3.5) |
| `impact-history [--limit N]` | Past predictions (M3.5) |

### Approval Workflow (High-Risk Operations)

When task execution reaches high-risk operations (such as `bash` commands), the system creates approval tickets:

```bash
# View pending tickets
python main.py tickets
# -> {"tickets": [{"id": "ticket_abc123", "tool_name": "bash", "status": "pending", ...}], "count": 1}

# Approve ticket (task continues execution)
python main.py approve ticket_abc123 --reason "Safe command, reviewed"

# Reject ticket (task enters failure/retry)
python main.py reject ticket_abc123 --reason "Too risky"
```

### Unattended Mode

Fully automatic execution without human intervention:

```bash
# Method 1: Environment variable
export HARNESS_NON_INTERACTIVE=true
python main.py worker --concurrency 1

# Method 2: Command-line parameter
python main.py worker --concurrency 1 --non-interactive
```

In unattended mode:
- Low risk (read/glob) auto-passes
- Medium risk (write/edit) auto-passes
- High risk (bash) auto-creates pending ticket, does not block process
- Tickets auto-expire after timeout, tasks are handled by failure policy (retry or dead-letter)

### Common Combinations

```bash
# Terminal 1: Start worker (unattended)
python main.py worker --non-interactive

# Terminal 2: Submit task
python main.py submit "Build a REST API for user auth"

# Terminal 3: Monitor status
python main.py list --status queued
python main.py list --status running
python main.py tickets --status pending
```

### Personal Mode Features

- **Unattended**: Throw tasks in, worker auto-runs to completion
- **Failure Recovery**: timeout/retry/dead-letter mechanism
- **High-Risk Confirmation**: HIGH risk operations require confirmation (or whitelist auto-pass)
- **Metrics & Alerts**: Auto-collect success rate, duration, and other metrics
- **Restart Recovery**: Process interruption can recover to processable state

### M3 Features

#### Multi-Model Routing (M3.1)

Different agent types can use different LLM models:

```bash
export HARNESS_PLANNER_MODEL="claude-opus-4-6"
export HARNESS_GENERATOR_MODEL="gpt-4"
```

#### Agent Memory (M3.2)

Agents remember facts, experiences, and preferences across tasks:

```bash
# Search memories
python main.py memory-search "database schema"

# Add a project convention
python main.py memory-add "Always use SQLAlchemy async session" --type fact --scope global

# View stats
python main.py memory-stats
```

Memories are automatically injected into agent prompts during execution.

#### Self-Learning (M3.3)

The system learns from execution history to improve planning:

```bash
# Trigger analysis
python main.py learning-analyze

# View learned insights
python main.py learning-insights
```

Insights are automatically injected into orchestrator planning prompts.

#### DAG Templates (M3.4)

Skip LLM planning for common task patterns:

```bash
# List templates
python main.py templates

# Run with template
python main.py run "Build Todo API" --template build_api --var feature=Todo --var language=Python
```

#### Impact Analysis (M3.5)

Predict which files a task will affect, verify changes after execution:

```bash
# Predict impact
python main.py impact-predict "Refactor the DAG engine" --project .

# View dependency graph
python main.py impact-graph --project .
```

### Known Limitations

- Single-user scenario (no multi-tenancy)
- No persistent database (uses JSON files)
- Single-machine execution (no distribution)
- Impact analysis only supports Python import resolution

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
| `memory/` | M3.2: Agent memory (store, retrieve, share) |
| `learning/` | M3.3: Execution pattern analysis and optimization |
| `templates/` | M3.4: Reusable DAG templates (YAML + variables) |
| `analysis/` | M3.5: Dependency graph, impact prediction, change verification |
| `visualizer/` | M2.3: Web console (FastAPI + WebSocket) |
| `backend/` | M2: Execution backend abstraction (local/worktree) |
| `control_plane/` | Job queue, worker, approval tickets |

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
