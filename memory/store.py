"""
MemoryStore -- Persistent storage for agent memory entries.

Follows the atomic-write pattern from control_plane/repository.py:
one JSON file per entry, write to tmp then os.replace.

Directory layout:
    {base_path}/global/{memory_id}.json
    {base_path}/agents/{agent_type}/{memory_id}.json
    {base_path}/sessions/{session_id}/{memory_id}.json
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.models import MemoryEntry, MemoryScope, MemoryType

logger = logging.getLogger(__name__)


def _sanitize_path_component(value: str, name: str) -> str:
    """Validate a path component to prevent path traversal attacks."""
    if not value:
        return value
    dangerous = ("/", "\\", "..", "\x00")
    for seq in dangerous:
        if seq in value:
            raise ValueError(
                f"Invalid {name}: contains forbidden sequence '{seq}'"
            )
    return value


def _json_dump_atomic(data: dict[str, Any], path: Path) -> None:
    """Write *data* to *path* atomically via a temporary file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(path))
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryStore:
    """Persistent store for MemoryEntry objects with atomic writes."""

    def __init__(self, base_path: str = "./data/memory") -> None:
        self.base_path = Path(base_path)
        self._id_to_path: dict[str, Path] = {}
        self._index_loaded = False

    def _scope_dir(
        self,
        scope: MemoryScope,
        agent_type: str = "",
        session_id: str = "",
    ) -> Path:
        """Return the directory for a given scope."""
        if agent_type:
            _sanitize_path_component(agent_type, "agent_type")
        if session_id:
            _sanitize_path_component(session_id, "session_id")
        if scope == MemoryScope.GLOBAL:
            d = self.base_path / "global"
        elif scope == MemoryScope.PRIVATE:
            d = self.base_path / "agents" / agent_type
        else:  # SESSION
            d = self.base_path / "sessions" / session_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _entry_path(self, entry: MemoryEntry) -> Path:
        """Return the file path for an entry."""
        _sanitize_path_component(entry.id, "entry.id")
        d = self._scope_dir(
            entry.scope,
            agent_type=entry.agent_type,
            session_id=entry.session_id or "",
        )
        return d / f"{entry.id}.json"

    def _find_entry_path(self, memory_id: str) -> Path | None:
        """Find an entry file using the in-memory index."""
        if not self._index_loaded:
            self._build_index()
        return self._id_to_path.get(memory_id)

    def _build_index(self) -> None:
        """Build in-memory index mapping memory_id -> file path."""
        self._id_to_path.clear()
        if not self.base_path.exists():
            self._index_loaded = True
            return
        for json_file in self.base_path.rglob("*.json"):
            if json_file.name.endswith(".tmp"):
                continue
            stem = json_file.stem
            self._id_to_path[stem] = json_file
        self._index_loaded = True

    def _index_add(self, memory_id: str, path: Path) -> None:
        """Add or update index entry."""
        if self._index_loaded:
            self._id_to_path[memory_id] = path

    def _index_remove(self, memory_id: str) -> None:
        """Remove index entry."""
        if self._index_loaded:
            self._id_to_path.pop(memory_id, None)

    def _load_entry(self, path: Path) -> MemoryEntry | None:
        """Load a MemoryEntry from a JSON file."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return MemoryEntry(**data)
        except Exception as e:
            logger.warning("Failed to load memory entry %s: %s", path, e)
            return None

    # -- CRUD --

    def store(self, entry: MemoryEntry) -> MemoryEntry:
        """Persist a new memory entry."""
        path = self._entry_path(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        _json_dump_atomic(entry.model_dump(mode="json"), path)
        self._index_add(entry.id, path)
        logger.debug(
            "Stored memory entry %s (scope=%s, type=%s)",
            entry.id, entry.scope, entry.memory_type,
        )
        return entry

    def get(self, memory_id: str) -> MemoryEntry | None:
        """Retrieve a memory entry by ID."""
        path = self._find_entry_path(memory_id)
        if path is None:
            return None
        return self._load_entry(path)

    def update(self, entry: MemoryEntry) -> MemoryEntry:
        """Update an existing memory entry."""
        old_path = self._find_entry_path(entry.id)
        new_path = self._entry_path(entry)
        # Write new file first (atomic), then remove old if scope changed
        _json_dump_atomic(entry.model_dump(mode="json"), new_path)
        self._index_add(entry.id, new_path)
        if old_path and old_path != new_path:
            try:
                old_path.unlink()
            except OSError:
                pass
        return entry

    def delete(self, memory_id: str) -> bool:
        """Delete a memory entry by ID."""
        path = self._find_entry_path(memory_id)
        if path is None:
            return False
        try:
            path.unlink()
            self._index_remove(memory_id)
            return True
        except OSError:
            return False

    # -- Queries --

    def list_entries(
        self,
        scope: MemoryScope | None = None,
        agent_type: str | None = None,
        session_id: str | None = None,
        memory_type: MemoryType | None = None,
    ) -> list[MemoryEntry]:
        """List entries with optional filters."""
        entries: list[MemoryEntry] = []

        if scope == MemoryScope.GLOBAL:
            scan_dirs = [self.base_path / "global"]
        elif scope == MemoryScope.PRIVATE:
            if agent_type:
                scan_dirs = [self.base_path / "agents" / agent_type]
            else:
                agents_dir = self.base_path / "agents"
                # Expand to subdirs
                if agents_dir.exists():
                    scan_dirs = [d for d in agents_dir.iterdir() if d.is_dir()]
                else:
                    scan_dirs = []
        elif scope == MemoryScope.SESSION:
            if session_id:
                scan_dirs = [self.base_path / "sessions" / session_id]
            else:
                sessions_dir = self.base_path / "sessions"
                if sessions_dir.exists():
                    scan_dirs = [d for d in sessions_dir.iterdir() if d.is_dir()]
                else:
                    scan_dirs = []
        else:
            # No scope filter: scan everything
            scan_dirs = []
            for top in ("global", "agents", "sessions"):
                top_dir = self.base_path / top
                if not top_dir.exists():
                    continue
                if top == "global":
                    scan_dirs.append(top_dir)
                else:
                    for d in top_dir.rglob("*"):
                        if d.is_dir():
                            scan_dirs.append(d)

        for scan_dir in scan_dirs:
            if not scan_dir.exists():
                continue
            for f in scan_dir.glob("*.json"):
                entry = self._load_entry(f)
                if entry is None:
                    continue
                # Apply remaining filters
                if agent_type and entry.agent_type != agent_type:
                    continue
                if session_id and entry.session_id != session_id:
                    continue
                if memory_type and entry.memory_type != memory_type:
                    continue
                entries.append(entry)

        return sorted(entries, key=lambda e: e.created_at, reverse=True)

    def search(
        self,
        query: str,
        scope: MemoryScope | None = None,
        agent_type: str | None = None,
        session_id: str | None = None,
        memory_type: MemoryType | None = None,
        limit: int = 10,
    ) -> list[MemoryEntry]:
        """Search entries by keyword matching against content and keywords."""
        all_entries = self.list_entries(
            scope=scope,
            agent_type=agent_type,
            session_id=session_id,
            memory_type=memory_type,
        )

        if not query.strip():
            return all_entries[:limit]

        query_tokens = set(re.findall(r"\w+", query.lower()))

        def score(entry: MemoryEntry) -> float:
            content_tokens = set(re.findall(r"\w+", entry.content.lower()))
            keyword_tokens = set(k.lower() for k in entry.keywords)
            overlap = len(query_tokens & (content_tokens | keyword_tokens))
            return overlap

        scored = [(e, s) for e in all_entries if (s := score(e)) > 0]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [e for e, _ in scored[:limit]]

    def get_relevant(
        self,
        agent_type: str,
        session_id: str | None = None,
        context: str = "",
        limit: int = 10,
    ) -> list[MemoryEntry]:
        """Retrieve relevant memories: PRIVATE for agent + SESSION + GLOBAL."""
        entries: list[MemoryEntry] = []

        # PRIVATE entries for this agent
        entries.extend(self.list_entries(
            scope=MemoryScope.PRIVATE,
            agent_type=agent_type,
        ))

        # SESSION entries for this session
        if session_id:
            entries.extend(self.list_entries(
                scope=MemoryScope.SESSION,
                session_id=session_id,
            ))

        # GLOBAL entries
        entries.extend(self.list_entries(scope=MemoryScope.GLOBAL))

        # Deduplicate by ID
        seen: set[str] = set()
        unique: list[MemoryEntry] = []
        for e in entries:
            if e.id not in seen:
                seen.add(e.id)
                unique.append(e)

        # Sort by relevance_score descending
        unique.sort(key=lambda e: e.relevance_score, reverse=True)
        return unique[:limit]

    # -- Maintenance --

    def record_access(self, memory_id: str, entry: MemoryEntry | None = None) -> None:
        """Increment access count and update last_accessed_at."""
        if entry is None:
            entry = self.get(memory_id)
        if entry is None:
            return
        entry.access_count += 1
        entry.last_accessed_at = _utc_now()
        self.update(entry)

    def cleanup_expired(self) -> int:
        """Remove entries past their expires_at. Returns count removed."""
        now = _utc_now()
        removed = 0
        all_entries = self.list_entries()
        for entry in all_entries:
            if entry.expires_at and entry.expires_at <= now:
                self.delete(entry.id)
                removed += 1
        if removed:
            logger.info("Cleaned up %d expired memory entries", removed)
        return removed

    def enforce_limits(self, max_per_agent: int) -> int:
        """Prune oldest entries when an agent exceeds max_per_agent across all scopes."""
        pruned = 0
        # Group by agent_type (across all scopes) for counting
        agent_entries: dict[str, list[MemoryEntry]] = {}
        for entry in self.list_entries():
            key = entry.agent_type
            agent_entries.setdefault(key, []).append(entry)

        for key, entries in agent_entries.items():
            if len(entries) <= max_per_agent:
                continue
            # Sort by created_at ascending (oldest first)
            entries.sort(key=lambda e: e.created_at)
            to_remove = entries[: len(entries) - max_per_agent]
            for entry in to_remove:
                self.delete(entry.id)
                pruned += 1

        if pruned:
            logger.info("Pruned %d memory entries to enforce limits", pruned)
        return pruned

    def recompute_relevance(self, half_life_days: float) -> None:
        """Recompute relevance_score for all entries based on decay."""
        now = _utc_now()
        all_entries = self.list_entries()
        for entry in all_entries:
            days_since = (now - entry.created_at).total_seconds() / 86400
            recency = 0.5 ** (days_since / max(half_life_days, 0.001))
            frequency_bonus = 1.0 + min(entry.access_count, 10) * 0.1
            entry.relevance_score = recency * frequency_bonus
            # Write directly using known path from index to avoid re-scanning
            path = self._find_entry_path(entry.id)
            if path:
                _json_dump_atomic(entry.model_dump(mode="json"), path)
            else:
                self.update(entry)

        if all_entries:
            logger.info("Recomputed relevance for %d entries", len(all_entries))
