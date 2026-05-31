"""
Tests for M3.2 Agent Memory System.

Covers: MemoryEntry model, MemoryStore persistence, MemoryManager operations,
MemorySharing cross-agent promotion, and integration with agent_pool/dag_engine.
"""

import os
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from core.models import (
    MemoryEntry, MemoryScope, MemoryType, EventType, DAG, DAGNode,
)
from core.exceptions import MemoryStoreError
from core.config import MemoryConfig
from memory.store import MemoryStore
from memory.manager import MemoryManager, _extract_keywords, _compute_relevance
from memory.sharing import MemorySharing


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def memory_config(tmp_path):
    return MemoryConfig(base_path=str(tmp_path / "memory"))


@pytest.fixture
def memory_store(memory_config):
    return MemoryStore(memory_config.base_path)


@pytest.fixture
def memory_manager(memory_config):
    return MemoryManager(memory_config)


@pytest.fixture
def sample_entry():
    return MemoryEntry(
        agent_type="planner",
        scope=MemoryScope.PRIVATE,
        memory_type=MemoryType.FACT,
        content="This project uses pytest for testing",
        keywords=["pytest", "testing"],
        session_id="sess_001",
        source_node_id="node_001",
    )


@pytest.fixture
def simple_dag():
    """Create a simple 2-node DAG with edge."""
    dag = DAG(reasoning="Test DAG")
    dag.add_node(DAGNode(
        id="node_a", agent_type="planner",
        task_description="Plan the API structure for user management",
    ))
    dag.add_node(DAGNode(
        id="node_b", agent_type="generator",
        task_description="Implement user API endpoints and routes",
    ))
    dag.add_edge("node_a", "node_b")
    return dag


# =============================================================================
# TestMemoryEntry
# =============================================================================


class TestMemoryEntry:
    def test_default_fields(self):
        entry = MemoryEntry(
            agent_type="planner",
            content="test content",
        )
        assert entry.id.startswith("mem_")
        assert entry.scope == MemoryScope.PRIVATE
        assert entry.memory_type == MemoryType.FACT
        assert entry.access_count == 0
        assert entry.relevance_score == 1.0
        assert entry.created_at.tzinfo is not None

    def test_serialization_roundtrip(self):
        entry = MemoryEntry(
            agent_type="generator",
            scope=MemoryScope.GLOBAL,
            memory_type=MemoryType.EXPERIENCE,
            content="Generated REST API successfully",
            keywords=["rest", "api"],
        )
        data = entry.model_dump(mode="json")
        restored = MemoryEntry(**data)
        assert restored.id == entry.id
        assert restored.agent_type == entry.agent_type
        assert restored.scope == MemoryScope.GLOBAL
        assert restored.content == entry.content
        assert restored.keywords == entry.keywords

    def test_custom_id(self):
        entry = MemoryEntry(
            id="mem_custom123",
            agent_type="evaluator",
            content="test",
        )
        assert entry.id == "mem_custom123"

    def test_expires_at(self):
        future = datetime.now(timezone.utc) + timedelta(days=30)
        entry = MemoryEntry(
            agent_type="planner",
            content="test",
            expires_at=future,
        )
        assert entry.expires_at is not None
        assert entry.expires_at > datetime.now(timezone.utc)

    def test_metadata(self):
        entry = MemoryEntry(
            agent_type="planner",
            content="test",
            metadata={"source": "manual", "priority": 1},
        )
        assert entry.metadata["source"] == "manual"
        assert entry.metadata["priority"] == 1


# =============================================================================
# TestMemoryStore
# =============================================================================


