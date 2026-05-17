"""
Embedding provider abstraction for semantic memory retrieval (#508 P1).

Defines the EmbeddingProvider interface and concrete implementations:
- OpenAIEmbeddingProvider: uses OpenAI embeddings API
- LocalHashEmbeddingProvider: zero-dependency fallback using character n-gram hashing

Usage::

    provider = OpenAIEmbeddingProvider(api_key="sk-...", model="text-embedding-3-small")
    vector = provider.embed("database migration failed")
    # vector: [0.012, -0.034, 0.056, ...]

    vectors = provider.embed_batch(["hello", "world"])
    # vectors: [[...], [...]]

The LocalHashEmbeddingProvider requires no external dependencies and provides
a basic but functional embedding space for development and testing.
"""

from __future__ import annotations

import hashlib
import logging
import math
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)

# Standard embedding dimension for the default hash provider
DEFAULT_EMBEDDING_DIM = 128


class EmbeddingProvider(ABC):
    """Abstract interface for text embedding providers (#508 P1).

    Implementations convert text strings into fixed-length float vectors
    for semantic similarity comparison via cosine distance.
    """

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Embed a single text string into a vector.

        Args:
            text: Input text to embed.

        Returns:
            Fixed-length list of floats.
        """
        ...

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple text strings into vectors.

        Args:
            texts: List of input texts.

        Returns:
            List of vectors, one per input text.
        """
        ...

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Dimension of the embedding vectors."""
        ...


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """Embedding provider using OpenAI's embeddings API (#508 P1).

    Requires `openai` package and a valid API key.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "text-embedding-3-small",
        base_url: str | None = None,
        dimension: int = 1536,
    ) -> None:
        self._model = model
        self._dimension = dimension
        self._api_key = api_key
        self._base_url = base_url
        self._client: Any = None

    def _get_client(self) -> Any:
        """Lazy-initialize the OpenAI client."""
        if self._client is None:
            from openai import OpenAI

            kwargs: dict[str, Any] = {}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = OpenAI(**kwargs)
        return self._client

    def embed(self, text: str) -> list[float]:
        """Embed a single text using OpenAI API."""
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts using OpenAI batch API."""
        if not texts:
            return []

        client = self._get_client()
        response = client.embeddings.create(
            input=texts,
            model=self._model,
            dimensions=self._dimension,
        )

        # Sort by index to maintain order
        results = sorted(response.data, key=lambda d: d.index)
        return [d.embedding for d in results]

    @property
    def dimension(self) -> int:
        return self._dimension


class LocalHashEmbeddingProvider(EmbeddingProvider):
    """Zero-dependency embedding provider using character n-gram hashing (#508 P1).

    Uses 3-gram hashing to produce a fixed-dimension vector. This is NOT
    a semantic embedding — it captures character-level similarity only.
    Suitable for development, testing, and environments without API access.

    For production semantic search, use OpenAIEmbeddingProvider instead.
    """

    def __init__(self, dimension: int = DEFAULT_EMBEDDING_DIM) -> None:
        self._dimension = dimension

    def embed(self, text: str) -> list[float]:
        """Embed text using character n-gram hashing."""
        vector = [0.0] * self._dimension

        if not text.strip():
            return vector

        # Generate 3-grams and hash each into the vector
        normalized = text.lower().strip()
        for n in (2, 3, 4):
            for i in range(len(normalized) - n + 1):
                gram = normalized[i:i + n]
                h = int(hashlib.md5(gram.encode()).hexdigest(), 16)
                idx = h % self._dimension
                vector[idx] += 1.0

        # L2 normalize
        norm = math.sqrt(sum(v * v for v in vector))
        if norm > 0:
            vector = [v / norm for v in vector]

        return vector

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts."""
        return [self.embed(t) for t in texts]

    @property
    def dimension(self) -> int:
        return self._dimension


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if len(a) != len(b):
        raise ValueError(
            f"Vector dimensions must match: {len(a)} != {len(b)}"
        )
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def create_embedding_provider(
    provider_type: str = "local",
    **kwargs: Any,
) -> EmbeddingProvider:
    """Factory function to create an embedding provider (#508 P1).

    Args:
        provider_type: "openai" or "local" (default).
        **kwargs: Provider-specific configuration.

    Returns:
        EmbeddingProvider instance.
    """
    if provider_type == "openai":
        return OpenAIEmbeddingProvider(**kwargs)
    if provider_type == "local":
        return LocalHashEmbeddingProvider(**kwargs)
    raise ValueError(f"Unknown embedding provider: {provider_type}")
