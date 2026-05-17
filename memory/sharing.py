"""
MemorySharing -- Cross-agent memory sharing within and across sessions.

Manages promotion of memories between scopes:
  PRIVATE -> SESSION (share within a session)
  SESSION -> GLOBAL (persist across sessions)
"""

from __future__ import annotations

import logging
from typing import Any

from core.models import MemoryEntry, MemoryScope, EventType
from memory.manager import MemoryManager

logger = logging.getLogger(__name__)


class MemorySharing:
    """
    Manages cross-agent memory sharing within a session.

    When agent A produces insights relevant to agent B's upcoming task,
    this module promotes PRIVATE memories to SESSION scope or creates
    SESSION-scope entries that reference the original.
    """

    def __init__(self, memory_manager: MemoryManager) -> None:
        self.manager = memory_manager

    def share_with_downstream(
        self,
        from_agent: str,
        to_agent: str,
        session_id: str,
        dag: Any,
        node_id: str,
    ) -> list[MemoryEntry]:
        """Share relevant memories from upstream agent to downstream agent.

        Examines the DAG to find what the downstream agent will work on,
        then copies relevant PRIVATE memories from from_agent into SESSION scope.
        """
        # Get the downstream node's task description
        node = dag.nodes.get(node_id)
        if not node:
            return []

        # Get private memories from the upstream agent
        private_entries = self.manager.store.list_entries(
            scope=MemoryScope.PRIVATE,
            agent_type=from_agent,
        )

        shared: list[MemoryEntry] = []
        for entry in private_entries:
            # Simple relevance: check keyword overlap with downstream task
            task_keywords = set(
                w.lower() for w in node.task_description.split()
                if len(w) > 3
            )
            entry_tokens = set(k.lower() for k in entry.keywords)
            if not task_keywords or not entry_tokens:
                continue

            overlap = len(task_keywords & entry_tokens)
            if overlap > 0:
                promoted = self.promote_to_session(
                    memory_id=entry.id,
                    session_id=session_id,
                    target_agent=to_agent,
                )
                if promoted:
                    shared.append(promoted)

        if shared:
            self.manager._emit_event(EventType.MEMORY_SHARED, {
                "from_agent": from_agent,
                "to_agent": to_agent,
                "session_id": session_id,
                "count": len(shared),
            })
            logger.debug(
                "Shared %d memories from %s to %s",
                len(shared), from_agent, to_agent,
            )

        return shared

    def promote_to_session(
        self,
        memory_id: str,
        session_id: str,
        target_agent: str | None = None,
    ) -> MemoryEntry | None:
        """Create a SESSION-scope copy of an existing entry."""
        original = self.manager.store.get(memory_id)
        if original is None:
            return None

        # Don't duplicate if already session-scoped
        if original.scope == MemoryScope.SESSION:
            return original

        # Check for existing session-scoped copy with same content+agent+type
        existing = self.manager.store.list_entries(
            scope=MemoryScope.SESSION,
            session_id=session_id,
        )
        agent = target_agent or original.agent_type
        for e in existing:
            if (e.content == original.content
                    and e.agent_type == agent
                    and e.source_node_id == original.source_node_id
                    and e.memory_type == original.memory_type):
                return e

        # Create new entry in SESSION scope
        return self.manager.store_learning(
            agent_type=agent,
            content=original.content,
            memory_type=original.memory_type,
            scope=MemoryScope.SESSION,
            session_id=session_id,
            source_node_id=original.source_node_id,
            keywords=list(original.keywords),
        )

    def promote_to_global(
        self,
        memory_id: str,
    ) -> MemoryEntry | None:
        """Promote a memory to GLOBAL scope for cross-session persistence."""
        original = self.manager.store.get(memory_id)
        if original is None:
            return None

        if original.scope == MemoryScope.GLOBAL:
            return original

        # Check for existing global copy with same content+agent+type
        existing = self.manager.store.list_entries(
            scope=MemoryScope.GLOBAL,
        )
        for e in existing:
            if (e.content == original.content
                    and e.agent_type == original.agent_type
                    and e.source_node_id == original.source_node_id
                    and e.memory_type == original.memory_type):
                return e

        return self.manager.store_learning(
            agent_type=original.agent_type,
            content=original.content,
            memory_type=original.memory_type,
            scope=MemoryScope.GLOBAL,
            source_node_id=original.source_node_id,
            keywords=list(original.keywords),
        )

    def get_shared_for_agent(
        self,
        agent_type: str,
        session_id: str,
    ) -> list[MemoryEntry]:
        """Get all shared memories available to an agent in a session."""
        entries: list[MemoryEntry] = []

        # SESSION-scope memories for this session
        entries.extend(
            self.manager.store.list_entries(
                scope=MemoryScope.SESSION,
                session_id=session_id,
            )
        )

        # GLOBAL memories
        entries.extend(
            self.manager.store.list_entries(scope=MemoryScope.GLOBAL)
        )

        # Deduplicate
        seen: set[str] = set()
        unique: list[MemoryEntry] = []
        for e in entries:
            if e.id not in seen:
                seen.add(e.id)
                unique.append(e)

        unique.sort(key=lambda e: e.relevance_score, reverse=True)
        return unique
