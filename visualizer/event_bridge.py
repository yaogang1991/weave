"""
Event Bridge: Connects DAGExecutionEngine events to WebSocket clients.

Usage:
    bridge = WebSocketEventBridge()
    engine.on_event(bridge.handle_event)
    
    # In FastAPI:
    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        await bridge.connect(websocket)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from core.models import ExecutionEvent


class WebSocketEventBridge:
    """
    Bridges DAG execution events to WebSocket clients.
    
    This is a singleton-style broadcaster:
    - Any number of WebSocket clients can connect
    - Execution events are broadcast to all connected clients
    - Clients can also request historical data
    """

    def __init__(self):
        self._clients: list[Any] = []  # WebSocket objects
        self._lock = asyncio.Lock()
        self._history: list[dict] = []  # Recent events buffer
        self._max_history = 1000

    async def connect(self, websocket) -> None:
        """Register a new WebSocket client."""
        async with self._lock:
            self._clients.append(websocket)
        
        # Send recent history upon connection
        if self._history:
            await websocket.send_json({
                "type": "history",
                "events": self._history,
            })

    async def disconnect(self, websocket) -> None:
        """Remove a WebSocket client."""
        async with self._lock:
            if websocket in self._clients:
                self._clients.remove(websocket)

    async def handle_event(self, event: ExecutionEvent) -> None:
        """
        Event handler compatible with DAGExecutionEngine.on_event().
        
        Broadcasts the event to all connected WebSocket clients.
        """
        payload = {
            "type": "execution_event",
            "timestamp": event.timestamp.isoformat() if event.timestamp else None,
            "node_id": event.node_id,
            "event_type": event.event_type,
            "details": event.details,
        }
        
        # Buffer for late-joining clients
        self._history.append(payload)
        if len(self._history) > self._max_history:
            self._history.pop(0)
        
        await self._broadcast(payload)

    async def broadcast_dag(self, dag_data: dict) -> None:
        """Broadcast a DAG structure update."""
        await self._broadcast({
            "type": "dag_update",
            "dag": dag_data,
        })

    async def broadcast_session_start(self, session_id: str, dag_data: dict) -> None:
        """Broadcast session start with initial DAG."""
        await self._broadcast({
            "type": "session_start",
            "session_id": session_id,
            "dag": dag_data,
        })

    async def broadcast_session_end(self, session_id: str, summary: dict) -> None:
        """Broadcast session completion."""
        await self._broadcast({
            "type": "session_end",
            "session_id": session_id,
            "summary": summary,
        })

    async def _broadcast(self, payload: dict) -> None:
        """Send payload to all connected clients, removing dead ones."""
        dead_clients = []
        
        async with self._lock:
            clients = list(self._clients)
        
        for client in clients:
            try:
                await client.send_json(payload)
            except Exception:
                dead_clients.append(client)
        
        if dead_clients:
            async with self._lock:
                for client in dead_clients:
                    if client in self._clients:
                        self._clients.remove(client)

    def get_history(self) -> list[dict]:
        """Get buffered event history."""
        return list(self._history)

    def clear_history(self) -> None:
        """Clear event history buffer."""
        self._history.clear()
