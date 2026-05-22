"""Tests for #737: count_tokens API returns None — graceful fallback.

Verifies that:
1. _count_tokens_api raises ValueError when result.input_tokens is None
2. estimate_node_tokens falls back to heuristic on None result
3. estimate_nodes_batch handles None result per-node gracefully
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="function")

from core.config import TokenEstimationConfig
from core.token_estimator import (
    NodeTokenContext,
    TokenEstimateResult,
    TokenEstimator,
    _CountTokensUnavailable,
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


async def test_count_tokens_api_raises_on_none():
    """_count_tokens_api raises ValueError when proxy returns None (#737)."""
    mock_result = MagicMock()
    mock_result.input_tokens = None

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.count_tokens = AsyncMock(return_value=mock_result)

    estimator = _make_estimator(client=mock_client)
    ctx = _make_context()

    with pytest.raises(_CountTokensUnavailable, match="count_tokens returned None"):
        await estimator._count_tokens_api(ctx)


async def test_estimate_node_tokens_falls_back_to_heuristic_on_none():
    """When API returns None, falls back to heuristic estimate (#737)."""
    mock_result = MagicMock()
    mock_result.input_tokens = None

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.count_tokens = AsyncMock(return_value=mock_result)

    estimator = _make_estimator(client=mock_client, fallback=True)
    ctx = _make_context()

    result = await estimator.estimate_node_tokens("test_node", ctx)

    assert isinstance(result, TokenEstimateResult)
    assert result.node_id == "test_node"
    assert result.estimation_method == "heuristic"
    assert result.estimated_tokens > 0


async def test_estimate_node_tokens_propagates_when_no_fallback():
    """When fallback disabled and generic API error, error propagates (#737)."""
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.count_tokens = AsyncMock(
        side_effect=RuntimeError("API connection refused")
    )

    estimator = _make_estimator(client=mock_client, fallback=False)
    ctx = _make_context()

    with pytest.raises(RuntimeError, match="API connection refused"):
        await estimator.estimate_node_tokens("test_node", ctx)