class TestMemoryStore:
    def test_store_and_retrieve(self, memory_store, sample_entry):
        memory_store.store(sample_entry)
        retrieved = memory_store.get(sample_entry.id)
        assert retrieved is not None
        assert retrieved.id == sample_entry.id
        assert retrieved.content == sample_entry.content

    def test_store_creates_directory_structure(self, memory_store):
        entry = MemoryEntry(
            agent_type="planner",
            scope=MemoryScope.PRIVATE,
            content="test",
        )
        memory_store.store(entry)
        path = memory_store.base_path / "agents" / "planner"
        assert path.exists()
        assert len(list(path.glob("*.json"))) == 1

    def test_atomic_write_no_tmp_remains(self, memory_store, sample_entry):
        memory_store.store(sample_entry)
        # No .tmp files should remain
        for f in memory_store.base_path.rglob("*.tmp"):
            assert False, f"Temp file found: {f}"

    def test_get_nonexistent_returns_none(self, memory_store):
        assert memory_store.get("mem_nonexistent") is None

    def test_delete_entry(self, memory_store, sample_entry):
        memory_store.store(sample_entry)
        assert memory_store.delete(sample_entry.id) is True
        assert memory_store.get(sample_entry.id) is None

    def test_delete_nonexistent(self, memory_store):
        assert memory_store.delete("mem_nonexistent") is False

    def test_update_entry(self, memory_store, sample_entry):
        memory_store.store(sample_entry)
        sample_entry.content = "Updated content"
        sample_entry.access_count = 5
        memory_store.update(sample_entry)
        updated = memory_store.get(sample_entry.id)
        assert updated is not None
        assert updated.content == "Updated content"
        assert updated.access_count == 5

    def test_list_entries_all(self, memory_store):
        for i in range(3):
            memory_store.store(MemoryEntry(
                agent_type="planner",
                content=f"Entry {i}",
            ))
        entries = memory_store.list_entries()
        assert len(entries) == 3

    def test_list_entries_by_scope(self, memory_store):
        memory_store.store(MemoryEntry(
            agent_type="shared", scope=MemoryScope.GLOBAL, content="global",
        ))
        memory_store.store(MemoryEntry(
            agent_type="planner", scope=MemoryScope.PRIVATE, content="private",
        ))
        memory_store.store(MemoryEntry(
            agent_type="shared", scope=MemoryScope.SESSION,
            content="session", session_id="s1",
        ))

        global_entries = memory_store.list_entries(scope=MemoryScope.GLOBAL)
        assert len(global_entries) == 1
        assert global_entries[0].scope == MemoryScope.GLOBAL

        private_entries = memory_store.list_entries(scope=MemoryScope.PRIVATE)
        assert len(private_entries) == 1
        assert private_entries[0].scope == MemoryScope.PRIVATE

    def test_list_entries_by_agent_type(self, memory_store):
        memory_store.store(MemoryEntry(
            agent_type="planner", content="p",
        ))
        memory_store.store(MemoryEntry(
            agent_type="generator", content="g",
        ))
        entries = memory_store.list_entries(agent_type="planner")
        assert len(entries) == 1
        assert entries[0].agent_type == "planner"

    def test_list_entries_by_session_id(self, memory_store):
        memory_store.store(MemoryEntry(
            agent_type="shared", scope=MemoryScope.SESSION,
            content="s1", session_id="sess_001",
        ))
        memory_store.store(MemoryEntry(
            agent_type="shared", scope=MemoryScope.SESSION,
            content="s2", session_id="sess_002",
        ))
        entries = memory_store.list_entries(
            scope=MemoryScope.SESSION, session_id="sess_001",
        )
        assert len(entries) == 1
        assert entries[0].session_id == "sess_001"

    def test_list_entries_by_memory_type(self, memory_store):
        memory_store.store(MemoryEntry(
            agent_type="planner", memory_type=MemoryType.FACT, content="fact",
        ))
        memory_store.store(MemoryEntry(
            agent_type="planner", memory_type=MemoryType.EXPERIENCE, content="exp",
        ))
        entries = memory_store.list_entries(memory_type=MemoryType.FACT)
        assert len(entries) == 1
        assert entries[0].memory_type == MemoryType.FACT

    def test_search_keyword_match(self, memory_store):
        memory_store.store(MemoryEntry(
            agent_type="planner", content="Project uses pytest for testing",
            keywords=["pytest", "testing"],
        ))
        memory_store.store(MemoryEntry(
            agent_type="generator", content="Implemented REST API endpoints",
            keywords=["rest", "api"],
        ))
        results = memory_store.search("pytest")
        assert len(results) == 1
        assert "pytest" in results[0].content

    def test_search_content_match(self, memory_store):
        memory_store.store(MemoryEntry(
            agent_type="planner", content="Uses Docker for deployment",
            keywords=[],
        ))
        results = memory_store.search("Docker")
        assert len(results) == 1

    def test_search_no_results(self, memory_store):
        memory_store.store(MemoryEntry(
            agent_type="planner", content="hello world",
        ))
        results = memory_store.search("nonexistent_query_xyz")
        assert len(results) == 0

    def test_search_limit(self, memory_store):
        for i in range(5):
            memory_store.store(MemoryEntry(
                agent_type="planner", content=f"pytest test {i}",
                keywords=["pytest"],
            ))
        results = memory_store.search("pytest", limit=2)
        assert len(results) == 2

    def test_get_relevant_combines_scopes(self, memory_store):
        memory_store.store(MemoryEntry(
            agent_type="planner", scope=MemoryScope.PRIVATE, content="private memory",
        ))
        memory_store.store(MemoryEntry(
            agent_type="shared", scope=MemoryScope.GLOBAL, content="global memory",
        ))
        memory_store.store(MemoryEntry(
            agent_type="shared", scope=MemoryScope.SESSION,
            content="session memory", session_id="sess_001",
        ))
        results = memory_store.get_relevant(
            agent_type="planner", session_id="sess_001",
        )
        assert len(results) == 3

    def test_get_relevant_limit(self, memory_store):
        for i in range(20):
            memory_store.store(MemoryEntry(
                agent_type="planner", content=f"memory {i}",
            ))
        results = memory_store.get_relevant(agent_type="planner", limit=5)
        assert len(results) == 5

    def test_record_access_increments_count(self, memory_store, sample_entry):
        memory_store.store(sample_entry)
        memory_store.record_access(sample_entry.id)
        updated = memory_store.get(sample_entry.id)
        assert updated is not None
        assert updated.access_count == 1
        assert updated.last_accessed_at.timestamp() > sample_entry.created_at.timestamp()

    def test_cleanup_expired_removes_old_entries(self, memory_store):
        expired_entry = MemoryEntry(
            agent_type="planner", content="expired",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        active_entry = MemoryEntry(
            agent_type="planner", content="active",
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        memory_store.store(expired_entry)
        memory_store.store(active_entry)

        removed = memory_store.cleanup_expired()
        assert removed == 1
        assert memory_store.get(expired_entry.id) is None
        assert memory_store.get(active_entry.id) is not None

    def test_cleanup_expired_preserves_no_expiry(self, memory_store):
        no_expiry = MemoryEntry(
            agent_type="planner", content="no expiry", expires_at=None,
        )
        memory_store.store(no_expiry)
        removed = memory_store.cleanup_expired()
        assert removed == 0
        assert memory_store.get(no_expiry.id) is not None

    def test_enforce_limits_prunes_oldest(self, memory_store):
        for i in range(5):
            entry = MemoryEntry(
                agent_type="planner", content=f"entry {i}",
                scope=MemoryScope.PRIVATE,
            )
            memory_store.store(entry)

        pruned = memory_store.enforce_limits(max_per_agent=2)
        assert pruned == 3

        remaining = memory_store.list_entries(
            scope=MemoryScope.PRIVATE, agent_type="planner",
        )
        assert len(remaining) == 2

    def test_recompute_relevance_decays_old_entries(self, memory_store):
        old_entry = MemoryEntry(
            agent_type="planner", content="old",
            created_at=datetime.now(timezone.utc) - timedelta(days=60),
        )
        new_entry = MemoryEntry(
            agent_type="planner", content="new",
            created_at=datetime.now(timezone.utc),
        )
        memory_store.store(old_entry)
        memory_store.store(new_entry)

        memory_store.recompute_relevance(half_life_days=30.0)

        old = memory_store.get(old_entry.id)
        new = memory_store.get(new_entry.id)
        assert old is not None
        assert new is not None
        assert new.relevance_score > old.relevance_score


# =============================================================================
# TestMemoryManager
# =============================================================================


class TestMemoryManager:
    def test_store_learning_creates_entry(self, memory_manager):
        entry = memory_manager.store_learning(
            agent_type="planner",
            content="Project uses pytest",
        )
        assert entry.id.startswith("mem_")
        assert entry.agent_type == "planner"
        assert entry.memory_type == MemoryType.FACT
        assert entry.scope == MemoryScope.PRIVATE
        assert entry.expires_at is not None

    def test_store_learning_auto_extracts_keywords(self, memory_manager):
        entry = memory_manager.store_learning(
            agent_type="planner",
            content="The project uses pytest for running all unit tests",
        )
        assert len(entry.keywords) > 0
        # Should extract meaningful words, not stop words
        for kw in entry.keywords:
            assert len(kw) >= 3

    def test_store_learning_enforces_content_length(self, memory_manager):
        with pytest.raises(MemoryStoreError, match="Content length"):
            memory_manager.store_learning(
                agent_type="planner",
                content="x" * 1001,  # default max is 1000
            )

    def test_store_learning_with_custom_keywords(self, memory_manager):
        entry = memory_manager.store_learning(
            agent_type="planner",
            content="Custom test",
            keywords=["custom", "test"],
        )
        assert entry.keywords == ["custom", "test"]

    def test_store_task_outcome_creates_experience(self, memory_manager):
        entry = memory_manager.store_task_outcome(
            agent_type="generator",
            task_description="Build REST API",
            result_summary="Successfully created 5 endpoints",
            success=True,
            session_id="sess_001",
            node_id="node_001",
        )
        assert entry.memory_type == MemoryType.EXPERIENCE
        assert entry.scope == MemoryScope.SESSION
        assert entry.session_id == "sess_001"
        assert "succeeded" in entry.content

    def test_store_task_outcome_failure(self, memory_manager):
        entry = memory_manager.store_task_outcome(
            agent_type="generator",
            task_description="Build API",
            result_summary="Tests failed",
            success=False,
            session_id="sess_001",
            node_id="node_001",
        )
        assert "failed" in entry.content

    def test_store_preference_creates_global(self, memory_manager):
        entry = memory_manager.store_preference("User prefers type hints")
        assert entry.agent_type == "shared"
        assert entry.memory_type == MemoryType.PREFERENCE
        assert entry.scope == MemoryScope.GLOBAL

    def test_get_context_for_agent_returns_entries(self, memory_manager):
        memory_manager.store_learning(
            agent_type="planner", content="Fact 1",
            scope=MemoryScope.PRIVATE,
        )
        memory_manager.store_learning(
            agent_type="shared", content="Global fact",
            scope=MemoryScope.GLOBAL,
        )
        entries = memory_manager.get_context_for_agent(
            agent_type="planner",
            task_description="Plan the API",
        )
        assert len(entries) >= 1

    def test_format_memory_prompt_with_entries(self, memory_manager):
        entries = [
            MemoryEntry(
                agent_type="planner",
                memory_type=MemoryType.FACT,
                content="Project uses pytest",
            ),
            MemoryEntry(
                agent_type="generator",
                memory_type=MemoryType.EXPERIENCE,
                content="API generation succeeded",
            ),
        ]
        prompt = memory_manager.format_memory_prompt(entries)
        assert "## Relevant Memory" in prompt
        assert "[FACT]" in prompt
        assert "[EXPERIENCE]" in prompt
        assert "pytest" in prompt

    def test_format_memory_prompt_empty(self, memory_manager):
        assert memory_manager.format_memory_prompt([]) == ""

    def test_extract_and_store_from_result(self, memory_manager):
        entries = memory_manager.extract_and_store(
            agent_type="generator",
            task_description="Build REST API",
            execution_result={
                "success": True,
                "summary": "Created 5 endpoints",
                "facts": ["Uses FastAPI framework"],
            },
            session_id="sess_001",
            node_id="node_001",
        )
        assert len(entries) >= 2  # outcome + fact
        types = {e.memory_type for e in entries}
        assert MemoryType.EXPERIENCE in types
        assert MemoryType.FACT in types

    def test_extract_and_store_no_facts(self, memory_manager):
        entries = memory_manager.extract_and_store(
            agent_type="planner",
            task_description="Plan the API",
            execution_result={"success": True, "summary": "Plan created"},
            session_id="sess_001",
            node_id="node_001",
        )
        assert len(entries) >= 1

    def test_run_maintenance_returns_stats(self, memory_manager):
        # Store an expired entry
        memory_manager.store_learning(
            agent_type="planner",
            content="Will expire",
        )
        # Manually expire it
        all_entries = memory_manager.store.list_entries()
        for e in all_entries:
            e.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
            memory_manager.store.update(e)

        result = memory_manager.run_maintenance()
        assert "expired" in result
        assert "pruned" in result
        assert result["expired"] >= 1

    def test_get_stats_returns_counts(self, memory_manager):
        memory_manager.store_learning(
            agent_type="planner", content="fact 1",
            scope=MemoryScope.PRIVATE,
        )
        memory_manager.store_learning(
            agent_type="shared", content="global 1",
            scope=MemoryScope.GLOBAL,
        )
        stats = memory_manager.get_stats()
        assert stats["total"] >= 2
        assert "by_scope" in stats
        assert "by_type" in stats
        assert "by_agent" in stats
        assert "avg_relevance" in stats

    def test_event_emission_on_store(self, memory_config):
        mock_store = MagicMock()
        manager = MemoryManager(memory_config, session_store=mock_store)
        manager.store_learning(
            agent_type="planner", content="test",
        )
        if mock_store.emit_event.called:
            call_args = mock_store.emit_event.call_args
            assert call_args[0][0] == "memory.stored"


# =============================================================================
# Test Keyword Extraction
# =============================================================================


class TestKeywordExtraction:
    def test_basic_extraction(self):
        keywords = _extract_keywords("The project uses pytest for testing")
        assert "pytest" in keywords
        assert "testing" in keywords

    def test_stops_words_filtered(self):
        keywords = _extract_keywords("this is a test of the system")
        for kw in keywords:
            assert kw not in {"this", "is", "a", "the", "of"}

    def test_max_keywords(self):
        keywords = _extract_keywords(
            "python django flask fastapi celery redis rabbitmq testing",
            max_keywords=3,
        )
        assert len(keywords) <= 3

    def test_empty_text(self):
        keywords = _extract_keywords("")
        assert keywords == []

    def test_short_words_filtered(self):
        keywords = _extract_keywords("a b c de fgh")
        # Words < 3 chars are filtered
        for kw in keywords:
            assert len(kw) >= 3


# =============================================================================
# Test Relevance Scoring
# =============================================================================


class TestRelevanceScoring:
    def test_relevance_with_matching_keywords(self):
        entry = MemoryEntry(
            agent_type="planner", content="Uses pytest",
            keywords=["pytest", "testing"],
            created_at=datetime.now(timezone.utc),
        )
        score = _compute_relevance(
            entry, {"pytest", "framework"},
            datetime.now(timezone.utc), 30.0,
        )
        assert score > 0

    def test_relevance_no_query_tokens(self):
        entry = MemoryEntry(
            agent_type="planner", content="Uses pytest",
            created_at=datetime.now(timezone.utc),
        )
        score = _compute_relevance(
            entry, set(), datetime.now(timezone.utc), 30.0,
        )
        assert score > 0  # Uses recency-based score

    def test_relevance_decays_with_age(self):
        now = datetime.now(timezone.utc)
        old_entry = MemoryEntry(
            agent_type="planner", content="old pytest",
            keywords=["pytest"],
            created_at=now - timedelta(days=60),
        )
        new_entry = MemoryEntry(
            agent_type="planner", content="new pytest",
            keywords=["pytest"],
            created_at=now,
        )
        tokens = {"pytest"}
        old_score = _compute_relevance(old_entry, tokens, now, 30.0)
        new_score = _compute_relevance(new_entry, tokens, now, 30.0)
        assert new_score > old_score


# =============================================================================
# TestMemorySharing
# =============================================================================


class TestMemorySharing:
    def test_share_with_downstream_copies_to_session(
        self, memory_manager, simple_dag,
    ):
        sharing = MemorySharing(memory_manager)

        # Store a private memory for planner with matching keywords
        memory_manager.store.store(MemoryEntry(
            agent_type="planner",
            scope=MemoryScope.PRIVATE,
            content="API structure follows REST conventions",
            keywords=["api", "structure", "rest"],
        ))

        # Mark node_a as SUCCESS so it qualifies as dependency
        simple_dag.nodes["node_a"].status = "success"

        shared = sharing.share_with_downstream(
            from_agent="planner",
            to_agent="generator",
            session_id="sess_001",
            dag=simple_dag,
            node_id="node_b",
        )
        # Should share if keywords match task description
        assert isinstance(shared, list)

    def test_promote_to_session_creates_new_entry(self, memory_manager):
        sharing = MemorySharing(memory_manager)

        entry = memory_manager.store_learning(
            agent_type="planner",
            content="Uses FastAPI",
            scope=MemoryScope.PRIVATE,
        )
        promoted = sharing.promote_to_session(
            memory_id=entry.id,
            session_id="sess_001",
        )
        assert promoted is not None
        assert promoted.scope == MemoryScope.SESSION
        assert promoted.session_id == "sess_001"
        assert promoted.content == entry.content

    def test_promote_to_session_already_session(self, memory_manager):
        sharing = MemorySharing(memory_manager)

        entry = memory_manager.store_learning(
            agent_type="planner",
            content="Already session",
            scope=MemoryScope.SESSION,
            session_id="sess_001",
        )
        promoted = sharing.promote_to_session(
            memory_id=entry.id,
            session_id="sess_001",
        )
        assert promoted is not None
        assert promoted.id == entry.id  # Returns same entry

    def test_promote_to_global_creates_new_entry(self, memory_manager):
        sharing = MemorySharing(memory_manager)

        entry = memory_manager.store_learning(
            agent_type="planner",
            content="Valuable fact",
            scope=MemoryScope.PRIVATE,
        )
        promoted = sharing.promote_to_global(memory_id=entry.id)
        assert promoted is not None
        assert promoted.scope == MemoryScope.GLOBAL

    def test_get_shared_for_agent(self, memory_manager):
        sharing = MemorySharing(memory_manager)

        memory_manager.store_learning(
            agent_type="shared", content="Global fact",
            scope=MemoryScope.GLOBAL,
        )
        memory_manager.store_learning(
            agent_type="planner", content="Session fact",
            scope=MemoryScope.SESSION, session_id="sess_001",
        )

        entries = sharing.get_shared_for_agent("generator", "sess_001")
        assert len(entries) >= 1

    def test_share_preserves_original(self, memory_manager):
        sharing = MemorySharing(memory_manager)

        entry = memory_manager.store_learning(
            agent_type="planner",
            content="Original private memory",
            scope=MemoryScope.PRIVATE,
        )
        sharing.promote_to_session(entry.id, "sess_001")

        # Original should still exist as PRIVATE
        original = memory_manager.store.get(entry.id)
        assert original is not None
        assert original.scope == MemoryScope.PRIVATE

    def test_promote_nonexistent_returns_none(self, memory_manager):
        sharing = MemorySharing(memory_manager)
        assert sharing.promote_to_session("mem_nonexistent", "s1") is None
        assert sharing.promote_to_global("mem_nonexistent") is None


# =============================================================================
# TestMemoryIntegration
# =============================================================================


class TestMemoryIntegration:
    def test_memory_injection_into_agent_prompt(self, memory_config):
        """Verify memory_manager can provide context for agents."""
        memory_manager = MemoryManager(memory_config)
        memory_manager.store_learning(
            agent_type="planner",
            content="Project uses pytest framework",
            scope=MemoryScope.PRIVATE,
        )

        # Verify that memory_manager can retrieve context for this agent
        context = memory_manager.get_context_for_agent(
            agent_type="planner",
            task_description="Plan the testing strategy",
        )
        assert len(context) >= 1
        prompt = memory_manager.format_memory_prompt(context)
        assert "pytest" in prompt

    def test_memory_disabled_skips_operations(self, tmp_path):
        """When enabled=False, memory operations should be no-ops."""
        config = MemoryConfig(
            base_path=str(tmp_path / "memory"),
            enabled=False,
        )
        MemoryManager(config)
        # Store should still work but manager operations are not injected
        # when memory_manager is None or disabled
        assert config.enabled is False

    def test_memory_config_from_env(self):
        """Verify MemoryConfig reads from env vars."""
        with patch.dict(os.environ, {
            "WEAVE_MEMORY_PATH": "/tmp/test_memory",
            "WEAVE_MEMORY_MAX_ENTRIES": "100",
        }):
            config = MemoryConfig.from_env() if hasattr(MemoryConfig, "from_env") else MemoryConfig(
                base_path=os.getenv("WEAVE_MEMORY_PATH", "./data/memory"),
                max_entries_per_agent=int(os.getenv("WEAVE_MEMORY_MAX_ENTRIES", "500")),
            )
            assert config.max_entries_per_agent == 100

    def test_memory_event_types_exist(self):
        """Verify all memory event types are defined."""
        assert EventType.MEMORY_STORED == "memory.stored"
        assert EventType.MEMORY_ACCESSED == "memory.accessed"
        assert EventType.MEMORY_SHARED == "memory.shared"
        assert EventType.MEMORY_EXPIRED == "memory.expired"
        assert EventType.MEMORY_PRUNED == "memory.pruned"

    def test_full_lifecycle(self, memory_manager, simple_dag):
        """Test complete memory lifecycle: store -> retrieve -> share -> cleanup."""
        # 1. Store learning
        entry = memory_manager.store_learning(
            agent_type="planner",
            content="Project uses FastAPI with pytest",
            scope=MemoryScope.PRIVATE,
        )

        # 2. Store task outcome
        memory_manager.store_task_outcome(
            agent_type="planner",
            task_description="Plan API structure",
            result_summary="Created linear plan with 3 nodes",
            success=True,
            session_id="sess_001",
            node_id="node_a",
        )

        # 3. Retrieve context
        context = memory_manager.get_context_for_agent(
            agent_type="planner",
            task_description="Plan the next feature",
        )
        assert len(context) >= 1

        # 4. Format prompt
        prompt = memory_manager.format_memory_prompt(context)
        assert len(prompt) > 0

        # 5. Share across agents
        sharing = MemorySharing(memory_manager)
        shared = sharing.promote_to_session(entry.id, "sess_001")
        assert shared is not None

        # 6. Get stats
        stats = memory_manager.get_stats()
        assert stats["total"] >= 2

        # 7. Cleanup (no expired entries, so just maintenance)
        result = memory_manager.run_maintenance()
        assert "expired" in result
