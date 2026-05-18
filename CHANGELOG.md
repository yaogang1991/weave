# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - 2026-05-17

### Added

- **M3.6: Skills System** -- YAML-based prompt templates for single-agent invocations with variable substitution
- **M3.5: Impact Analysis** -- Pre-execution impact prediction, post-execution change verification, and dependency graph (Python AST import resolution)
- **M3.4: DAG Templates** -- 7 built-in reusable YAML templates (build_api, fix_bug, add_feature, refactor, add_tests, add_auth, setup_project) to skip LLM planning
- **M3.3: Self-Learning** -- Automatic execution pattern analysis, insight-to-memory conversion, and planning hint injection
- **M3.2: Agent Memory** -- Persistent cross-session memory with PRIVATE/SESSION/GLOBAL scope promotion and automatic extraction
- **M3.1: Multi-Model Routing** -- Per-agent-type model routing with fallback chains
- **M3.0: Architecture Refactoring** -- Domain model splitting into `core/*_models.py`, CLI module extraction into `cli/`, control plane decomposition into focused modules
- **M2: Execution Backend Abstraction** -- Local and git worktree isolation backends, sandbox providers, BackendManager with risk-driven selection and auto-fallback
- **M2.3: Web Console** -- FastAPI dashboard with real-time DAG monitoring, WebSocket event bridge, and management console
- **M2.1: Watchdog** -- Background coroutine heartbeat monitoring with configurable thresholds
- **M1.1: Approval Workflow** -- Ticket-based human approval system for high-risk operations
- **M1: Worker Mode** -- File-based job queue, worker consumer with lease mechanism, non-interactive mode, orphaned job recovery
- **MCP Client** -- Model Context Protocol integration via stdio transport
- **Execution Hooks** -- Lifecycle callback system (MemoryHook, LearningHook, ImpactHook) decoupling subsystems from core execution
- **Security Phase 1** -- Credential isolation, immutable state patterns

### Changed

- Refactored monolithic modules into focused single-responsibility files
- Extracted node executor, quality gate, and retry policy from DAG engine
- Extracted worker executor and worker recovery from worker module
- Split control plane into job lifecycle, run lifecycle, backend lifecycle, and job result modules
- Unified LLM client supporting both Anthropic and OpenAI providers

## [1.0.0] - 2025-01-01

### Added

- Core DAG orchestration with planner, generator, and evaluator agents
- Agent capability registry with project-specific extensions via `.weave/agents.yaml`
- Tool registry (read, write, edit, bash, glob, grep, git)
- Guardrails system with risk classification and permission modes
- Event-sourced session management (append-only JSONL)
- Intelligent orchestrator with LLM-driven planning and failure adaptation
- Automated evaluation engine with criterion checkers
- CLI interface with plan/execute/run commands
- Reporter and audit logging

[2.0.0]: https://github.com/yaogang1991/weave/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/yaogang1991/weave/releases/tag/v1.0.0
