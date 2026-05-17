"""CLI memory commands — search, list, stats, add, cleanup (M3.2)."""

from __future__ import annotations

import json

from core.config import WeaveConfig
from core.models import MemoryScope, MemoryType


def _make_memory_manager():
    """Create a MemoryManager from config."""
    from memory.manager import MemoryManager
    config = WeaveConfig.from_env()
    return MemoryManager(config.memory)


async def cmd_memory_search(args):
    """Search agent memory."""
    manager = _make_memory_manager()
    scope = MemoryScope(args.scope) if args.scope else None
    memory_type = MemoryType(args.type) if args.type else None

    entries = manager.store.search(
        query=args.query,
        scope=scope,
        agent_type=args.agent,
        memory_type=memory_type,
        limit=args.limit,
    )
    result = [
        {
            "id": e.id,
            "agent_type": e.agent_type,
            "scope": e.scope.value,
            "type": e.memory_type.value,
            "content": e.content,
            "keywords": e.keywords,
            "relevance_score": e.relevance_score,
            "access_count": e.access_count,
            "created_at": e.created_at.isoformat(),
        }
        for e in entries
    ]
    print(json.dumps(result, indent=2, default=str))


async def cmd_memory_list(args):
    """List agent memory entries."""
    manager = _make_memory_manager()
    scope = MemoryScope(args.scope) if args.scope else None
    memory_type = MemoryType(args.type) if args.type else None

    entries = manager.store.list_entries(
        scope=scope,
        agent_type=args.agent,
        memory_type=memory_type,
    )
    result = [
        {
            "id": e.id,
            "agent_type": e.agent_type,
            "scope": e.scope.value,
            "type": e.memory_type.value,
            "content": e.content[:200],
            "keywords": e.keywords,
            "relevance_score": e.relevance_score,
            "access_count": e.access_count,
            "created_at": e.created_at.isoformat(),
        }
        for e in entries
    ]
    print(json.dumps(result, indent=2, default=str))


async def cmd_memory_stats(args):
    """Show memory system statistics."""
    manager = _make_memory_manager()
    stats = manager.get_stats()
    print(json.dumps(stats, indent=2, default=str))


async def cmd_memory_add(args):
    """Add a manual memory entry."""
    manager = _make_memory_manager()

    entry = manager.store_learning(
        agent_type=args.agent,
        content=args.content,
        memory_type=MemoryType(args.type),
        scope=MemoryScope(args.scope),
        keywords=args.keywords if args.keywords else None,
    )
    print(json.dumps({
        "id": entry.id,
        "agent_type": entry.agent_type,
        "scope": entry.scope.value,
        "type": entry.memory_type.value,
        "message": "Memory entry added",
    }, indent=2, default=str))


async def cmd_memory_cleanup(args):
    """Run memory maintenance."""
    manager = _make_memory_manager()
    result = manager.run_maintenance()
    print(json.dumps(result, indent=2, default=str))
