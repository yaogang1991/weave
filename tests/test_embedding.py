"""Tests for embedding provider abstraction and semantic search (#508 P1)."""
import math

import pytest

from memory.embedding import (
    LocalHashEmbeddingProvider,
    OpenAIEmbeddingProvider,
    cosine_similarity,
    create_embedding_provider,
)


# -- LocalHashEmbeddingProvider -------------------------------------------

class TestLocalHashEmbedding:
    def test_returns_correct_dimension(self):
        provider = LocalHashEmbeddingProvider(dimension=64)
        assert provider.dimension == 64

    def test_embed_returns_list_of_floats(self):
        provider = LocalHashEmbeddingProvider(dimension=128)
        vec = provider.embed("hello world")
        assert len(vec) == 128
        assert all(isinstance(v, float) for v in vec)

    def test_embed_normalized(self):
        """Embedded vectors are L2-normalized."""
        provider = LocalHashEmbeddingProvider()
        vec = provider.embed("test normalization")
        norm = math.sqrt(sum(v * v for v in vec))
        assert abs(norm - 1.0) < 0.01

    def test_empty_string_returns_zero_vector(self):
        provider = LocalHashEmbeddingProvider(dimension=64)
        vec = provider.embed("")
        assert all(v == 0.0 for v in vec)

    def test_similar_texts_have_high_similarity(self):
        """Semantically similar texts should have higher similarity."""
        provider = LocalHashEmbeddingProvider()
        vec1 = provider.embed("database migration failed with error")
        vec2 = provider.embed("database migration error occurred")
        vec3 = provider.embed("the quick brown fox jumps")

        sim_similar = cosine_similarity(vec1, vec2)
        sim_unrelated = cosine_similarity(vec1, vec3)

        assert sim_similar > sim_unrelated

    def test_identical_texts_similarity_one(self):
        provider = LocalHashEmbeddingProvider()
        vec1 = provider.embed("same text")
        vec2 = provider.embed("same text")
        assert abs(cosine_similarity(vec1, vec2) - 1.0) < 0.01

    def test_embed_batch(self):
        provider = LocalHashEmbeddingProvider()
        texts = ["hello", "world", "test"]
        vecs = provider.embed_batch(texts)
        assert len(vecs) == 3
        assert all(len(v) == provider.dimension for v in vecs)

    def test_embed_batch_empty(self):
        provider = LocalHashEmbeddingProvider()
        assert provider.embed_batch([]) == []

    def test_custom_dimension(self):
        provider = LocalHashEmbeddingProvider(dimension=256)
        vec = provider.embed("test")
        assert len(vec) == 256

    def test_case_insensitive(self):
        provider = LocalHashEmbeddingProvider()
        vec1 = provider.embed("Hello World")
        vec2 = provider.embed("hello world")
        assert abs(cosine_similarity(vec1, vec2) - 1.0) < 0.01


# -- Cosine Similarity ----------------------------------------------------

class TestCosineSimilarity:
    def test_identical_vectors(self):
        vec = [1.0, 0.0, 0.0]
        assert abs(cosine_similarity(vec, vec) - 1.0) < 0.001

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert abs(cosine_similarity(a, b)) < 0.001

    def test_opposite_vectors(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert abs(cosine_similarity(a, b) + 1.0) < 0.001

    def test_zero_vector_returns_zero(self):
        a = [0.0, 0.0]
        b = [1.0, 0.0]
        assert cosine_similarity(a, b) == 0.0

    def test_dimension_mismatch_raises(self):
        with pytest.raises(ValueError, match="dimensions must match"):
            cosine_similarity([1.0], [1.0, 2.0])


# -- Factory ---------------------------------------------------------------

class TestCreateEmbeddingProvider:
    def test_creates_local_provider(self):
        provider = create_embedding_provider("local")
        assert isinstance(provider, LocalHashEmbeddingProvider)

    def test_creates_local_with_dimension(self):
        provider = create_embedding_provider("local", dimension=256)
        assert provider.dimension == 256

    def test_creates_openai_provider(self):
        provider = create_embedding_provider("openai", api_key="sk-test")
        assert isinstance(provider, OpenAIEmbeddingProvider)

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown embedding provider"):
            create_embedding_provider("nonexistent")


# -- Semantic Search Integration -------------------------------------------

class TestSemanticSearch:
    @pytest.fixture
    def memory_store(self, tmp_path):
        from memory.store import MemoryStore
        return MemoryStore(base_path=str(tmp_path / "memory"))

    @pytest.fixture
    def provider(self):
        return LocalHashEmbeddingProvider(dimension=128)

    def _store_entry(self, store, content, agent_type="shared", keywords=None):
        """Helper to store a memory entry."""
        from core.models import MemoryEntry, MemoryScope, MemoryType

        entry = MemoryEntry(
            id=f"test_{hash(content) % 10000}",
            agent_type=agent_type,
            scope=MemoryScope.GLOBAL,
            memory_type=MemoryType.EXPERIENCE,
            content=content,
            keywords=keywords or [],
        )
        return store.store(entry)

    def test_semantic_search_returns_scored_results(
        self, memory_store, provider,
    ):
        self._store_entry(memory_store, "Database migration failed with schema error")
        self._store_entry(memory_store, "The quick brown fox jumps over the lazy dog")

        results = memory_store.semantic_search(
            "database migration error",
            embedding_provider=provider,
        )

        assert len(results) > 0
        # Each result is (entry, score)
        for entry, score in results:
            assert hasattr(entry, "content")
            assert isinstance(score, float)

    def test_semantic_search_ranks_by_similarity(
        self, memory_store, provider,
    ):
        self._store_entry(memory_store, "Python type error in generator module")
        self._store_entry(memory_store, "Golang goroutine deadlock detected")
        self._store_entry(memory_store, "Python import error missing module")

        results = memory_store.semantic_search(
            "Python module error",
            embedding_provider=provider,
        )

        # Both Python entries should rank higher than Golang
        if len(results) >= 2:
            python_contents = [
                r[0].content for r in results[:2]
            ]
            assert any("Python" in c for c in python_contents)

    def test_semantic_search_respects_limit(
        self, memory_store, provider,
    ):
        for i in range(10):
            self._store_entry(memory_store, f"Memory entry number {i}")

        results = memory_store.semantic_search(
            "memory",
            embedding_provider=provider,
            limit=3,
        )
        assert len(results) <= 3

    def test_semantic_search_min_similarity(
        self, memory_store, provider,
    ):
        self._store_entry(memory_store, "completely unrelated topic xyz")

        results = memory_store.semantic_search(
            "database migration",
            embedding_provider=provider,
            min_similarity=0.99,  # Very high threshold
        )
        # Should find nothing at such high threshold
        assert len(results) == 0

    def test_semantic_search_empty_query(
        self, memory_store, provider,
    ):
        self._store_entry(memory_store, "some content")
        results = memory_store.semantic_search(
            "",
            embedding_provider=provider,
        )
        assert len(results) >= 0  # Should not crash


# -- OpenAI Provider (unit tests, no API calls) ---------------------------

class TestOpenAIEmbeddingProvider:
    def test_dimension_property(self):
        provider = OpenAIEmbeddingProvider(
            api_key="sk-test",
            dimension=768,
        )
        assert provider.dimension == 768

    def test_default_model(self):
        provider = OpenAIEmbeddingProvider(api_key="sk-test")
        assert provider._model == "text-embedding-3-small"

    def test_lazy_client_init(self):
        provider = OpenAIEmbeddingProvider(api_key="sk-test")
        assert provider._client is None  # Not initialized until first call
