"""
MemoryManager -- High-level memory operations.

Primary interface for agent_pool, dag_engine, CLI, and web API.
Handles keyword extraction, relevance scoring, prompt formatting,
and automatic learning extraction from execution results.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from core.models import MemoryEntry, MemoryScope, MemoryType, EventType
from memory.store import MemoryStore

logger = logging.getLogger(__name__)

# Simple stop words for keyword extraction
_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "must", "shall", "can", "need", "dare",
    "it", "its", "this", "that", "these", "those", "i", "me", "my",
    "we", "our", "you", "your", "he", "him", "his", "she", "her",
    "they", "them", "their", "what", "which", "who", "whom", "where",
    "when", "how", "not", "no", "nor", "and", "but", "or", "for",
    "so", "yet", "both", "either", "neither", "each", "every", "all",
    "any", "few", "more", "most", "other", "some", "such", "than",
    "too", "very", "just", "about", "above", "after", "again", "also",
    "at", "by", "from", "in", "into", "of", "on", "to", "with", "up",
    "out", "off", "over", "under", "then", "once", "here", "there",
    "if", "as", "until", "while", "because", "since", "through",
    "during", "before", "between", "among", "against", "below",
})


def _extract_keywords(text: str, max_keywords: int = 5) -> list[str]:
    """Extract keywords from text using simple frequency heuristics."""
    tokens = re.findall(r"\w+", text.lower())
    freq: dict[str, int] = {}
    for t in tokens:
        if len(t) < 3 or t in _STOP_WORDS:
            continue
        freq[t] = freq.get(t, 0) + 1
    # Sort by frequency descending, take top N
    sorted_words = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    return [w for w, _ in sorted_words[:max_keywords]]


def _compute_relevance(
    entry: MemoryEntry,
    query_tokens: set[str],
    now: datetime,
    half_life_days: float,
) -> float:
    """Compute relevance score: keyword_overlap * recency * frequency."""
    if not query_tokens:
        # Default recency-based score
        days_since = (now - entry.created_at).total_seconds() / 86400
        recency = 0.5 ** (days_since / max(half_life_days, 0.001))
        frequency_bonus = 1.0 + min(entry.access_count, 10) * 0.1
        return recency * frequency_bonus

    entry_tokens = set(k.lower() for k in entry.keywords)
    content_tokens = set(re.findall(r"\w+", entry.content.lower()))
    all_tokens = entry_tokens | content_tokens

    overlap = len(query_tokens & all_tokens)
    keyword_score = overlap / max(len(query_tokens), 1)

    days_since = (now - entry.created_at).total_seconds() / 86400
    recency = 0.5 ** (days_since / max(half_life_days, 0.001))

    frequency_bonus = 1.0 + min(entry.access_count, 10) * 0.1

    return keyword_score * recency * frequency_bonus


class MemoryManager:
    """
    High-level memory operations: store, retrieve, inject, extract.

    This is the primary interface used by agent_pool, dag_engine, and CLI.
    """

    def __init__(
        self,
        config: Any,  # MemoryConfig from core.config
        session_store: Any = None,
        session_id: str | None = None,
    ) -> None:
        self.config = config
        self.store = MemoryStore(config.base_path)
        self.session_store = session_store
        self._session_id = session_id

    def _emit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Emit an event to the session store if available."""
        if self.session_store and hasattr(self.session_store, "emit_event") and self._session_id:
            try:
                self.session_store.emit_event(self._session_id, event_type, payload)
            except Exception as e:
                logger.debug("Failed to emit memory event: %s", e)

    # -- Storing memories --

    def store_learning(
        self,
        agent_type: str,
        content: str,
        memory_type: MemoryType = MemoryType.FACT,
        scope: MemoryScope = MemoryScope.PRIVATE,
        session_id: str | None = None,
        source_node_id: str | None = None,
        keywords: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEntry:
        """Store a learning/memory entry."""
        if len(content) > self.config.max_content_length:
            raise ValueError(
                f"Content length ({len(content)}) exceeds max "
                f"({self.config.max_content_length})"
            )

        if keywords is None:
            keywords = _extract_keywords(content)

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=self.config.default_ttl_days)

        entry = MemoryEntry(
            agent_type=agent_type,
            scope=scope,
            memory_type=memory_type,
            content=content,
            keywords=keywords,
            session_id=session_id,
            source_node_id=source_node_id,
            expires_at=expires_at,
            metadata=metadata or {},
        )

        self.store.store(entry)
        self._emit_event(EventType.MEMORY_STORED, {
            "memory_id": entry.id,
            "agent_type": agent_type,
            "scope": scope.value,
            "memory_type": memory_type.value,
            "content_preview": content[:100],
        })
        return entry

    def store_task_outcome(
        self,
        agent_type: str,
        task_description: str,
        result_summary: str,
        success: bool,
        session_id: str,
        node_id: str,
    ) -> MemoryEntry:
        """Store an EXPERIENCE entry from a task execution."""
        outcome = "succeeded" if success else "failed"
        content = f"Task '{task_description[:200]}' {outcome}. {result_summary[:500]}"

        # Trim to max content length
        if len(content) > self.config.max_content_length:
            content = content[: self.config.max_content_length - 3] + "..."

        keywords = _extract_keywords(task_description)

        return self.store_learning(
            agent_type=agent_type,
            content=content,
            memory_type=MemoryType.EXPERIENCE,
            scope=MemoryScope.SESSION,
            session_id=session_id,
            source_node_id=node_id,
            keywords=keywords,
            metadata={"success": success},
        )

    def store_preference(
        self,
        content: str,
        scope: MemoryScope = MemoryScope.GLOBAL,
    ) -> MemoryEntry:
        """Store a user preference (typically global scope)."""
        return self.store_learning(
            agent_type="shared",
            content=content,
            memory_type=MemoryType.PREFERENCE,
            scope=scope,
        )

    # -- Retrieving memories --

    def list_entries(
        self,
        scope: MemoryScope | None = None,
        agent_type: str | None = None,
        session_id: str | None = None,
        memory_type: MemoryType | None = None,
    ) -> list[MemoryEntry]:
        """List memory entries via the store. Wrapper for external consumers."""
        return self.store.list_entries(
            scope=scope,
            agent_type=agent_type,
            session_id=session_id,
            memory_type=memory_type,
        )

    def get_context_for_agent(
        self,
        agent_type: str,
        task_description: str,
        session_id: str | None = None,
    ) -> list[MemoryEntry]:
        """Retrieve relevant memories for an agent and task."""
        entries = self.store.get_relevant(
            agent_type=agent_type,
            session_id=session_id,
            context=task_description,
            limit=self.config.retrieval_limit,
        )

        # Re-score with task-specific relevance (in-memory only)
        now = datetime.now(timezone.utc)
        query_tokens = set(re.findall(r"\w+", task_description.lower()))
        for entry in entries:
            entry.relevance_score = _compute_relevance(
                entry, query_tokens, now, self.config.decay_half_life_days,
            )
            # Update access stats in-memory only; persisted by run_maintenance
            entry.access_count += 1
            entry.last_accessed_at = now

        # Re-sort by updated score
        entries.sort(key=lambda e: e.relevance_score, reverse=True)
        return entries[:self.config.retrieval_limit]

    def format_memory_prompt(self, entries: list[MemoryEntry]) -> str:
        """Format memory entries into a prompt section for agent injection."""
        if not entries:
            return ""

        lines = ["## Relevant Memory"]
        for entry in entries:
            tag = entry.memory_type.value.upper()
            lines.append(
                f"- [{tag}] {entry.content}"
            )
        return "\n".join(lines) + "\n"

    # -- Post-task extraction --

    def extract_and_store(
        self,
        agent_type: str,
        task_description: str,
        execution_result: dict[str, Any],
        session_id: str,
        node_id: str,
    ) -> list[MemoryEntry]:
        """Analyze execution result and extract learnings."""
        entries: list[MemoryEntry] = []

        # Store the task outcome as an EXPERIENCE
        status = execution_result.get("status", "completed")
        success = execution_result.get("success", status == "completed")
        summary = execution_result.get("summary", execution_result.get("output", ""))
        if summary:
            outcome = self.store_task_outcome(
                agent_type=agent_type,
                task_description=task_description,
                result_summary=str(summary)[:500],
                success=success,
                session_id=session_id,
                node_id=node_id,
            )
            entries.append(outcome)

        # Extract facts from result metadata
        facts = execution_result.get("facts", [])
        for fact in facts:
            if isinstance(fact, str) and fact.strip():
                try:
                    entry = self.store_learning(
                        agent_type=agent_type,
                        content=fact.strip()[:self.config.max_content_length],
                        memory_type=MemoryType.FACT,
                        scope=MemoryScope.GLOBAL,
                        session_id=session_id,
                        source_node_id=node_id,
                    )
                    entries.append(entry)
                except ValueError:
                    pass  # Skip oversized facts

        return entries

    # -- Maintenance --

    def run_maintenance(self) -> dict[str, int]:
        """Run cleanup tasks. Returns stats."""
        expired = self.store.cleanup_expired()
        pruned = self.store.enforce_limits(self.config.max_entries_per_agent)
        # Flush any in-memory access count changes to disk
        self._flush_access_updates()
        self.store.recompute_relevance(self.config.decay_half_life_days)
        return {"expired": expired, "pruned": pruned}

    def _flush_access_updates(self) -> None:
        """Persist entries whose access_count has changed since last load."""
        for entry in self.store.list_entries():
            path = self.store._find_entry_path(entry.id)
            if path:
                # Only write if file content differs (access_count changed)
                try:
                    import json
                    with open(path, "r", encoding="utf-8") as f:
                        disk_data = json.load(f)
                    if disk_data.get("access_count", 0) != entry.access_count:
                        from memory.store import _json_dump_atomic
                        _json_dump_atomic(entry.model_dump(mode="json"), path)
                except Exception:
                    pass

    # -- Statistics --

    def get_stats(self) -> dict[str, Any]:
        """Get memory system statistics."""
        all_entries = self.store.list_entries()

        by_scope: dict[str, int] = {}
        by_type: dict[str, int] = {}
        by_agent: dict[str, int] = {}

        for entry in all_entries:
            scope_key = entry.scope.value
            type_key = entry.memory_type.value
            agent_key = entry.agent_type

            by_scope[scope_key] = by_scope.get(scope_key, 0) + 1
            by_type[type_key] = by_type.get(type_key, 0) + 1
            by_agent[agent_key] = by_agent.get(agent_key, 0) + 1

        avg_relevance = 0.0
        if all_entries:
            avg_relevance = sum(e.relevance_score for e in all_entries) / len(all_entries)

        return {
            "total": len(all_entries),
            "by_scope": by_scope,
            "by_type": by_type,
            "by_agent": by_agent,
            "avg_relevance": round(avg_relevance, 4),
        }
