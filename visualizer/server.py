"""
Harness Visualizer Server: FastAPI + WebSocket for real-time DAG monitoring.

Endpoints:
    GET  /                  → Static dashboard (HTML)
    WS   /ws                → Real-time execution events
    GET  /api/sessions      → List all sessions
    GET  /api/sessions/{id} → Get session events & DAG
    GET  /api/plans         → List saved execution plans
    GET  /api/health        → Health check
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure project root is on path when running server directly
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

from visualizer.event_bridge import WebSocketEventBridge
from session.store import SessionStore
from core.config import HarnessConfig


app = FastAPI(title="Harness Visualizer", version="2.0")
bridge = WebSocketEventBridge()

# Static files
static_path = Path(__file__).parent / "static"
if static_path.exists():
    app.mount("/static", StaticFiles(directory=static_path), name="static")


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the main monitoring dashboard."""
    index_file = static_path / "index.html"
    if index_file.exists():
        return HTMLResponse(content=index_file.read_text(), status_code=200)
    return HTMLResponse(content="<h1>Harness Visualizer</h1><p>Dashboard not built yet.</p>")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time event streaming."""
    await websocket.accept()
    await bridge.connect(websocket)
    try:
        while True:
            # Keep connection alive and handle client commands
            data = await websocket.receive_json()
            await _handle_client_command(websocket, data)
    except WebSocketDisconnect:
        await bridge.disconnect(websocket)
    except Exception:
        await bridge.disconnect(websocket)


async def _handle_client_command(websocket: WebSocket, data: dict) -> None:
    """Handle commands from WebSocket clients."""
    cmd = data.get("command")
    
    if cmd == "list_sessions":
        sessions = _list_sessions()
        await websocket.send_json({"type": "sessions_list", "sessions": sessions})
    
    elif cmd == "get_session":
        session_id = data.get("session_id")
        session_data = _get_session_data(session_id)
        await websocket.send_json({"type": "session_data", **session_data})
    
    elif cmd == "list_plans":
        plans = _list_plans()
        await websocket.send_json({"type": "plans_list", "plans": plans})
    
    elif cmd == "ping":
        await websocket.send_json({"type": "pong"})


@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/sessions")
async def api_list_sessions():
    return {"sessions": _list_sessions()}


@app.get("/api/sessions/{session_id}")
async def api_get_session(session_id: str):
    return _get_session_data(session_id)


@app.get("/api/plans")
async def api_list_plans():
    return {"plans": _list_plans()}


def _list_sessions() -> list[dict]:
    """List all sessions from the event store."""
    config = HarnessConfig.from_env()
    store = SessionStore(config.event_store_path)
    sessions = []
    
    for session_id in store.list_sessions():
        events = store.get_events(session_id)
        if not events:
            continue
        
        start_event = events[0]
        end_event = events[-1] if events else None
        
        # Count events by type
        node_events = [e for e in events if e.type.value.startswith("workflow.")]
        tool_calls = sum(1 for e in events if e.type.value == "agent.tool_use")
        
        sessions.append({
            "session_id": session_id,
            "created_at": start_event.timestamp.isoformat() if start_event.timestamp else None,
            "updated_at": end_event.timestamp.isoformat() if end_event and end_event.timestamp else None,
            "event_count": len(events),
            "tool_calls": tool_calls,
            "workflow_stages": len(node_events),
        })
    
    # Sort by created_at desc
    sessions.sort(key=lambda x: x["created_at"] or "", reverse=True)
    return sessions


def _get_session_data(session_id: str) -> dict:
    """Get full session data including events and DAG info."""
    config = HarnessConfig.from_env()
    store = SessionStore(config.event_store_path)
    
    events = store.get_events(session_id)
    events_data = []
    
    for event in events:
        events_data.append({
            "id": event.id,
            "timestamp": event.timestamp.isoformat() if event.timestamp else None,
            "type": event.type.value,
            "payload": event.payload,
        })
    
    # Try to reconstruct DAG from events
    dag_data = _reconstruct_dag_from_events(events_data)
    
    return {
        "session_id": session_id,
        "events": events_data,
        "dag": dag_data,
        "event_count": len(events_data),
    }


def _reconstruct_dag_from_events(events: list[dict]) -> dict | None:
    """
    Attempt to reconstruct a DAG from session events.
    Looks for DAG structure in plan files or session payloads.
    """
    # Look for plan references in events
    for event in events:
        payload = event.get("payload", {})
        if isinstance(payload, dict):
            # Check if payload contains DAG-like data
            if "nodes" in payload and "edges" in payload:
                return payload
    
    return None


def _list_plans() -> list[dict]:
    """List all saved execution plans."""
    plans_dir = Path("./data/plans")
    if not plans_dir.exists():
        return []
    
    plans = []
    for plan_file in sorted(plans_dir.glob("plan_*.json"), reverse=True):
        try:
            data = json.loads(plan_file.read_text())
            plans.append({
                "file": str(plan_file),
                "reasoning": data.get("reasoning", "")[:100],
                "node_count": len(data.get("nodes", [])),
                "levels": data.get("levels", []),
            })
        except Exception:
            continue
    
    return plans


# ── Integration helpers ──────────────────────────────────────────────

def get_event_bridge() -> WebSocketEventBridge:
    """Get the global WebSocket event bridge instance."""
    return bridge


async def run_server(host: str = "0.0.0.0", port: int = 8080) -> None:
    """Run the visualizer server (programmatic entry point)."""
    import uvicorn
    await uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="info")
    ).serve()
