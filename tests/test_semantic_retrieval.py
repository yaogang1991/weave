"""Tests for hybrid semantic retrieval integration (#508 P2)."""
import pytest

from core.config import MemoryConfig
from core.memory_models import MemoryEntry
from memory.manager import MemoryManager


class TestHybridRetrieval:
    """Verify MemoryManager.get_context_for_agent uses hybrid retrieval."""

    @pytest.fixture
    def manager(self, tmp_path):
        config = MemoryConfig(
            base_path=str(tmp_path / "memory"),
            semantic_search_enabled=True,
            embedding_provider="local",
            retrieval_limit=5,
        )
        return MemoryManager(config)

    @pytest.fixture
    def populated_manager(self, tmp_path):
        config = MemoryConfig(
            base_path=str(tmp_path / "memory"),
            semantic_search_enabled=True,
            embedding_provider="local",
            retrieval_limit=5,
        )
        mgr = MemoryManager(config)
        # Store some entries
        mgr.store_learning(
            agent_type="planner",
            content="Authentication module uses JWT tokens for session management",
            keywords=["authentication", "jwt", "session"],
        )
        mgr.store_learning(
            agent_type="generator",
            content="Database connection uses connection pooling with SQLAlchemy",
            keywords=["database", "sqlalchemy", "pooling"],
        )
        mgr.store_learning(
            agent_type="planner",
            content="Error handling follows the repository pattern with custom exceptions",
            keywords=["error", "repository", "exceptions"],
        )
        return mgr

    def test_semantic_search_enabled_creates_provider(self, tmp_path):
        """Manager creates embedding provider when semantic search enabled."""
        config = MemoryConfig(
            base_path=str(tmp_path / "memory"),
            semantic_search_enabled=True,
            embedding_provider="local",
        )
        mgr = MemoryManager(config)
        assert mgr._embedding_provider is not None

    def test_semantic_search_disabled_no_provider(self, tmp_path):
        """Manager skips embedding provider when semantic search disabled."""
        config = MemoryConfig(
            base_path=str(tmp_path / "memory"),
            semantic_search_enabled=False,
        )
        mgr = MemoryManager(config)
        assert mgr._embedding_provider is None

    def test_hybrid_retrieval_returns_entries(self, populated_manager):
        """Hybrid retrieval returns relevant entries."""
        results = populated_manager.get_context_for_agent(
            agent_type="planner",
            task_description="implement user authentication with JWT",
        )
        assert isinstance(results, list)
        assert len(results) > 0
        # All results should be MemoryEntry
        for entry in results:
            assert isinstance(entry, MemoryEntry)

    def test_hybrid_retrieval_respects_limit(self, populated_manager):
        """Results are capped at retrieval_limit."""
        results = populated_manager.get_context_for_agent(
            agent_type="planner",
            task_description="implement authentication",
        )
        assert len(results) <= populated_manager.config.retrieval_limit

    def test_hybrid_retrieval_scores_are_float(self, populated_manager):
        """All entries have relevance_score as float."""
        results = populated_manager.get_context_for_agent(
            agent_type="planner",
            task_description="database connection setup",
        )
        for entry in results:
            assert isinstance(entry.relevance_score, float)

    def test_hybrid_retrieval_sorted_by_score(self, populated_manager):
        """Results are sorted by relevance_score descending."""
        results = populated_manager.get_context_for_agent(
            agent_type="planner",
            task_description="authentication implementation",
        )
        if len(results) > 1:
            for i in range(len(results) - 1):
                assert results[i].relevance_score >= results[i + 1].relevance_score

    def test_fallback_to_keyword_without_provider(self, tmp_path):
        """Falls back to keyword search when no embedding provider."""
        config = MemoryConfig(
            base_path=str(tmp_path / "memory"),
            semantic_search_enabled=False,
            retrieval_limit=5,
        )
        mgr = MemoryManager(config)
        mgr.store_learning(
            agent_type="planner",
            content="Test entry for keyword search fallback",
            keywords=["test", "keyword"],
        )
        results = mgr.get_context_for_agent(
            agent_type="planner",
            task_description="test entry keyword",
        )
        assert len(results) > 0

    def test_access_count_increments(self, populated_manager):
        """Access count increments on retrieval."""
        results = populated_manager.get_context_for_agent(
            agent_type="planner",
            task_description="authentication",
        )
        for entry in results:
            assert entry.access_count >= 1


class TestMemoryConfigEmbedding:
    """Verify MemoryConfig embedding settings."""

    def test_default_embedding_provider(self):
        config = MemoryConfig()
        assert config.embedding_provider == "local"

    def test_default_semantic_search_enabled(self):
        config = MemoryConfig()
        assert config.semantic_search_enabled is True

    def test_custom_embedding_provider(self):
        config = MemoryConfig(embedding_provider="openai")
        assert config.embedding_provider == "openai"
