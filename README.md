# Weave

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-brightgreen.svg)](https://www.python.org/)
[![Version](https://img.shields.io/badge/Version-0.3.7-orange.svg)](pyproject.toml)

**Intelligent Multi-Agent Orchestration for Autonomous Software Development**

Based on [Anthropic Managed Agents](https://www.anthropic.com/engineering/managed-agents) architecture. Weave orchestrates multiple LLM agents (planner, generator, evaluator) to automate the full software development lifecycle via LLM-driven dynamic DAG generation and execution.

> DAG is the loom, Agent is the shuttle. Multiple agents collaborate by role, weaving requirements into complete software.

[中文文档](README_zh.md) | [Architecture](ARCHITECTURE.md) | [Contributing](CONTRIBUTING.md) | [Changelog](CHANGELOG.md) | [Roadmap](docs/roadmap.md)

---

## Why Weave?

| Problem | Weave's Answer |
|---------|---------------|
| Single-agent tools can't handle complex tasks | Multi-agent DAG orchestration with parallel execution |
| Hard-coded workflows break on edge cases | LLM-driven planner adapts in real-time |
| Cloud-only solutions lock you in | Fully self-hosted, token-cost only |
| No quality guarantee on generated code | Contract-driven evaluation with automated checks |

## Core Features

- **LLM-Driven DAG Orchestration** -- Planner agent generates execution DAGs dynamically, adapting to failures in real-time
- **Multi-Model Routing** -- Assign different LLM models per agent role (e.g., Opus for planning, Sonnet for coding)
- **Agent Memory** -- Persistent cross-session memory with scope promotion (PRIVATE → SESSION → GLOBAL)
- **Self-Learning** -- Automatic pattern analysis from execution history, feeding optimization hints back to the planner
- **Impact Analysis** -- Pre-execution impact prediction and post-execution change verification
- **DAG Templates** -- Reusable YAML templates to skip LLM planning for recurring task patterns
- **Skills System** -- YAML-based prompt templates for single-agent invocations with variable substitution
- **MCP Integration** -- Model Context Protocol client for tool discovery and execution via stdio transport
- **Web Console** -- Real-time DAG monitoring, job management, and alert dashboard
- **Approval Workflow** -- Human-in-the-loop gate for high-risk operations
- **Multiple Backends** -- Local or git worktree isolation, with Docker sandbox support

## Quick Start

### Prerequisites

- Python 3.11+
- An Anthropic API key (or OpenAI-compatible endpoint)

### Install

```bash
git clone https://github.com/yaogang1991/weave.git
cd weave
pip install -r requirements.txt
```

### Run

```bash
# Set your API key
export ANTHROPIC_API_KEY="sk-ant-..."

# One-command plan + execute
python main.py run "Build a REST API for todo items"
```

Or plan first, then execute:

```bash
python main.py plan "Build a REST API for user authentication"
python main.py execute ./data/plans/plan_xxx.json
```

## Usage

### Interactive Mode

```bash
# Plan and execute in one step
python main.py run "Add OAuth2 support to the API"

# With project-specific agents
python main.py run "Design login page" --project ./my-project --max-parallel 5

# Using a DAG template (skip LLM planning)
python main.py run "Build Todo API" --template build_api --var feature=Todo --var language=Python
```

### Worker Mode (Unattended)

```bash
# Terminal 1: Start worker
python main.py worker --concurrency 1

# Terminal 2: Submit task
python main.py submit "Build a REST API for user auth"

# Terminal 3: Monitor
python main.py list --status running
python main.py tickets --status pending

# Non-interactive mode
export WEAVE_NON_INTERACTIVE=true
python main.py worker --non-interactive
```

### MCP Server Mode

```bash
python main.py serve
```

### Web Console

```bash
python main.py viz
# Open http://localhost:8765 for the dashboard
```

### Command Reference

| Command | Description |
|---------|-------------|
| `run "<req>"` | Plan + execute in one step |
| `plan "<req>"` | Generate execution plan (DAG) |
| `execute <plan>` | Execute a saved plan |
| `submit "<req>"` | Submit task to worker queue |
| `worker` | Start worker (queue consumer) |
| `status <id>` | View job status |
| `list` | List jobs |
| `cancel <id>` | Cancel a running job |
| `recover` | Recover orphaned jobs |
| `tickets` | List approval tickets |
| `approve <id>` | Approve a ticket |
| `reject <id>` | Reject a ticket |
| `templates` | List DAG templates |
| `skills` | List available skills |
| `skill <name>` | Invoke a skill |
| `serve` | Start MCP server |
| `viz` | Start web console |
| `memory-search` | Search agent memory |
| `memory-add` | Add memory entry |
| `memory-stats` | Memory statistics |
| `learning-analyze` | Trigger pattern analysis |
| `learning-insights` | View learning insights |
| `impact-predict` | Predict impact of a change |
| `impact-graph` | Show dependency graph |
| `console` | Interactive management console |

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                     Orchestrator Layer                        │
│   Planner  ·  Generator  ·  Evaluator                        │
├──────────────────────────────────────────────────────────────┤
│              Session Manager (Append-Only Event Log)          │
├──────────────────────────────────────────────────────────────┤
│                     Weave Core (Dumb Loop)                    │
│   Agent Worker  ←  Tool Registry  ←  Guardrails              │
├──────────────────────────────────────────────────────────────┤
│   Sandbox  ·  Git  ·  Reporter                                │
├──────────────────────────────────────────────────────────────┤
│   Memory  ·  Learning  ·  Impact Analysis                     │
└──────────────────────────────────────────────────────────────┘
```

**Four-layer architecture:** Orchestrator → Session Manager → Weave Core → Execution Layer

For the full architecture document, see [ARCHITECTURE.md](ARCHITECTURE.md).

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | -- | Anthropic API key (required) |
| `OPENAI_API_KEY` | -- | OpenAI API key (alternative) |
| `WEAVE_MODEL` | `claude-sonnet-4-6` | Default LLM model |
| `WEAVE_DEFAULT_BACKEND` | `local` | Execution backend (`local`/`worktree`) |
| `WEAVE_NON_INTERACTIVE` | `false` | Disable interactive prompts |
| `WEAVE_PLANNER_MODEL` | -- | Override model for planner agent |
| `WEAVE_GENERATOR_MODEL` | -- | Override model for generator agent |

### Project Configuration

Create `.weave/config.yaml` in your project:

```yaml
guardrails:
  permission_mode: default
  max_file_size: 100000

memory:
  enabled: true
  max_entries: 500

backend:
  type: local
```

See [docs/config_reference.md](docs/config_reference.md) for the full configuration reference.

## Custom Agents

Register project-specific agents in `.weave/agents.yaml`:

```yaml
agents:
  - id: ui_designer
    name: UI Designer
    skills: [ui_design, react_component_dev, tailwind_css]
    constraints: [Only modifies frontend/src/]
```

The orchestrator discovers these automatically and assigns them during planning.

## Module Overview

| Module | Responsibility |
|--------|---------------|
| `core/` | Domain models, configuration, DAG engine, LLM client/router, watchdog |
| `cli/` | CLI command handlers |
| `session/` | Event storage, state recovery, checkpoint |
| `agent/` | LLM API calls, agent pool, system prompts |
| `tools/` | Built-in tools + command runner + MCP integration |
| `guardrails/` | Risk classification, permission control |
| `evaluator/` | Automated evaluation (checkers, lint, runner) |
| `orchestrator/` | Workflow orchestration, plan validation, prompts |
| `memory/` | Agent memory (store, retrieve, share) |
| `learning/` | Execution pattern analysis and optimization |
| `templates/` | Reusable DAG templates (YAML + variables) |
| `analysis/` | Dependency graph, impact prediction, change verification |
| `visualizer/` | Web console (FastAPI + WebSocket) |
| `backend/` | Execution backend (local/worktree) + sandbox providers |
| `control_plane/` | Job queue, worker, execution hooks, approval tickets |
| `mcp/` | Model Context Protocol client (stdio transport) |
| `skills/` | YAML skill definitions with variable substitution |

## Comparison with Anthropic Managed Agents

| Feature | Anthropic Managed Agents | Weave |
|---------|-------------------------|-------|
| Running Location | Anthropic Cloud | Local / Self-hosted |
| Pricing | $0.08/session-hour + tokens | Token cost only |
| Session | Managed event log | Local JSONL |
| Sandbox | Managed container | Docker / Local |
| LLM | Claude only | Claude / OpenAI-compatible |
| MCP | Native support | Client integration |
| Custom Agents | Limited | Full YAML-based registration |

## Known Limitations

- Single-user scenario (no multi-tenancy)
- File-based storage (no external database required)
- Single-machine execution (no distributed mode)
- Impact analysis supports Python import resolution only

## Documentation

- [Architecture](ARCHITECTURE.md) -- Full system architecture and component details
- [Contributing](CONTRIBUTING.md) -- Development setup and PR process
- [Changelog](CHANGELOG.md) -- Release history
- [Roadmap](docs/roadmap.md) -- Milestone history and future plans
- [Config Reference](docs/config_reference.md) -- All configuration options
- [Developer Guide](docs/dev_guide.md) -- Extending agents, tools, and backends
- [Specs](docs/specs/) -- Per-module engineering specifications
- [ADRs](docs/adrs/) -- Architecture Decision Records

## License

[Apache License 2.0](LICENSE)

Copyright 2026 yaogang1991
