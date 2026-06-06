"""Tests for weave_ui/cli_renderer.py and weave_ui/event_bridge.py."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone

from core.models import DAG, DAGNode, ExecutionEvent, NodeStatus
from weave_ui.cli_renderer import CLIDAGRenderer
from weave_ui.event_bridge import WebSocketEventBridge
from datetime import datetime, timezone


class TestCLIDAGRenderer:
    @pytest.fixture
    def renderer(self):
        return CLIDAGRenderer()

    @pytest.fixture
    def sample_dag(self):
        dag = DAG(reasoning="test plan")
        dag.add_node(DAGNode(id="n1", agent_type="planner", task_description="Plan the work"))
        dag.add_node(DAGNode(id="n2", agent_type="generator", task_description="Generate code"))
        dag.add_node(DAGNode(id="n3", agent_type="evaluator", task_description="Evaluate results"))
        dag.add_edge("n1", "n2")
        dag.add_edge("n2", "n3")
        return dag

    def test_handle_started_event(self, renderer):
        event = ExecutionEvent(
            event_type="started", node_id="n1", details={}, timestamp=datetime.now(timezone.utc),
        )
        renderer.handle_event(event)
        assert renderer._node_status["n1"] == "running"
        assert renderer._event_count == 1

    def test_handle_completed_event(self, renderer):
        renderer._node_start_times["n1"] = 1000.0
        event = ExecutionEvent(
            event_type="completed", node_id="n1", details={}, timestamp=datetime.now(timezone.utc),
        )
        renderer.handle_event(event)
        assert renderer._node_status["n1"] == "success"

    def test_handle_failed_event(self, renderer):
        event = ExecutionEvent(
            event_type="failed", node_id="n1", details={"reason": "timeout"}, timestamp=datetime.now(timezone.utc),
        )
        renderer.handle_event(event)
        assert renderer._node_status["n1"] == "failed"

    def test_handle_retrying_event(self, renderer):
        event = ExecutionEvent(
            event_type="retrying", node_id="n1", details={}, timestamp=datetime.now(timezone.utc),
        )
        renderer.handle_event(event)
        assert renderer._node_status["n1"] == "retrying"

    def test_handle_skipped_event(self, renderer):
        event = ExecutionEvent(
            event_type="skipped", node_id="n1", details={}, timestamp=datetime.now(timezone.utc),
        )
        renderer.handle_event(event)
        assert renderer._node_status["n1"] == "skipped"

    def test_render_dag(self, renderer, sample_dag, capsys):
        renderer.render_dag(sample_dag)
        captured = capsys.readouterr()
        assert "DAG Execution Plan" in captured.out
        assert "planner" in captured.out
        assert "generator" in captured.out

    def test_render_summary(self, renderer, sample_dag, capsys):
        for nid in sample_dag.nodes:
            sample_dag.nodes[nid].status = NodeStatus.SUCCESS
        renderer.render_summary(sample_dag)
        captured = capsys.readouterr()
        assert "Total Nodes" in captured.out
        assert "Success" in captured.out

    def test_render_live_status(self, renderer, sample_dag, capsys):
        sample_dag.nodes["n1"].status = NodeStatus.SUCCESS
        sample_dag.nodes["n1"].started_at = datetime.now(timezone.utc)
        sample_dag.nodes["n1"].completed_at = datetime.now(timezone.utc)
        renderer.render_live_status(sample_dag)
        captured = capsys.readouterr()
        assert "n1" in captured.out

    def test_get_duration_returns_none_without_start(self, renderer):
        assert renderer._get_duration("nonexistent") is None

    def test_agent_color_known_types(self, renderer):
        assert renderer._agent_color("planner") != ""
        assert renderer._agent_color("generator") != ""
        assert renderer._agent_color("evaluator") != ""

    def test_agent_color_unknown_type(self, renderer):
        color = renderer._agent_color("custom_agent")
        assert color != ""


class TestWebSocketEventBridge:
    @pytest.fixture
    def bridge(self):
        return WebSocketEventBridge()

    def test_initial_state(self, bridge):
        assert bridge._clients == []
        assert bridge.get_history() == []

    @pytest.mark.asyncio
    async def test_connect_adds_client(self, bridge):
        ws = MagicMock()
        ws.send_json = AsyncMock()
        await bridge.connect(ws)
        assert ws in bridge._clients

    @pytest.mark.asyncio
    async def test_connect_sends_history(self, bridge):
        ws = MagicMock()
        ws.send_json = AsyncMock()
        event = ExecutionEvent(
            event_type="started", node_id="n1", details={}, timestamp=datetime.now(timezone.utc),
        )
        await bridge.handle_event(event)
        await bridge.connect(ws)
        ws.send_json.assert_called_once()
        call_args = ws.send_json.call_args[0][0]
        assert call_args["type"] == "history"

    @pytest.mark.asyncio
    async def test_disconnect_removes_client(self, bridge):
        ws = MagicMock()
        ws.send_json = AsyncMock()
        bridge._clients.append(ws)
        await bridge.disconnect(ws)
        assert ws not in bridge._clients

    @pytest.mark.asyncio
    async def test_handle_event_broadcasts(self, bridge):
        ws = MagicMock()
        ws.send_json = AsyncMock()
        bridge._clients.append(ws)
        event = ExecutionEvent(
            event_type="started", node_id="n1", details={"key": "val"}, timestamp=datetime.now(timezone.utc),
        )
        await bridge.handle_event(event)
        ws.send_json.assert_called_once()
        payload = ws.send_json.call_args[0][0]
        assert payload["type"] == "execution_event"
        assert payload["node_id"] == "n1"

    @pytest.mark.asyncio
    async def test_handle_event_buffers_history(self, bridge):
        event = ExecutionEvent(
            event_type="completed", node_id="n1", details={}, timestamp=datetime.now(timezone.utc),
        )
        await bridge.handle_event(event)
        assert len(bridge.get_history()) == 1

    @pytest.mark.asyncio
    async def test_broadcast_dag(self, bridge):
        ws = MagicMock()
        ws.send_json = AsyncMock()
        bridge._clients.append(ws)
        await bridge.broadcast_dag({"nodes": ["a", "b"]})
        payload = ws.send_json.call_args[0][0]
        assert payload["type"] == "dag_update"

    @pytest.mark.asyncio
    async def test_broadcast_session_start(self, bridge):
        ws = MagicMock()
        ws.send_json = AsyncMock()
        bridge._clients.append(ws)
        await bridge.broadcast_session_start("sess1", {"nodes": []})
        payload = ws.send_json.call_args[0][0]
        assert payload["type"] == "session_start"
        assert payload["session_id"] == "sess1"

    @pytest.mark.asyncio
    async def test_broadcast_session_end(self, bridge):
        ws = MagicMock()
        ws.send_json = AsyncMock()
        bridge._clients.append(ws)
        await bridge.broadcast_session_end("sess1", {"total": 3})
        payload = ws.send_json.call_args[0][0]
        assert payload["type"] == "session_end"

    @pytest.mark.asyncio
    async def test_dead_client_removed(self, bridge):
        ws = MagicMock()
        ws.send_json = AsyncMock(side_effect=Exception("disconnected"))
        bridge._clients.append(ws)
        event = ExecutionEvent(
            event_type="started", node_id="n1", details={}, timestamp=datetime.now(timezone.utc),
        )
        await bridge.handle_event(event)
        assert ws not in bridge._clients

    def test_clear_history(self, bridge):
        bridge._history.append({"type": "test"})
        bridge.clear_history()
        assert bridge.get_history() == []
