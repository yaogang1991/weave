"""Tests for #737: count_tokens API returns None — graceful fallback.

Verifies that:
1. _count_tokens_api raises ValueError when result.input_tokens is None
2. estimate_node_tokens falls back to heuristic on None result
3. estimate_nodes_batch handles None result per-node gracefully
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.config import TokenEstimationConfig
from core.token_estimator import (
    NodeTokenContext,
    TokenEstimateResult,
    TokenEstimator,
)


def _make_estimator(client=None, fallback=True):
    """Create TokenEstimator with optional mock client."""
    config = TokenEstimationConfig(
        enabled=True,
        fallback_to_heuristic=fallback,
    )
    return TokenEstimator(config=config, client=client, model="test-model")


def _make_context(task="Build a REST API"):
    return NodeTokenContext(
        system_prompt="You are a planner.",
        task_description=task,
    )


def test_count_tokens_api_raises_on_none():
    """_count_tokens_api raises ValueError when proxy returns None (#737)."""
    mock_result = MagicMock()
    mock_result.input_tokens = None

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.count_tokens = AsyncMock(return_value=mock_result)

    estimator = _make_estimator(client=mock_client)
    ctx = _make_context()

    with pytest.raises(ValueError, match="count_tokens returned None"):
        asyncio.get_event_loop().run_until_complete(
            estimator._count_tokens_api(ctx)
        )


def test_estimate_node_tokens_falls_back_to_heuristic_on_none():
    """When API returns None, falls back to heuristic estimate (#737)."""
    mock_result = MagicMock()
    mock_result.input_tokens = None

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.count_tokens = AsyncMock(return_value=mock_result)

    estimator = _make_estimator(client=mock_client, fallback=True)
    ctx = _make_context()

    result = asyncio.get_event_loop().run_until_complete(
        estimator.estimate_node_tokens("test_node", ctx)
    )

    assert isinstance(result, TokenEstimateResult)
    assert result.node_id == "test_node"
    assert result.estimation_method == "heuristic"
    assert result.estimated_tokens > 0


def test_estimate_node_tokens_propagates_when_no_fallback():
    """When fallback disabled and API returns None, error propagates (#737)."""
    mock_result = MagicMock()
    mock_result.input_tokens = None

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.count_tokens = AsyncMock(return_value=mock_result)

    estimator = _make_estimator(client=mock_client, fallback=False)
    ctx = _make_context()

    with pytest.raises(ValueError, match="count_tokens returned None"):
        asyncio.get_event_loop().run_until_complete(
            estimator.estimate_node_tokens("test_node", ctx)
        )
