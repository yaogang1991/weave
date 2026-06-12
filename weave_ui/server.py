"""
Weave Visualizer Server: FastAPI + WebSocket for real-time DAG monitoring.

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
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Ensure project root is on path when running server directly
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel as PydanticModel  # noqa: E402

from weave_ui.event_bridge import WebSocketEventBridge  # noqa: E402
from session.store import SessionStore  # noqa: E402
from core.config import WeaveConfig  # noqa: E402

from control_plane.models import JobStatus  # noqa: E402
from control_plane.repository import JobRepository  # noqa: E402
from control_plane.approval import ApprovalRepository, TicketStatus  # noqa: E402


app = FastAPI(title="Weave UI", version="3.0")
bridge = WebSocketEventBridge()


# ── API Key authentication (#494) ──────────────────────────────────

_API_KEY_HEADER = "X-API-Key"
_ENV_API_KEY = "WEAVE_API_KEY"

# Paths that don't require auth (health check, static assets, websocket)
_PUBLIC_PATHS = {"/", "/api/health", "/ws", "/favicon.ico"}


@app.middleware("http")
async def api_key_auth(request: Request, call_next):
    """Require API key for non-public endpoints when WEAVE_API_KEY is set."""
    api_key = os.environ.get(_ENV_API_KEY)
    if not api_key:
        # No key configured — skip auth
        return await call_next(request)

    # Public paths don't require auth
    if request.url.path in _PUBLIC_PATHS:
        return await call_next(request)

    # Static assets
    if request.url.path.startswith("/static/"):
        return await call_next(request)

    # Check API key
    provided = request.headers.get(_API_KEY_HEADER) or request.query_params.get("api_key")
    if provided != api_key:
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing API key"},
        )

    return await call_next(request)


# ── Request body models ──────────────────────────────────────────────


class RejectRequest(PydanticModel):
    reason: str = ""


class MemoryAddRequest(PydanticModel):
    content: str
    memory_type: str = "fact"
    scope: str = "global"
    agent_type: str = "shared"
    keywords: list[str] = []


class SubmitJobRequest(PydanticModel):
    requirement: str
    workspace: str
    template: str | None = None
    priority: int = 0


class AddWorkspaceRequest(PydanticModel):
    path: str
    label: str = ""


class NotificationPrefsUpdate(PydanticModel):
    on_succeeded: bool | None = None
    on_failed: bool | None = None
    on_stuck: bool | None = None
    on_pending_approval: bool | None = None


class TaskTemplateCreate(PydanticModel):
    model_config = {"extra": "allow"}
    name: str
    description: str = ""
    category: str = "general"
    prompt: str = ""


class TaskTemplateUpdate(PydanticModel):
    model_config = {"extra": "allow"}
    name: str | None = None
    description: str | None = None
    category: str | None = None
    prompt: str | None = None


class AnnotationUpdate(PydanticModel):
    tags: list[str] | None = None
    notes: str | None = None
    rating: int | None = None


# Static files
static_path = Path(__file__).parent / "static"
if static_path.exists():
    app.mount("/static", StaticFiles(directory=static_path), name="static")


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the main monitoring dashboard."""
    index_file = static_path / "index.html"
    if index_file.exists():
        return HTMLResponse(content=index_file.read_text(encoding="utf-8"), status_code=200)
    return HTMLResponse(content="<h1>Weave Visualizer</h1><p>Dashboard not built yet.</p>")


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


@app.get("/api/sessions/{session_id}/dag")
async def api_get_session_dag(session_id: str):
    """Get DAG structure with node execution status."""
    config = WeaveConfig.from_env()
    store = SessionStore(config.event_store_path)
    events = store.get_events(session_id)
    if not events:
        raise HTTPException(status_code=404, detail="Session not found")

    events_data = []
    for event in events:
        events_data.append({
            "id": event.id,
            "timestamp": event.timestamp.isoformat() if event.timestamp else None,
            "type": event.type.value,
            "payload": event.payload,
        })

    return _build_dag_response(events_data)


