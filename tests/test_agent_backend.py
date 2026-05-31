"""Tests for M4.0 AgentBackend abstraction layer."""
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.backend_models import BackendContext, BackendResult, BackendStatus
from core.dag_models import DAGNode, DAGNodeModel
from agent.backends.base import AgentBackend
from agent.backends.builtin import BuiltinBackend
from agent.backends.registry import BackendRegistry


# -- BackendModels tests --


class TestBackendResult:
    def test_defaults(self):
        r = BackendResult()
        assert r.status == BackendStatus.COMPLETED
        assert r.summary == ""
        assert r.artifacts == []
        assert r.output == ""
        assert r.error == ""

    def test_to_dict_format(self):
        r = BackendResult(
            status=BackendStatus.COMPLETED,
            summary="done",
            artifacts=["a.py", "b.py"],
            output="ok",
        )
        d = r.to_dict()
        assert d == {
            "status": "completed",
            "summary": "done",
            "artifacts": ["a.py", "b.py"],
            "output": "ok",
        }

    def test_failed_result(self):
        r = BackendResult(
            status=BackendStatus.FAILED,
            error="something broke",
        )
        assert r.status == BackendStatus.FAILED
        assert r.error == "something broke"


class TestBackendContext:
    def test_basic_context(self):
        node = MagicMock()
        ctx = BackendContext(node=node, session_id="s1")
        assert ctx.node is node
        assert ctx.session_id == "s1"
        assert ctx.workspace_path is None
        assert ctx.cancel_event is None

    def test_context_with_all_fields(self):
        node = MagicMock()
        event = threading.Event()
        callback = MagicMock()
        ctx = BackendContext(
            node=node,
            session_id="s1",
            workspace_path="/tmp/ws",
            job_id="j1",
            run_id="r1",
            cancel_event=event,
            progress_callback=callback,
        )
        assert ctx.workspace_path == "/tmp/ws"
        assert ctx.cancel_event is event
        assert ctx.progress_callback is callback


# -- DAGNode backend field tests --


class TestDAGNodeBackend:
    def test_default_backend(self):
        node = DAGNode(
            id="n1",
            agent_type="generator",
            task_description="test",
        )
        assert node.backend is None

    def test_custom_backend(self):
        node = DAGNode(
            id="n1",
            agent_type="generator",
            task_description="test",
            backend="external",
        )
        assert node.backend == "external"

    def test_explicit_builtin(self):
        node = DAGNode(
            id="n1",
            agent_type="generator",
            task_description="test",
            backend="builtin",
        )
        assert node.backend == "builtin"


class TestDAGNodeModelBackend:
    def test_default_backend(self):
        m = DAGNodeModel(
            id="n1",
            agent_type="generator",
            task_description="test",
        )
        assert m.backend is None

    def test_custom_backend(self):
        m = DAGNodeModel(
            id="n1",
            agent_type="generator",
            task_description="test",
            backend="claude_code",
        )
        assert m.backend == "claude_code"


# -- BuiltinBackend tests --


class TestBuiltinBackend:
    def _make_pool(self, return_value=None):
        closure = AsyncMock(return_value=return_value or {
            "status": "completed",
            "summary": "test done",
            "artifacts": ["a.py"],
            "output": "ok",
        })
        pool = MagicMock()
        pool.get_executor.return_value = closure
        return pool, closure

    @pytest.mark.asyncio
    async def test_execute_returns_result(self):
        pool, closure = self._make_pool()
        backend = BuiltinBackend(pool=pool, session_id="s1")

        node = DAGNode(id="n1", agent_type="generator", task_description="test")
        ctx = BackendContext(node=node, artifacts=[])

        result = await backend.execute(ctx)
        assert result.status == BackendStatus.COMPLETED
        assert result.summary == "test done"
        assert result.artifacts == ["a.py"]
        assert result.output == "ok"

    @pytest.mark.asyncio
    async def test_execute_passes_context_to_closure(self):
        pool, closure = self._make_pool()
        backend = BuiltinBackend(pool=pool, session_id="s1")

        node = DAGNode(id="n1", agent_type="generator", task_description="test")
        event = threading.Event()
        callback = MagicMock()
        ctx = BackendContext(
            node=node,
            artifacts=[],
            cancel_event=event,
            progress_callback=callback,
            workspace_path="/tmp/ws",
        )

        await backend.execute(ctx)
        closure.assert_called_once_with(
            node,
            [],
            cancel_event=event,
            progress_callback=callback,
            workspace_path="/tmp/ws",
        )

    @pytest.mark.asyncio
    async def test_execute_reraises_exceptions(self):
        from core.exceptions import PendingApprovalError
        pool, closure = self._make_pool()
        closure.side_effect = PendingApprovalError("need approval")
        backend = BuiltinBackend(pool=pool, session_id="s1")

        node = DAGNode(id="n1", agent_type="generator", task_description="test")
        ctx = BackendContext(node=node, artifacts=[])

        with pytest.raises(PendingApprovalError):
            await backend.execute(ctx)

    @pytest.mark.asyncio
    async def test_health_check_always_true(self):
        pool, _ = self._make_pool()
        backend = BuiltinBackend(pool=pool, session_id="s1")
        assert await backend.health_check() is True

    def test_get_capabilities_returns_empty(self):
        pool, _ = self._make_pool()
        backend = BuiltinBackend(pool=pool, session_id="s1")
        assert backend.get_capabilities() == []

    def test_name_property(self):
        pool, _ = self._make_pool()
        backend = BuiltinBackend(pool=pool, session_id="s1")
        assert backend.name == "builtin"

    def test_lazy_closure_creation(self):
        pool, closure = self._make_pool()
        backend = BuiltinBackend(pool=pool, session_id="s1")
        pool.get_executor.assert_not_called()
        backend._ensure_closure()
        pool.get_executor.assert_called_once_with("s1")
        backend._ensure_closure()
        pool.get_executor.assert_called_once()


