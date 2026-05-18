# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.7] - 2026-05-18

### Added

- WASM runtime proof-of-concept backend (#507)
- A2A (Agent-to-Agent) protocol: Agent Card models and discovery endpoint (#506)
- SWE-bench runner framework for benchmarking (#513)
- MCP Server framework with stdio transport (#512)
- Prompt injection detection as input-layer defense (#511)
- Output injection monitoring for tool results (#511)
- DAG node isolation guard (#511)
- Embedding provider abstraction for semantic memory retrieval (#508)
- Semantic retrieval integration into MemoryManager (#508)
- Learning analysis MCP tools (#512)
- weave.memory_store MCP tool (#512)
- OpenTelemetry GenAI Semantic Conventions integration (#509)
- Prompt Prefix Caching for Anthropic API (#503)
- Proactive context compaction (#480)
- Structured output for DAG generation (#505)
- Worker integration with memory module for cross-session learning (#481)
- DAG execution state persistence for crash recovery (#455)
- Periodic auto-snapshot for event sourcing sessions (#510)
- Rollback restore for failed snapshots (#487)
- Resource limits for DockerSandbox (#483) and LocalSandbox (#482)
- BackendManager risk-based sandbox selection (#484)

### Changed

- Extracted checkpoint and compat proxies from dag_engine (#516)
- Extracted schemas, validators, and AST utils from tools/registry (#515)
- Extracted shared CLI args to cli/args.py (#497)
- Extracted MCP tools from cli/execution.py (#566)
- Replaced Any type annotations with concrete types (#498)
- Replaced node field mutations with model_copy (#486)
- Renamed harness -> weave across entire codebase (#520)
- Consolidated error classification into control_plane/errors.py (#501)
- Switched license from MIT to Apache-2.0

### Fixed

- Handle truncated LLM planning responses (#561)
- Increase evaluator node timeout to 480s (#568)
- CJK-aware token estimation in worker (#479)
- Skip LLM retries when tool calls have empty args (#541)
- Propagate evaluator lint feedback to upstream generator on retry (#523)
- Auto-approve file writes within --project directory (#524)
- Harden bash command deny list against shell injection (#493)
- Prevent path traversal when base_cwd is not configured (#500)
- Bind visualizer to 127.0.0.1 + add API key auth (#494)
- Validate webhook URLs against SSRF attacks (#495)
- Credential isolation & immutable state (#456)
- Resolve 13 pre-existing test failures in hemostasis tests (#549)

## [0.3.6] - 2026-05-08

### Added

- **Skills System** -- YAML-based prompt templates for single-agent invocations with variable substitution

## [0.3.5] - 2026-05-06

### Added

- **M3.5: Impact Analysis** -- Pre-execution impact prediction, post-execution change verification, dependency graph via Python AST import resolution
- **M3.4: DAG Templates** -- 7 built-in YAML templates (build_api, fix_bug, add_feature, refactor, add_tests, add_auth, setup_project) to skip LLM planning

### Changed

- Execution hooks (MemoryHook, LearningHook, ImpactHook) decoupling subsystems from core flow

## [0.3.3] - 2026-05-02

### Added

- **M3.3: Self-Learning** -- Execution pattern analysis, insight-to-memory conversion, planning hint injection
- **M3.2: Agent Memory** -- Persistent cross-session memory with PRIVATE/SESSION/GLOBAL scope promotion and automatic extraction
- **M3.1: Multi-Model Routing** -- Per-agent-type model routing with fallback chains
- **M3.0: Knowledge System** -- Module SPECs, ADRs, config reference, developer guide, knowledge index

### Changed

- Domain model splitting into `core/*_models.py` with unified re-export
- CLI module extraction into `cli/` subdirectory
- Control plane decomposition into job lifecycle, run lifecycle, backend lifecycle
- Extracted NodeExecutor, QualityGate, RetryPolicy from DAG engine
- Extracted WorkerExecutor, WorkerRecovery from worker module
- Extracted ExecutionFactory from RunService

## [0.2.3] - 2026-04-20

### Added

- **M2.3: Web Console** -- FastAPI dashboard with real-time DAG monitoring, WebSocket event bridge, management console
- **M2.1/M2.2: Execution Backend** -- Worktree isolation backend, execution backend abstraction, local backend
- **M2.0: Watchdog** -- Node heartbeat protocol with configurable thresholds, fail-fast on timeout
- Metrics aggregation and alert system

### Changed

- Integrated evaluator into DAG flow
- Enhanced agent worker with improved tool integration
- Consolidated v1/v2 codebase and added visualizer module

## [0.1.1] - 2026-04-10

### Added

- **M1.1: Approval Workflow** -- Ticket-based human approval for high-risk operations, unified guardrails with tri-state entry, non-interactive mode
- Guardrails truth-labeling and config health checks

### Fixed

- Approval lifecycle wiring and recovery transitions
- Approval reject/resume control-plane state transitions
- Rejection races for running and leased jobs

## [0.1.0] - 2026-04-05

### Added

- **M1: Worker Mode** -- File-based job queue, worker consumer with lease mechanism, timeout/retry/dead-letter handling, true replan closed-loop
- Personal guardrails with risk classification and permission modes
- Control plane with persistent repository and CLI commands (submit, status, list, cancel, worker, recover)
- Integration tests and documentation

## [0.0.1] - 2026-04-01

### Added

- Core DAG orchestration with planner, generator, and evaluator agents
- Agent capability registry with project-specific extensions via `.weave/agents.yaml`
- Tool registry (read, write, edit, bash, glob, grep, git)
- Event-sourced session management (append-only JSONL)
- Intelligent orchestrator with LLM-driven planning and failure adaptation
- Automated evaluation engine with criterion checkers
- CLI interface with plan/execute/run commands
- Reporter and audit logging

[0.3.7]: https://github.com/yaogang1991/weave/compare/v0.3.6...v0.3.7
[0.3.6]: https://github.com/yaogang1991/weave/compare/v0.3.5...v0.3.6
[0.3.5]: https://github.com/yaogang1991/weave/compare/v0.3.3...v0.3.5
[0.3.3]: https://github.com/yaogang1991/weave/compare/v0.2.3...v0.3.3
[0.2.3]: https://github.com/yaogang1991/weave/compare/v0.1.1...v0.2.3
[0.1.1]: https://github.com/yaogang1991/weave/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/yaogang1991/weave/compare/v0.0.1...v0.1.0
[0.0.1]: https://github.com/yaogang1991/weave/releases/tag/v0.0.1
