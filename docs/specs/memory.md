# Module SPEC: Agent Memory

---
**Module:** `memory/store.py`, `memory/manager.py`, `memory/sharing.py`
**Last Updated:** 2026-05-10
**Status:** IMPLEMENTED
---

## Purpose

Persistent cross-task, cross-session memory system for LLM agents. Agents can store facts, experiences, and preferences during execution, and retrieve relevant memories in future tasks. The system supports three scope levels (PRIVATE → SESSION → GLOBAL) with automatic relevance scoring and decay.

## Public Interfaces

### MemoryStore (`memory/store.py`)

```python
class MemoryStore:
    """Persistent store for MemoryEntry objects with atomic writes."""

    def store(self, entry: MemoryEntry) -> MemoryEntry
    def get(self, memory_id: str) -> MemoryEntry | None
    def update(self, entry: MemoryEntry) -> MemoryEntry
    def delete(self, memory_id: str) -> bool

    def list_entries(
        scope: MemoryScope | None = None,
        agent_type: str | None = None,
        session_id: str | None = None,
        memory_type: MemoryType | None = None,
    ) -> list[MemoryEntry]

    def search(
        query: str,
        scope: MemoryScope | None = None,
        limit: int = 10,
    ) -> list[MemoryEntry]

    def get_relevant(
        agent_type: str,
        session_id: str | None = None,
        context: str = "",
        limit: int = 10,
    ) -> list[MemoryEntry]

    def cleanup_expired(self) -> int
    def enforce_limits(self, max_per_agent: int) -> int
    def recompute_relevance(self, half_life_days: float) -> None
```

### MemoryManager (`memory/manager.py`)

```python
class MemoryManager:
    """High-level memory operations: store, retrieve, inject, extract."""

    def store_learning(
        agent_type: str,
        content: str,
        memory_type: MemoryType = MemoryType.FACT,
        scope: MemoryScope = MemoryScope.PRIVATE,
        session_id: str | None = None,
        source_node_id: str | None = None,
        keywords: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEntry

    def store_task_outcome(
        agent_type: str,
        task_description: str,
        result_summary: str,
        success: bool,
        session_id: str,
        node_id: str,
    ) -> MemoryEntry

    def get_context_for_agent(
        agent_type: str,
        task_description: str,
        session_id: str | None = None,
    ) -> list[MemoryEntry]

    def format_memory_prompt(self, entries: list[MemoryEntry]) -> str
    def extract_and_store(
        agent_type: str,
        task_description: str,
        execution_result: dict[str, Any],
        session_id: str,
        node_id: str,
    ) -> list[MemoryEntry]

    def run_maintenance(self) -> dict[str, int]
    def get_stats(self) -> dict[str, Any]
```

### MemorySharing (`memory/sharing.py`)

```python
class MemorySharing:
    """Cross-agent memory sharing within and across sessions."""

    def share_with_downstream(
        from_agent: str,
        to_agent: str,
        session_id: str,
        dag: Any,
        node_id: str,
    ) -> list[MemoryEntry]

    def promote_to_session(
        memory_id: str,
        session_id: str,
        target_agent: str | None = None,
    ) -> MemoryEntry | None

    def promote_to_global(self, memory_id: str) -> MemoryEntry | None
    def get_shared_for_agent(
        agent_type: str,
        session_id: str,
    ) -> list[MemoryEntry]
```

## Data Models (`core/models.py`)

```python
class MemoryScope(str, Enum):
    PRIVATE  = "private"   # Per-agent
    SESSION  = "session"   # Shared within session
    GLOBAL   = "global"    # Cross-session persistent

class MemoryType(str, Enum):
    FACT        = "fact"
    EXPERIENCE  = "experience"
    PREFERENCE  = "preference"
    CONTEXT     = "context"

class MemoryEntry(BaseModel):
    id: str
    agent_type: str
    scope: MemoryScope
    memory_type: MemoryType
    content: str
    keywords: list[str]
    session_id: str | None
    source_node_id: str | None
    relevance_score: float
    access_count: int
    created_at: datetime
    last_accessed_at: datetime | None
    expires_at: datetime
    metadata: dict[str, Any]
```

## Data Flow

```
Agent 执行前:
  MemoryManager.get_context_for_agent() → MemoryStore.get_relevant()
    → 关键词重评分 → 返回 top-N 条目 → format_memory_prompt() → 注入 system prompt

Agent 执行后:
  MemoryManager.extract_and_store() → store_task_outcome() (EXPERIENCE)
    + extract facts from result metadata → store_learning() (FACT)

DAG 节点间:
  DAG Engine → MemorySharing.share_with_downstream()
    → PRIVATE → promote_to_session() → SESSION scope

定期维护:
  run_maintenance() → cleanup_expired + enforce_limits + recompute_relevance
```

## Directory Layout

```
./data/memory/
├── global/              # GLOBAL scope entries
│   └── {memory_id}.json
├── agents/              # PRIVATE scope entries
│   ├── planner/
│   │   └── {memory_id}.json
│   └── generator/
│       └── {memory_id}.json
└── sessions/            # SESSION scope entries
    └── {session_id}/
        └── {memory_id}.json
```

## Relevance Scoring

```
score = keyword_overlap × recency × frequency_bonus

keyword_overlap  = |query ∩ (keywords ∪ content_tokens)| / |query|
recency          = 0.5 ^ (days_since_created / half_life_days)
frequency_bonus  = 1.0 + min(access_count, 10) × 0.1
```

## Error Handling

| Error | Condition | Handling |
|-------|-----------|----------|
| `ValueError` | Content exceeds `max_content_length` | Raised from `store_learning` |
| `ValueError` | Invalid path component (path traversal) | Raised from `_sanitize_path_component` |
| Silent failure | JSON parse error on load | Returns `None`, logged at WARNING |

## Dependencies

### Imports From
- `core/models.py` — MemoryEntry, MemoryScope, MemoryType, EventType
- `core/config.py` — MemoryConfig

### Imported By
- `agent/agent_pool.py` — Memory injection and extraction hooks
- `core/dag_engine.py` — Cross-agent memory sharing
- `control_plane/service.py` — MemoryManager initialization and maintenance
- `learning/optimizer.py` — Insight → memory conversion

## Configuration

| Env Var | Config Key | Default | Description |
|---------|-----------|---------|-------------|
| `HARNESS_MEMORY_ENABLED` | `memory.enabled` | `true` | Enable memory system |
| `HARNESS_MEMORY_PATH` | `memory.base_path` | `./data/memory` | Storage directory |
| `HARNESS_MEMORY_MAX_ENTRIES` | `memory.max_entries_per_agent` | `500` | Max entries per agent |
| `HARNESS_MEMORY_MAX_LENGTH` | `memory.max_content_length` | `1000` | Max chars per entry |
| `HARNESS_MEMORY_TTL_DAYS` | `memory.default_ttl_days` | `90` | Default expiry days |
| `HARNESS_MEMORY_RETRIEVAL_LIMIT` | `memory.retrieval_limit` | `10` | Max memories per prompt |
| `HARNESS_MEMORY_DECAY_DAYS` | `memory.decay_half_life_days` | `30.0` | Relevance half-life |

## Invariants

- All writes are atomic (temp file + `os.replace`)
- Memory IDs are globally unique (uuid4 hex[:8])
- Path traversal is prevented via `_sanitize_path_component`
- `retrieval_limit` ≤ `max_entries_per_agent`