@app.get("/api/runs/{session_id}/tokens")
async def api_run_tokens(session_id: str):
    """M5.1: Token summary for a session/run."""
    from monitoring.token_reporter import TokenReporter

    config = WeaveConfig.from_env()
    store = SessionStore(config.event_store_path)
    events = store.get_events(session_id)
    if not events:
        raise HTTPException(status_code=404, detail="Session not found")

    reporter = TokenReporter()
    summary = reporter.summarize_run(events)
    return summary.model_dump()


@app.get("/api/plans")
async def api_list_plans():
    return {"plans": _list_plans()}


def _list_sessions() -> list[dict]:
    """List all sessions from the event store."""
    config = WeaveConfig.from_env()
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
            "created_at": (
                start_event.timestamp.isoformat() if start_event.timestamp else None
            ),
            "updated_at": (
                end_event.timestamp.isoformat()
                if end_event and end_event.timestamp else None
            ),
            "event_count": len(events),
            "tool_calls": tool_calls,
            "workflow_stages": len(node_events),
        })

    # Sort by created_at desc
    sessions.sort(key=lambda x: x["created_at"] or "", reverse=True)
    return sessions


def _get_session_data(session_id: str) -> dict:
    """Get full session data including events and DAG info."""
    config = WeaveConfig.from_env()
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
    # Look for DAG stored via session.dag event
    for event in events:
        if event.get("type") == "session.dag":
            payload = event.get("payload", {})
            if isinstance(payload, dict) and "nodes" in payload and "edges" in payload:
                return payload

    # Fallback: check any event payload for DAG-like data
    for event in events:
        payload = event.get("payload", {})
        if isinstance(payload, dict):
            if "nodes" in payload and "edges" in payload:
                return payload

    return None


def _build_dag_response(events: list[dict]) -> dict:
    """Build a DAG response with node statuses from session events."""
    # Find session.dag event for structure
    dag_payload = None
    for event in events:
        if event.get("type") == "session.dag":
            payload = event.get("payload", {})
            if isinstance(payload, dict) and "nodes" in payload:
                dag_payload = payload
                break

    if not dag_payload:
        return {
            "nodes": [], "edges": [], "levels": [],
            "reasoning": "", "requirement": "",
        }

    raw_nodes = dag_payload.get("nodes", {})
    raw_edges = dag_payload.get("edges", [])
    requirement = dag_payload.get("requirement", "")
    reasoning = dag_payload.get("reasoning", "")

    # Build status map from workflow events
    node_status: dict[str, str] = {}
    node_times: dict[str, dict] = {}
    for event in events:
        evt_type = event.get("type", "")
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            continue
        # stage_start → running
        if evt_type == "workflow.stage_start":
            nid = payload.get("node_id") or payload.get("stage")
            if nid:
                node_status[nid] = "running"
                node_times.setdefault(nid, {})["started_at"] = event.get("timestamp")
        # stage_end → success
        elif evt_type == "workflow.stage_end":
            nid = payload.get("node_id") or payload.get("stage")
            if nid:
                node_status[nid] = "success"
                node_times.setdefault(nid, {})["completed_at"] = event.get("timestamp")
        # stage_error → failed
        elif evt_type == "workflow.stage_error":
            nid = payload.get("node_id") or payload.get("stage")
            if nid:
                node_status[nid] = "failed"
                node_times.setdefault(nid, {})["completed_at"] = event.get("timestamp")

    # Convert nodes dict to array
    nodes_array = []
    for nid, ndata in raw_nodes.items():
        if not isinstance(ndata, dict):
            ndata = {"task": str(ndata)}
        times = node_times.get(nid, {})
        started = times.get("started_at")
        completed = times.get("completed_at")
        duration = None
        if started and completed:
            try:
                s = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
                e = datetime.fromisoformat(str(completed).replace("Z", "+00:00"))
                duration = int((e - s).total_seconds() * 1000)
            except (ValueError, TypeError):
                pass
        nodes_array.append({
            "id": nid,
            "agent_type": ndata.get("agent_type", "worker"),
            "task": ndata.get("task", ""),
            "status": node_status.get(nid, "pending"),
            "started_at": started,
            "completed_at": completed,
            "error": None,
            "duration_ms": duration,
        })

    # Compute topological levels (Kahn's algorithm)
    node_ids = set(raw_nodes.keys())
    in_degree: dict[str, int] = {nid: 0 for nid in node_ids}
    adjacency: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for edge in raw_edges:
        src = edge.get("from", "")
        dst = edge.get("to", "")
        if src in node_ids and dst in node_ids:
            adjacency[src].append(dst)
            in_degree[dst] += 1

    levels: list[list[str]] = []
    queue = [nid for nid in node_ids if in_degree[nid] == 0]
    visited = set()
    while queue:
        level = sorted(queue)
        levels.append(level)
        visited.update(level)
        next_queue = []
        for nid in level:
            for dst in adjacency[nid]:
                in_degree[dst] -= 1
                if in_degree[dst] == 0 and dst not in visited:
                    next_queue.append(dst)
        queue = next_queue
    # Add any remaining nodes (cycles)
    remaining = [nid for nid in node_ids if nid not in visited]
    if remaining:
        levels.append(sorted(remaining))

    edges_array = [
        {"from": e.get("from", ""), "to": e.get("to", ""), "dependency_type": e.get("dependency_type", "hard")}
        for e in raw_edges
    ]

    return {
        "nodes": nodes_array,
        "edges": edges_array,
        "levels": levels,
        "reasoning": reasoning,
        "requirement": requirement,
    }


