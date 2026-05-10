# ADR 0012: Static AST-Based Impact Analysis

**Status:** Accepted
**Date:** 2026-05-10
**Deciders:** Project Lead

## Context

Before executing a task, we want to predict which files will be affected. This enables:
- Risk assessment (how many files/modules will change?)
- Pre-execution snapshot for change verification
- Feedback to the orchestrator about task scope

Options:
1. **LLM-based prediction** — Ask the LLM which files will change
2. **Static AST analysis** — Parse imports, build dependency graph, match keywords
3. **Hybrid** — Static for initial prediction, LLM for refinement

## Decision

We chose **static AST analysis** with keyword matching as the primary prediction method.

- `DependencyGraph` uses Python's `ast` module to parse import statements
- Builds a bidirectional file dependency graph (forward: dependencies, reverse: dependents)
- `ImpactPredictor` matches requirement keywords against file names, then expands via dependency graph
- Historical predictions are stored in memory and reused if confidence ≥ 0.7
- LLM refinement is reserved for future use (the `llm_config` parameter exists but is unused)

## Consequences

**Positive:**
- Zero latency — no LLM API call, pure local computation
- Zero token cost — no additional API spending
- Deterministic — same codebase + requirement always produces the same prediction
- Language-aware for Python — understands `import`, `from X import Y`, relative imports

**Negative:**
- Python-only — only understands Python import syntax; other languages are invisible
- Keyword matching is imprecise — "fix bug in auth" may match `auth.py` but miss `security/utils.py`
- No semantic understanding — can't predict that "add rate limiting" affects middleware files
- File-level granularity only — no function/class-level impact prediction

## Alternatives Considered

- **LLM-based prediction**: More accurate for semantic matching but adds latency and cost. Reserved as an optional refinement path via the `llm_config` parameter.
- **Git blame / history analysis**: Predict impact based on which files changed in similar past commits. Interesting but requires substantial commit history and adds complexity. Could be layered on top in the future.
- **Language Server Protocol (LSP)**: Precise reference resolution for any language. Rejected — requires running an LSP server per language, too heavy for the single-user CLI use case.