# -- BackendRegistry tests --


class TestBackendRegistry:
    def _make_pool(self):
        closure = AsyncMock(return_value={
            "status": "completed",
            "summary": "ok",
            "artifacts": [],
            "output": "",
        })
        pool = MagicMock()
        pool.get_executor.return_value = closure
        return pool

    def test_builtin_always_registered(self):
        registry = BackendRegistry.from_pool(pool=self._make_pool(), session_id="s1")
        backend = registry.get_backend("builtin")
        assert isinstance(backend, BuiltinBackend)

    def test_fallback_on_missing_backend(self):
        registry = BackendRegistry.from_pool(pool=self._make_pool(), session_id="s1")
        backend = registry.get_backend("nonexistent")
        assert isinstance(backend, BuiltinBackend)

    def test_register_and_get_backend(self):
        registry = BackendRegistry.from_pool(pool=self._make_pool(), session_id="s1")
        mock_backend = MagicMock(spec=AgentBackend)
        mock_backend.health_check = AsyncMock(return_value=True)
        registry.register("external", mock_backend)
        assert registry.get_backend("external") is mock_backend

    @pytest.mark.asyncio
    async def test_execute_for_node_builtin(self):
        pool = self._make_pool()
        registry = BackendRegistry.from_pool(pool=pool, session_id="s1")
        node = DAGNode(id="n1", agent_type="generator", task_description="test")
        ctx = BackendContext(node=node, artifacts=[])

        result = await registry.execute_for_node("builtin", ctx)
        assert result.status == BackendStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_execute_for_node_external_healthy(self):
        pool = self._make_pool()
        registry = BackendRegistry.from_pool(pool=pool, session_id="s1")

        mock_backend = MagicMock(spec=AgentBackend)
        mock_backend.health_check = AsyncMock(return_value=True)
        mock_backend.execute = AsyncMock(return_value=BackendResult(
            status=BackendStatus.COMPLETED,
            summary="external done",
        ))
        registry.register("external", mock_backend)

        node = DAGNode(id="n1", agent_type="generator", task_description="test")
        ctx = BackendContext(node=node, artifacts=[])

        result = await registry.execute_for_node("external", ctx)
        assert result.status == BackendStatus.COMPLETED
        assert result.summary == "external done"

    @pytest.mark.asyncio
    async def test_execute_for_node_external_unhealthy(self):
        pool = self._make_pool()
        registry = BackendRegistry.from_pool(pool=pool, session_id="s1")

        mock_backend = MagicMock(spec=AgentBackend)
        mock_backend.health_check = AsyncMock(return_value=False)
        registry.register("external", mock_backend)

        node = DAGNode(id="n1", agent_type="generator", task_description="test")
        ctx = BackendContext(node=node, artifacts=[])

        result = await registry.execute_for_node("external", ctx)
        assert result.status == BackendStatus.COMPLETED
        mock_backend.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_for_node_health_check_exception(self):
        pool = self._make_pool()
        registry = BackendRegistry.from_pool(pool=pool, session_id="s1")

        mock_backend = MagicMock(spec=AgentBackend)
        mock_backend.health_check = AsyncMock(side_effect=ConnectionError("unreachable"))
        registry.register("external", mock_backend)

        node = DAGNode(id="n1", agent_type="generator", task_description="test")
        ctx = BackendContext(node=node, artifacts=[])

        result = await registry.execute_for_node("external", ctx)
        assert result.status == BackendStatus.COMPLETED


# -- AgentBackend ABC tests --


class TestAgentBackendABC:
    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            AgentBackend()

    def test_subclass_must_implement_all(self):
        class Partial(AgentBackend):
            async def execute(self, context):
                pass

        with pytest.raises(TypeError):
            Partial()

    def test_complete_subclass(self):
        class Complete(AgentBackend):
            async def execute(self, context):
                return BackendResult()

            async def health_check(self):
                return True

            def get_capabilities(self):
                return ["generator"]

        backend = Complete()
        assert backend.name == "Complete"
        assert backend.get_capabilities() == ["generator"]