def _list_plans() -> list[dict]:
    """List all saved execution plans."""
    plans_dir = Path("./data/plans")
    if not plans_dir.exists():
        return []

    plans = []
    for plan_file in sorted(plans_dir.glob("plan_*.json"), reverse=True):
        try:
            data = json.loads(plan_file.read_text(encoding="utf-8"))
            plans.append({
                "file": str(plan_file),
                "reasoning": data.get("reasoning", "")[:100],
                "node_count": len(data.get("nodes", [])),
                "levels": data.get("levels", []),
            })
        except Exception:
            continue

    return plans


# ── Web Console ──────────────────────────────────────────────────────

@app.get("/console", response_class=HTMLResponse)
async def console_page():
    """Serve the Web Console (Jobs/Runs/Tickets/Alerts)."""
    console_file = static_path / "console.html"
    if console_file.exists():
        return HTMLResponse(content=console_file.read_text(encoding="utf-8"), status_code=200)
    return HTMLResponse(content="<h1>Weave Console</h1><p>Console not built yet.</p>")


@app.get("/api/jobs")
async def api_list_jobs(status: str | None = None):
    """List jobs with optional status filter."""
    repo = JobRepository()
    job_status = None
    if status:
        try:
            job_status = JobStatus(status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
    jobs = repo.list_jobs(status=job_status)
    return {
        "jobs": [
            {
                "id": j.id,
                "requirement": j.requirement,
                "status": j.status.value,
                "attempt": j.attempt,
                "last_error": j.last_error,
                "error_category": j.error_category,
                "created_at": j.created_at.isoformat() if j.created_at else None,
                "updated_at": j.updated_at.isoformat() if j.updated_at else None,
            }
            for j in sorted(jobs, key=lambda x: x.created_at or datetime.min, reverse=True)
        ],
        "count": len(jobs),
    }


@app.get("/api/jobs/{job_id}")
async def api_get_job(job_id: str):
    """Get job details with runs."""
    repo = JobRepository()
    job = repo.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    runs = repo.list_runs_by_job(job_id)
    return {
        "job": {
            "id": job.id,
            "requirement": job.requirement,
            "status": job.status.value,
            "attempt": job.attempt,
            "last_error": job.last_error,
            "error_category": job.error_category,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        },
        "runs": [
            {
                "id": r.id,
                "session_id": r.session_id,
                "status": r.status.value,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            }
            for r in runs
        ],
    }


@app.post("/api/jobs/{job_id}/cancel")
async def api_cancel_job(job_id: str):
    """Cancel a job."""
    repo = JobRepository()
    try:
        job = repo.transition_job_status(job_id, JobStatus.CANCELED)
        # Cancel any in-flight run task
        # Access the global running tasks map if available
        # (workers register tasks via RunService._running_tasks)
        return {"job_id": job.id, "status": job.status.value, "message": "Job canceled"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/jobs/{job_id}/retry")
async def api_retry_job(job_id: str):
    """Retry a failed/dead_letter job."""
    repo = JobRepository()
    job = repo.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in (JobStatus.FAILED, JobStatus.DEAD_LETTER):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot retry job in status {job.status.value}",
        )

    try:
        job = repo.transition_job_status(job_id, JobStatus.QUEUED)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    job.last_error = ""
    job.error_category = ""
    repo.update_job(job)
    return {"job_id": job.id, "status": job.status.value, "message": "Job queued for retry"}


@app.post("/api/recover")
async def api_recover():
    """Recover orphaned jobs."""
    repo = JobRepository()
    orphaned = repo.recover_orphan_jobs()
    recovered = []
    for job in orphaned:
        # Update associated run records
        runs = repo.list_runs_by_job(job.id)
        from control_plane.models import RunStatus
        for r in runs:
            if r.status == RunStatus.RUNNING:
                r.status = RunStatus.ABORTED
                r.completed_at = datetime.now(timezone.utc)
                r.dag_result = {"error": "recovered", "reason": "Orphaned job recovered"}
                repo._persist_run(r)

        if job.status == JobStatus.LEASED:
            recovered.append(repo.transition_job_status(
                job.id,
                JobStatus.QUEUED,
                error="Recovered orphaned leased job",
                error_category="timeout",
            ))
        elif job.status == JobStatus.RUNNING:
            failed_job = repo.transition_job_status(
                job.id,
                JobStatus.FAILED,
                error="Recovered orphaned running job",
                error_category="timeout",
            )
            # Respect retry limits: only requeue if attempts remain
            if failed_job.attempt < failed_job.retry_policy.max_attempts:
                recovered.append(repo.transition_job_status(
                    failed_job.id,
                    JobStatus.QUEUED,
                    error="Recovered orphaned running job",
                    error_category="timeout",
                ))
            else:
                recovered.append(repo.transition_job_status(
                    failed_job.id,
                    JobStatus.DEAD_LETTER,
                    error="Recovered orphaned running job (retries exhausted)",
                    error_category="timeout",
                ))
    return {
        "recovered_count": len(recovered),
        "recovered_jobs": [{"id": j.id, "status": j.status.value} for j in recovered],
    }


@app.get("/api/tickets")
async def api_list_tickets(status: str | None = None, job_id: str | None = None):
    """List approval tickets."""
    repo = ApprovalRepository()
    ticket_status = None
    if status:
        try:
            ticket_status = TicketStatus(status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
    tickets = repo.list_tickets(status=ticket_status, job_id=job_id)
    return {
        "tickets": [
            {
                "id": t.id,
                "job_id": t.job_id,
                "tool_name": t.tool_name,
                "status": t.status.value,
                "risk_level": t.risk_level,
                "args_preview": t.args_preview,
                "requested_at": t.requested_at.isoformat() if t.requested_at else None,
                "expires_at": t.expires_at.isoformat() if t.expires_at else None,
            }
            for t in sorted(tickets, key=lambda x: x.requested_at or datetime.min, reverse=True)
        ],
        "count": len(tickets),
        "stats": repo.get_stats(),
    }


@app.post("/api/tickets/{ticket_id}/approve")
async def api_approve_ticket(ticket_id: str, reason: str = ""):
    """Approve a ticket."""
    repo = ApprovalRepository()
    try:
        ticket = repo.approve_ticket(ticket_id, reason=reason)
        return {"ticket_id": ticket.id, "status": ticket.status.value, "message": "Approved"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/tickets/{ticket_id}/reject")
async def api_reject_ticket(ticket_id: str, body: RejectRequest):
    """Reject a ticket."""
    repo = ApprovalRepository()
    try:
        ticket = repo.reject_ticket(ticket_id, reason=body.reason)
        return {"ticket_id": ticket.id, "status": ticket.status.value, "message": "Rejected"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/metrics")
async def api_metrics():
    """Get system metrics."""
    job_repo = JobRepository()
    from monitoring.metrics import MetricsCollector
    collector = MetricsCollector(job_repo)
    return collector.collect()


@app.get("/api/alerts")
async def api_alerts():
    """Get active alerts."""
    job_repo = JobRepository()
    approval_repo = ApprovalRepository()
    from monitoring.alerts import create_default_alerts
    manager = create_default_alerts(job_repo, approval_repo)
    return {"alerts": [a.__dict__ for a in manager.check_all()]}


# ── Memory API (M3.2) ───────────────────────────────────────────────


def _get_memory_manager():
    """Create a MemoryManager from config."""
    from memory.manager import MemoryManager
    config = WeaveConfig.from_env()
    return MemoryManager(config.memory)


@app.get("/api/memory")
async def api_list_memory(
    agent_type: str | None = None,
    scope: str | None = None,
    memory_type: str | None = None,
    limit: int = 50,
):
    """List memory entries with optional filters."""
    from core.models import MemoryScope, MemoryType
    manager = _get_memory_manager()
    scope_enum = MemoryScope(scope) if scope else None
    type_enum = MemoryType(memory_type) if memory_type else None

    entries = manager.store.list_entries(
        scope=scope_enum,
        agent_type=agent_type,
        memory_type=type_enum,
    )[:limit]
    return {
        "entries": [
            {
                "id": e.id,
                "agent_type": e.agent_type,
                "scope": e.scope.value,
                "type": e.memory_type.value,
                "content": e.content,
                "keywords": e.keywords,
                "relevance_score": e.relevance_score,
                "access_count": e.access_count,
                "created_at": e.created_at.isoformat(),
            }
            for e in entries
        ],
        "count": len(entries),
    }


@app.get("/api/memory/search")
async def api_search_memory(query: str, limit: int = 10):
    """Search memory entries by keyword."""
    manager = _get_memory_manager()
    entries = manager.store.search(query=query, limit=limit)
    return {
        "entries": [
            {
                "id": e.id,
                "agent_type": e.agent_type,
                "scope": e.scope.value,
                "type": e.memory_type.value,
                "content": e.content,
                "keywords": e.keywords,
                "relevance_score": e.relevance_score,
            }
            for e in entries
        ],
        "count": len(entries),
    }


@app.get("/api/memory/stats")
async def api_memory_stats():
    """Get memory system statistics."""
    manager = _get_memory_manager()
    return manager.get_stats()


@app.post("/api/memory")
async def api_add_memory(body: MemoryAddRequest):
    """Add a manual memory entry."""
    from core.models import MemoryScope, MemoryType
    manager = _get_memory_manager()
    entry = manager.store_learning(
        agent_type=body.agent_type,
        content=body.content,
        memory_type=MemoryType(body.memory_type),
        scope=MemoryScope(body.scope),
        keywords=body.keywords if body.keywords else None,
    )
    return {"id": entry.id, "message": "Memory entry added"}


@app.delete("/api/memory/{memory_id}")
async def api_delete_memory(memory_id: str):
    """Delete a specific memory entry."""
    manager = _get_memory_manager()
    if not manager.store.delete(memory_id):
        raise HTTPException(status_code=404, detail="Memory entry not found")
    return {"id": memory_id, "message": "Deleted"}


@app.post("/api/memory/cleanup")
async def api_memory_cleanup():
    """Run memory maintenance (expire, prune, recompute)."""
    manager = _get_memory_manager()
    return manager.run_maintenance()


# ── Learning API (M3.3) ──────────────────────────────────────────────


def _get_learning_scheduler():
    """Create a LearningScheduler from config."""
    from learning.analyzer import LearningAnalyzer
    from learning.optimizer import LearningOptimizer
    from learning.scheduler import LearningScheduler
    from control_plane.repository import JobRepository
    from monitoring.metrics import MetricsCollector

    config = WeaveConfig.from_env()
    memory_manager = _get_memory_manager()
    job_repo = JobRepository()
    metrics_collector = MetricsCollector(job_repo)

    analyzer = LearningAnalyzer(metrics_collector, memory_manager, config.learning)
    optimizer = LearningOptimizer(memory_manager)
    return LearningScheduler(config.learning, analyzer, optimizer)


@app.get("/api/learning/insights")
async def api_learning_insights(limit: int = 20):
    """List learning insights stored as memories."""
    manager = _get_memory_manager()
    entries = manager.store.search(
        query="recommendation pattern anti_pattern",
        limit=limit,
    )
    return {
        "insights": [
            {
                "id": e.id,
                "content": e.content,
                "keywords": e.keywords,
                "relevance_score": e.relevance_score,
                "created_at": e.created_at.isoformat(),
            }
            for e in entries
        ],
        "count": len(entries),
    }


@app.post("/api/learning/analyze")
async def api_learning_analyze():
    """Trigger a learning analysis run."""
    scheduler = _get_learning_scheduler()
    return scheduler.run_analysis()


@app.get("/api/learning/status")
async def api_learning_status():
    """Get learning system status."""
    scheduler = _get_learning_scheduler()
    return scheduler.get_status()


# ── Template API (M3.4) ────────────────────────────────────────────


class TemplateInstantiateRequest(PydanticModel):
    model_config = {"extra": "forbid"}
    variables: dict[str, str] = {}


@app.get("/api/templates")
async def api_list_templates():
    """List available DAG templates."""
    from templates.library import TemplateRegistry
    registry = TemplateRegistry()
    templates = registry.list_templates()
    return {
        "templates": [
            {
                "name": t.name,
                "description": t.description,
                "version": t.version,
                "category": t.category,
                "nodes": len(t.nodes),
                "edges": len(t.edges),
                "variables": list(t.variables.keys()),
            }
            for t in templates
        ],
        "count": len(templates),
    }


@app.post("/api/templates/{name}/instantiate")
async def api_instantiate_template(name: str, request: TemplateInstantiateRequest):
    """Instantiate a template with variable substitution."""
    from templates.library import TemplateRegistry
    registry = TemplateRegistry()
    try:
        dag = registry.instantiate(name, request.variables)
    except ValueError as e:
        msg = str(e)
        if "not found" in msg.lower():
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=422, detail=msg)
    return {
        "nodes": [{
            "id": n.id,
            "agent_type": n.agent_type,
            "task": n.task_description,
        } for n in dag.nodes.values()],
        "edges": [{"from": e.from_node, "to": e.to_node} for e in dag.edges],
        "reasoning": dag.reasoning,
    }


# ── Job Submission & Workspace APIs ──────────────────────────────────


def _get_run_service() -> "RunService":
    """Lazily create and cache a RunService singleton."""
    if not hasattr(app.state, "run_service"):
        from control_plane.service import RunService
        weave_config = WeaveConfig.from_env()
        app.state.run_service = RunService(
            repository=JobRepository(base_path="./data/jobs"),
            llm_config=weave_config.llm,
            default_backend=weave_config.default_backend,
            backend_base_path=weave_config.backend_base_path,
            approval_repo=ApprovalRepository(),
            non_interactive=True,
            approval_timeout_sec=weave_config.approval_timeout_sec,
            budget_config=weave_config.budget,
        )
    return app.state.run_service


@app.post("/api/jobs")
async def api_submit_job(body: SubmitJobRequest):
    """Submit a new job via the web UI."""
    service = _get_run_service()
    try:
        job = await service.submit_job(
            requirement=body.requirement,
            project_path=body.workspace,
        )
    except Exception as exc:
        logger.exception("Failed to submit job")
        raise HTTPException(status_code=500, detail="Failed to submit job. Check server logs for details.")

    return {
        "id": job.id,
        "status": job.status.value,
        "requirement": job.requirement,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }


_WORKSPACES_FILE = Path("./data/workspaces.json")
_PRESET_WORKSPACES: list[dict] = []
_workspaces_lock = asyncio.Lock()


def _load_workspaces() -> list[dict]:
    """Load workspaces from presets + user-configured paths."""
    workspaces = list(_PRESET_WORKSPACES)
    if _WORKSPACES_FILE.exists():
        try:
            custom = json.loads(_WORKSPACES_FILE.read_text(encoding="utf-8"))
            workspaces.extend(custom)
        except (json.JSONDecodeError, OSError):
            pass
    return workspaces


@app.get("/api/workspaces")
async def api_list_workspaces():
    """List available workspaces (presets + custom)."""
    return {"workspaces": _load_workspaces()}


@app.post("/api/workspaces")
async def api_add_workspace(body: AddWorkspaceRequest):
    """Add a custom workspace path."""
    async with _workspaces_lock:
        _WORKSPACES_FILE.parent.mkdir(parents=True, exist_ok=True)
        existing = []
        if _WORKSPACES_FILE.exists():
            try:
                existing = json.loads(_WORKSPACES_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                existing = []
        # Avoid duplicates
        if any(w.get("path") == body.path for w in existing):
            raise HTTPException(status_code=409, detail="Workspace already exists")
        entry = {"path": body.path, "label": body.label or body.path}
        existing.append(entry)
        _WORKSPACES_FILE.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return entry


@app.get("/api/jobs/{job_id}/summary")
async def api_job_summary(job_id: str):
    """Get the completion summary for a job."""
    repo = JobRepository()
    job = repo.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    runs = repo.list_runs_by_job(job_id)
    if not runs:
        return {"title": "No runs yet", "content": ""}

    # Find the latest completed run's session
    config = WeaveConfig.from_env()
    store = SessionStore(config.event_store_path)
    for run in reversed(runs):
        if run.session_id:
            events = store.get_events(run.session_id)
            # Look for the final summary event
            for evt in reversed(events):
                evt_type = evt.type.value if hasattr(evt, "type") else str(evt)
                evt_payload = evt.payload if hasattr(evt, "payload") else {}
                if "end" in evt_type or "summary" in evt_type or "result" in evt_type:
                    data = evt_payload if isinstance(evt_payload, dict) else {}
                    return {
                        "title": data.get("title", f"Run {run.id} completed"),
                        "content": data.get("content", data.get("result", "")),
                    }

    return {"title": f"Job {job_id}", "content": "No summary available"}


# ── Notification & Search APIs (M8.3) ──────────────────────────────


_NOTIF_PREFS_FILE = Path("./data/notification_preferences.json")
_DEFAULT_NOTIF_PREFS = {
    "on_succeeded": True,
    "on_failed": True,
    "on_stuck": True,
    "on_pending_approval": True,
}
_notif_lock = asyncio.Lock()


def _load_notif_prefs() -> dict:
    if _NOTIF_PREFS_FILE.exists():
        try:
            return {**_DEFAULT_NOTIF_PREFS, **json.loads(_NOTIF_PREFS_FILE.read_text(encoding="utf-8"))}
        except (json.JSONDecodeError, OSError):
            pass
    return dict(_DEFAULT_NOTIF_PREFS)


def _save_notif_prefs(prefs: dict) -> dict:
    _NOTIF_PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _NOTIF_PREFS_FILE.write_text(json.dumps(prefs, indent=2), encoding="utf-8")
    return prefs


@app.get("/api/notification-preferences")
async def api_get_notif_prefs():
    return _load_notif_prefs()


@app.put("/api/notification-preferences")
async def api_update_notif_prefs(prefs: NotificationPrefsUpdate):
    async with _notif_lock:
        saved = _load_notif_prefs()
        saved.update({k: v for k, v in prefs.model_dump(exclude_none=True).items() if k in _DEFAULT_NOTIF_PREFS})
        return _save_notif_prefs(saved)


@app.get("/api/search")
async def api_search(
    q: str = "",
    status: str | None = None,
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Full-text search across jobs with pagination."""
    repo = JobRepository()
    all_jobs = repo.list_jobs()
    matches = []
    highlights: dict[str, str] = {}
    q_lower = q.lower()

    for job in all_jobs:
        if status and job.status.value != status:
            continue
        if from_ and job.created_at and job.created_at.isoformat() < from_:
            continue
        if to and job.created_at and job.created_at.isoformat() > to:
            continue
        if q_lower:
            req_lower = job.requirement.lower()
            if q_lower not in req_lower and q_lower not in job.id.lower():
                continue
            idx = req_lower.find(q_lower)
            start = max(0, idx - 30)
            end = min(len(job.requirement), idx + len(q_lower) + 30)
            snippet = job.requirement[start:end]
            if start > 0:
                snippet = "..." + snippet
            if end < len(job.requirement):
                snippet = snippet + "..."
            highlights[job.id] = snippet
        matches.append({
            "id": job.id,
            "requirement": job.requirement,
            "status": job.status.value,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        })

    total = len(matches)
    page = matches[offset:offset + limit]
    return {"jobs": page, "highlights": highlights, "count": total}


# ── Task Templates & Annotations APIs (M8.4) ──────────────────────


_TASK_TEMPLATES_DIR = Path("./data/task_templates")
_ANNOTATIONS_DIR = Path("./data/annotations")
_annotations_lock = asyncio.Lock()


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _sanitize_filename(name: str) -> str:
    """Sanitize a user-supplied filename to prevent path traversal."""
    return "".join(c for c in name if c.isalnum() or c in "-_ ")


@app.get("/api/task-templates")
async def api_list_task_templates():
    _ensure_dir(_TASK_TEMPLATES_DIR)
    templates = []
    for f in sorted(_TASK_TEMPLATES_DIR.glob("*.yaml")):
        try:
            import yaml
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
            if data:
                data["filename"] = f.stem
                templates.append(data)
        except Exception:
            pass
    from templates.library import TemplateRegistry
    try:
        registry = TemplateRegistry()
        for t in registry.list_templates():
            templates.append({"name": t.name, "description": t.description, "category": "dag", "filename": t.name})
    except Exception:
        pass
    return {"templates": templates, "count": len(templates)}


@app.post("/api/task-templates")
async def api_create_task_template(body: TaskTemplateCreate):
    _ensure_dir(_TASK_TEMPLATES_DIR)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Template name is required")
    safe_name = _sanitize_filename(name)
    filepath = _TASK_TEMPLATES_DIR / f"{safe_name}.yaml"
    if filepath.exists():
        raise HTTPException(status_code=409, detail="Template already exists")
    import yaml
    filepath.write_text(yaml.dump(body.model_dump(), allow_unicode=True, default_flow_style=False), encoding="utf-8")
    return body.model_dump()


@app.put("/api/task-templates/{name}")
async def api_update_task_template(name: str, body: TaskTemplateUpdate):
    safe_name = _sanitize_filename(name)
    filepath = _TASK_TEMPLATES_DIR / f"{safe_name}.yaml"
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Template not found")
    import yaml
    existing = yaml.safe_load(filepath.read_text(encoding="utf-8")) or {}
    existing.update({k: v for k, v in body.model_dump(exclude_none=True).items()})
    filepath.write_text(yaml.dump(existing, allow_unicode=True, default_flow_style=False), encoding="utf-8")
    return existing


@app.delete("/api/task-templates/{name}")
async def api_delete_task_template(name: str):
    safe_name = _sanitize_filename(name)
    filepath = _TASK_TEMPLATES_DIR / f"{safe_name}.yaml"
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Template not found")
    filepath.unlink()
    return {"deleted": name}


@app.get("/api/jobs/{job_id}/annotations")
async def api_get_annotations(job_id: str):
    safe_id = _sanitize_filename(job_id)
    filepath = _ANNOTATIONS_DIR / f"{safe_id}.json"
    if not filepath.exists():
        return {"job_id": job_id, "tags": [], "notes": "", "rating": 0, "updated_at": ""}
    return json.loads(filepath.read_text(encoding="utf-8"))


@app.put("/api/jobs/{job_id}/annotations")
async def api_update_annotations(job_id: str, body: AnnotationUpdate):
    _ensure_dir(_ANNOTATIONS_DIR)
    safe_id = _sanitize_filename(job_id)
    filepath = _ANNOTATIONS_DIR / f"{safe_id}.json"
    async with _annotations_lock:
        existing = {}
        if filepath.exists():
            try:
                existing = json.loads(filepath.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        existing.update({"job_id": job_id, "updated_at": datetime.now(timezone.utc).isoformat()})
        for key in ("tags", "notes", "rating"):
            val = getattr(body, key, None)
            if val is not None:
                existing[key] = val
        filepath.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    return existing


# ── Integration helpers ──────────────────────────────────────────────

def get_event_bridge() -> WebSocketEventBridge:
    """Get the global WebSocket event bridge instance."""
    return bridge


async def run_server(host: str = "0.0.0.0", port: int = 8080) -> None:
    """Run the Weave UI server (programmatic entry point)."""
    import uvicorn
    await uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="info")
    ).serve()
