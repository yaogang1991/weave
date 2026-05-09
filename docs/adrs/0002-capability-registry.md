# ADR 0002: Agent Capability Registry

**Status:** Accepted
**Date:** 2026-05-08
**Deciders:** Project Lead

## Context

The orchestrator needs to know which agent types are available and what they can do. Two approaches:

1. **Hardcoded agent types**: Fixed planner/generator/evaluator roles in code
2. **Capability registry**: Dynamic registration where agents describe their skills, and the orchestrator discovers them at runtime

## Decision

We chose a **capability registry** (`AgentRegistry` in `core/agent_registry.py`).

- Default agents (planner, generator, evaluator) are registered at startup via `_register_defaults()`
- Projects extend via `.harness/agents.yaml`
- The registry exposes `to_prompt_description()` for the orchestrator to include in its planning prompt
- Default agents cannot be unregistered (protected)

## Consequences

**Positive:**
- Project-specific agents (e.g., `ui_designer`, `db_admin`) are first-class citizens
- No code changes needed to add new agent types — YAML config suffices
- Orchestrator prompt is dynamically generated from actual registry state

**Negative:**
- YAML misconfiguration can break planning (mitigated by validation)
- More complex than hardcoded types

## Alternatives Considered

- **Hardcoded**: Simpler but requires code changes for every new agent type. Cannot support project-specific customization.
- **Plugin marketplace**: External discovery and download of agent definitions. Deferred to M3+ (too much infrastructure for single-user M1).
