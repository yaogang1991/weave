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

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is on path when running server directly
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel as PydanticModel  # noqa: E402

from visualizer.event_bridge import WebSocketEventBridge  # noqa: E402
from session.store import SessionStore  # noqa: E402
from core.config import HarnessConfig  # noqa: E402

from control_plane.models import JobStatus  # noqa: E402
from control_plane.repository import JobRepository  # noqa: E402
from control_plane.approval import ApprovalRepository, TicketStatus  # noqa: E402


app = FastAPI(title="Harness Visualizer", version="2.0")
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
    return HTMLResponse(content="<h1>Harness Console</h1><p>Console not built yet.</p>")


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

    job.status = JobStatus.QUEUED
    job.attempt = 0
    job.last_error = ""
    job.error_category = ""
    job.lease_owner = None
    job.lease_expires_at = None
    job.updated_at = datetime.now(timezone.utc)
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
    config = HarnessConfig.from_env()
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

    config = HarnessConfig.from_env()
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
    variables: dict[str, str] = {}

    class Config:
        extra = "forbid"


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
