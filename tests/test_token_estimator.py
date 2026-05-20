"""Tests for M4.6 Phase 1: Token Estimation Layer."""
import asyncio
import time

import pytest
from unittest.mock import AsyncMock, MagicMock

from core.config import TokenEstimationConfig
from core.dag_models import DAGNode
from core.token_estimator import (
    NodeTokenContext,
    TokenEstimator,
    build_node_context,
)


def _ctx(task="test task", system="system", deps=None, tools=None):
    return NodeTokenContext(
        system_prompt=system,
        task_description=task,
        dependency_artifacts=deps or [],
        tools=tools or [],
        agent_type="generator",
    )


def _estimator(client=None, **kw):
    cfg = TokenEstimationConfig(**kw)
    return TokenEstimator(cfg, client=client)


class TestHeuristicEstimation:
    def test_basic(self):
        est = _estimator()
        ctx = _ctx(task="a" * 100)
        result = asyncio.get_event_loop().run_until_complete(
            est.estimate_node_tokens("n1", ctx),
        )
        assert result.estimation_method == "heuristic"
        assert result.estimated_tokens > 0
        assert not result.cached

    def test_breakdown(self):
        est = _estimator()
        ctx = _ctx(task="hello world")
        result = asyncio.get_event_loop().run_until_complete(
            est.estimate_node_tokens("n1", ctx),
        )
        assert "system" in result.breakdown
        assert "task" in result.breakdown

    def test_empty_context(self):
        est = _estimator()
        ctx = NodeTokenContext()
        result = asyncio.get_event_loop().run_until_complete(
            est.estimate_node_tokens("n1", ctx),
        )
        assert result.estimated_tokens == 0


class TestNoClientHeuristicOnly:
    def test_client_none_uses_heuristic(self):
        est = _estimator(client=None)
        ctx = _ctx(task="test")
        result = asyncio.get_event_loop().run_until_complete(
            est.estimate_node_tokens("n1", ctx),
        )
        assert result.estimation_method == "heuristic"


class TestAPIEstimation:
    def _mock_client(self, input_tokens=500):
        mock_result = MagicMock(input_tokens=input_tokens)
        client = MagicMock()
        client.messages.count_tokens = AsyncMock(return_value=mock_result)
        return client

    def test_api_success(self):
        client = self._mock_client(input_tokens=500)
        est = _estimator(client=client)
        ctx = _ctx(task="read main.py")
        result = asyncio.get_event_loop().run_until_complete(
            est.estimate_node_tokens("n1", ctx),
        )
        assert result.estimation_method == "api"
        assert result.estimated_tokens == 500

    def test_api_failure_fallback(self):
        client = MagicMock()
        client.messages.count_tokens = AsyncMock(side_effect=Exception("rate limit"))
        est = _estimator(client=client)
        ctx = _ctx(task="test")
        result = asyncio.get_event_loop().run_until_complete(
            est.estimate_node_tokens("n1", ctx),
        )
        assert result.estimation_method == "heuristic"

    def test_api_failure_no_fallback_raises(self):
        client = MagicMock()
        client.messages.count_tokens = AsyncMock(side_effect=Exception("fail"))
        est = _estimator(client=client, fallback_to_heuristic=False)
        ctx = _ctx(task="test")
        with pytest.raises(Exception, match="fail"):
            asyncio.get_event_loop().run_until_complete(
                est.estimate_node_tokens("n1", ctx),
            )

    def test_api_disabled_uses_heuristic(self):
        client = self._mock_client()
        est = _estimator(client=client, enabled=False)
        ctx = _ctx(task="test")
        result = asyncio.get_event_loop().run_until_complete(
            est.estimate_node_tokens("n1", ctx),
        )
        assert result.estimation_method == "heuristic"


class TestCaching:
    def test_cache_hit(self):
        client = MagicMock()
        mock_result = MagicMock(input_tokens=100)
        client.messages.count_tokens = AsyncMock(return_value=mock_result)
        est = _estimator(client=client)
        ctx = _ctx(task="same task")
        loop = asyncio.get_event_loop()
        r1 = loop.run_until_complete(est.estimate_node_tokens("n1", ctx))
        r2 = loop.run_until_complete(est.estimate_node_tokens("n1", ctx))
        assert not r1.cached
        assert r2.cached
        assert client.messages.count_tokens.call_count == 1

    def test_cache_expiry(self):
        client = MagicMock()
        mock_result = MagicMock(input_tokens=100)
        client.messages.count_tokens = AsyncMock(return_value=mock_result)
        est = _estimator(client=client, cache_ttl_seconds=0)
        ctx = _ctx(task="same task")
        loop = asyncio.get_event_loop()
        loop.run_until_complete(est.estimate_node_tokens("n1", ctx))
        time.sleep(0.1)
        r2 = loop.run_until_complete(est.estimate_node_tokens("n1", ctx))
        assert not r2.cached
        assert client.messages.count_tokens.call_count == 2


class TestBatchEstimation:
    def test_batch_all_succeed(self):
        client = MagicMock()
        mock_result = MagicMock(input_tokens=100)
        client.messages.count_tokens = AsyncMock(return_value=mock_result)
        est = _estimator(client=client)
        nodes = [(f"n{i}", _ctx(task=f"task {i}")) for i in range(5)]
        results = asyncio.get_event_loop().run_until_complete(
            est.estimate_nodes_batch(nodes),
        )
        assert len(results) == 5
        assert all(r.estimation_method == "api" for r in results)

    def test_batch_partial_failure(self):
        call_count = 0

        def side_effect(**kw):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise Exception("transient")
            return MagicMock(input_tokens=100)

        client = MagicMock()
        client.messages.count_tokens = AsyncMock(side_effect=side_effect)
        est = _estimator(client=client)
        nodes = [(f"n{i}", _ctx(task=f"task {i}")) for i in range(3)]
        results = asyncio.get_event_loop().run_until_complete(
            est.estimate_nodes_batch(nodes),
        )
        assert len(results) == 3
        methods = [r.estimation_method for r in results]
        assert "heuristic" in methods


class TestBuildNodeContext:
    def test_basic(self):
        node = DAGNode(
            id="gen1", agent_type="generator",
            task_description="Implement auth module",
        )
        ctx = build_node_context(
            node,
            agent_prompts={"generator": "You are a generator."},
            tool_definitions=[{"name": "read"}],
            dependency_file_paths=["src/auth.py"],
        )
        assert ctx.system_prompt == "You are a generator."
        assert ctx.task_description == "Implement auth module"
        assert ctx.dependency_artifacts == ["src/auth.py"]
        assert ctx.tools == [{"name": "read"}]
        assert ctx.agent_type == "generator"

    def test_unknown_agent_type(self):
        node = DAGNode(
            id="x", agent_type="custom",
            task_description="do stuff",
        )
        ctx = build_node_context(node, agent_prompts={})
        assert ctx.system_prompt == ""
