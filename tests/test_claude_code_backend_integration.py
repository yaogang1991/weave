"""Integration tests for M4.1 ClaudeCodeBackend with BackendRegistry."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.backend_models import BackendContext, BackendStatus
from core.dag_models import DAGNode
from agent.backends.registry import BackendRegistry


def _make_node(**kwargs) -> DAGNode:
    defaults = {
        "id": "node_1",
        "agent_type": "generator",
        "task_description": "Build a REST API",
    }
    defaults.update(kwargs)
    return DAGNode(**defaults)


def _make_pool_mock() -> MagicMock:
    pool = MagicMock()
    executor = AsyncMock(return_value={
        "summary": "builtin result",
        "artifacts": ["main.py"],
        "output": "done",
    })
    pool.get_executor = MagicMock(return_value=executor)
    return pool


class TestClaudeCodeBackendRegistryIntegration:
    @pytest.mark.asyncio
    async def test_register_and_execute_via_registry(self):
        from agent.backends.claude_code import (
            ClaudeCodeBackend,
            ClaudeCodeRuntimeConfig,
        )

        pool = _make_pool_mock()
        registry = BackendRegistry(pool=pool, session_id="s1")
        config = ClaudeCodeRuntimeConfig()
        backend = ClaudeCodeBackend(config=config)
        registry.register("claude_code", backend)

        node = _make_node()
        ctx = BackendContext(node=node, session_id="s1", workspace_path="/tmp")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({
            "result": "API built successfully",
            "is_error": False,
            "usage": {"input_tokens": 500, "output_tokens": 200},
            "session_id": "cc_sess_1",
        })

        with patch("shutil.which", return_value="/usr/bin/claude"):
            with patch("subprocess.run", return_value=mock_result):
                result = await registry.execute_for_node("claude_code", ctx)

        assert result.status == BackendStatus.COMPLETED
        assert "API built" in result.summary
        assert result.metadata["token_usage"]["input_tokens"] == 500

    @pytest.mark.asyncio
    async def test_fallback_to_builtin_when_unhealthy(self):
        from agent.backends.claude_code import (
            ClaudeCodeBackend,
            ClaudeCodeRuntimeConfig,
        )

        pool = _make_pool_mock()
        registry = BackendRegistry(pool=pool, session_id="s1")
        config = ClaudeCodeRuntimeConfig(cli_path="/nonexistent/claude")
        backend = ClaudeCodeBackend(config=config)
        backend._sdk_available = False
        registry.register("claude_code", backend)

        node = _make_node()
        ctx = BackendContext(node=node, session_id="s1")

        with patch("shutil.which", return_value=None):
            result = await registry.execute_for_node("claude_code", ctx)

        assert result.status == BackendStatus.COMPLETED
        assert result.summary == "builtin result"

    @pytest.mark.asyncio
    async def test_unknown_backend_falls_back_to_builtin(self):
        pool = _make_pool_mock()
        registry = BackendRegistry(pool=pool, session_id="s1")

        node = _make_node()
        ctx = BackendContext(node=node, session_id="s1")

        result = await registry.execute_for_node("nonexistent", ctx)
        assert result.status == BackendStatus.COMPLETED


class TestClaudeCodeBackendEndToEnd:
    @pytest.mark.asyncio
    async def test_full_execute_with_cli_success(self):
        from agent.backends.claude_code import (
            ClaudeCodeBackend,
            ClaudeCodeRuntimeConfig,
        )

        config = ClaudeCodeRuntimeConfig()
        backend = ClaudeCodeBackend(config=config)
        backend._sdk_available = False

        node = _make_node(agent_type="generator", task="Create hello.py")
        ctx = BackendContext(
            node=node,
            session_id="integration_test",
            workspace_path="/tmp/test_project",
        )

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({
            "result": "Created hello.py with basic Flask app",
            "is_error": False,
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 500,
                "cache_read_input_tokens": 200,
            },
            "session_id": "cc_integration",
        })

        git_result = MagicMock()
        git_result.returncode = 0
        git_result.stdout = "hello.py\nrequirements.txt\n"

        with patch("shutil.which", return_value="/usr/bin/claude"):
            with patch("subprocess.run", side_effect=[mock_result, git_result]):
                result = await backend.execute(ctx)

        assert result.status == BackendStatus.COMPLETED
        assert result.artifacts == ["hello.py", "requirements.txt"]
        assert result.metadata["token_usage"]["input_tokens"] == 1000
        assert result.metadata["token_usage"]["output_tokens"] == 500
        assert result.output == "Created hello.py with basic Flask app"

        d = result.to_dict()
        assert d["token_usage"]["input_tokens"] == 1000
