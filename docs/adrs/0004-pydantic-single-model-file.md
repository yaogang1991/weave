# ADR 0004: Single Model File (core/models.py)

**Status:** Accepted
**Date:** 2026-05-08
**Deciders:** Project Lead

## Context

The project has ~20+ Pydantic data models spanning DAGs, events, agents, guardrails, sessions, jobs, etc. Where should they live?

1. **Distributed**: Each module defines its own models (e.g., `dag/models.py`, `agent/models.py`)
2. **Centralized**: All models in a single file (`core/models.py`)

## Decision

We chose **centralized models** in `core/models.py`.

- All `BaseModel` subclasses are defined in one file
- Modules import from `core.models` — no circular dependency risk
- The file is the single source of truth for all data structures

## Consequences

**Positive:**
- No circular imports — models have no dependency on logic modules
- Easy to find any model — one place to look
- Consistent serialization — all models use `model_dump()`
- Simple refactoring — change a model once, see all impacts

**Negative:**
- File grows large as the project evolves (~30+ models currently)
- No logical grouping within the file (mitigated by section comments)
- Merge conflicts if multiple developers edit models simultaneously

## Alternatives Considered

- **Distributed models**: Better code organization, but creates circular import risk. Modules might depend on each other's models. Requires careful dependency management.
- **Model packages**: `core/models/` with one file per domain (dag.py, event.py, etc.). Good middle ground but adds directory complexity. May revisit if `models.py` exceeds 1000 lines.
