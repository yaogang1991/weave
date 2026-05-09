# ADR 0001: LLM-Driven DAG Orchestration

**Status:** Accepted
**Date:** 2026-05-08
**Deciders:** Project Lead

## Context

The orchestrator needs to generate execution plans (DAGs) from user requirements. Two approaches:

1. **Hardcoded state machine**: Predefined workflow templates (e.g., plan→implement→test) with fixed agent assignments
2. **LLM-driven orchestration**: An LLM agent analyzes requirements, queries the agent registry, and dynamically generates a custom DAG

## Decision

We chose **LLM-driven orchestration**. The `IntelligentOrchestrator` uses an LLM to:
- Analyze the user's requirement
- Query `AgentRegistry` for available capabilities
- Generate a `DAG` with appropriate agent assignments and dependencies
- Adapt to failures by re-planning

## Consequences

**Positive:**
- Handles novel requirements without template updates
- Can reason about task decomposition strategies
- Self-adapts when agents fail (replan instead of just retry)
- Extensible — new agent types are automatically discovered

**Negative:**
- LLM cost for every planning call
- Non-deterministic — same requirement may produce different DAGs
- Latency — planning requires an LLM API round-trip
- Risk of invalid DAGs (mitigated by validation against registry)

## Alternatives Considered

- **Template-only**: Faster, deterministic, but rigid. Cannot handle requirements outside template scope.
- **Hybrid (template + LLM fallback)**: Considered for M3 (see DAG Template Library). Templates for common tasks, LLM for novel ones.
