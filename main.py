#!/usr/bin/env python3
"""
Harness CLI Entry Point: Intelligent Multi-Agent Orchestration.

Usage:
    python main.py plan "Build a REST API for user authentication"
    python main.py execute ./data/plans/plan_xxx.json
    python main.py run "Add OAuth2 support" --project ./my-project
    python main.py viz                    # Launch visualizer dashboard
    python main.py run "Build API" --viz  # Run with live visualization
"""

import argparse
import asyncio
import json
import os
import sys
import uuid
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.config import HarnessConfig, LLMConfig
from core.agent_registry import AgentRegistry
from core.models import DAG, DAGNode
from core.dag_engine import DAGExecutionEngine
from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
from agent.agent_pool import AgentPool
from session.store import SessionStore
from tools.registry import ToolRegistry
from guardrails.policy import Guardrails, GuardrailPolicy, PermissionMode

# Control plane imports
from control_plane.models import JobStatus
from control_plane.repository import JobRepository
from control_plane.service import RunService
from control_plane.worker import TaskWorker, WorkerConfig, run_worker
from control_plane.approval import ApprovalRepository, TicketStatus


def load_registry(project_path: str | None = None) -> AgentRegistry:
    """Load agent registry with defaults + project custom agents."""
    registry = AgentRegistry()

    # Load project-specific agents if .harness/agents.yaml exists
    if project_path:
        agents_yaml = Path(project_path) / ".harness" / "agents.yaml"
        if agents_yaml.exists():
            print(f"Loading project agents from {agents_yaml}")
            registry.load_from_yaml(agents_yaml)

    return registry


def _serialize_dag(dag: DAG) -> dict:
    """Serialize a DAG to a JSON-compatible dict."""
    return {
        "reasoning": dag.reasoning,
        "nodes": [
            {
                "id": n.id,
                "agent_type": n.agent_type,
                "task": n.task_description,
                "success_criteria": n.success_criteria,
            }
            for n in dag.nodes.values()
        ],
        "edges": [{"from": e.from_node, "to": e.to_node} for e in dag.edges],
    }


def _parse_template_vars(var_list: list[str]) -> dict[str, str]:
    """Parse KEY=VALUE pairs from --var arguments."""
    variables: dict[str, str] = {}
    for item in var_list:
        if "=" in item:
            key, value = item.split("=", 1)
            variables[key.strip()] = value.strip()
        else:
            raise ValueError(f"Invalid --var format: {item} (expected KEY=VALUE)")
    return variables


async def cmd_plan(args):
    """Generate an execution plan (DAG) from requirements."""
    config = HarnessConfig.from_env()
    store = SessionStore(config.event_store_path)
    registry = load_registry(args.project)

    # M3.1: LLM router for multi-model support
    llm_router = None
    if config.model_routing.routing:
        from core.llm_router import LLMRouter
        llm_router = LLMRouter(config.model_routing, config.llm)

    # M3.3: Learning optimizer for planning hints
    learning_optimizer = None
    if config.memory.enabled:
        try:
            from memory.manager import MemoryManager
            from learning.optimizer import LearningOptimizer
            mm = MemoryManager(config.memory, session_store=store)
            learning_optimizer = LearningOptimizer(mm)
        except Exception:
            pass

    orchestrator = IntelligentOrchestrator(
        llm_config=config.llm,
        session_store=store,
        agent_registry=registry,
        llm_router=llm_router,
        learning_optimizer=learning_optimizer,
    )

    print(f"Planning: {args.requirement}")
    print(f"Available agents: {[a.id for a in registry.list_agents()]}")

    # Use template if specified
    if args.template:
        variables = _parse_template_vars(args.var)
        print(f"Using template: {args.template} (vars: {variables})")
        dag = await orchestrator.plan_from_template(
            template_name=args.template,
            variables=variables,
        )
    else:
        # Generate DAG via LLM
        dag = await orchestrator.plan(
            requirement=args.requirement,
            project_context={"project_path": args.project} if args.project else None,
        )

    # Save plan with deterministic filename
    plans_dir = Path("./data/plans")
    plans_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    plan_file = plans_dir / f"plan_{timestamp}_{uuid.uuid4().hex[:8]}.json"

    plan_data = _serialize_dag(dag)
    plan_data["levels"] = dag.topological_levels()

    with open(plan_file, "w") as f:
        json.dump(plan_data, f, indent=2, default=str)

    # Print plan summary
    print(f"\nPlan saved: {plan_file}")
    print(f"\nReasoning: {dag.reasoning}")
    print(f"\nExecution levels:")
    for i, level in enumerate(dag.topological_levels()):
        print(f"  Level {i}: {' → '.join(level)}")

    return dag


async def cmd_execute(args, dag: DAG | None = None):
    """Execute a saved plan (DAG). Accepts DAG directly to avoid re-serialization."""
    config = HarnessConfig.from_env()
    store = SessionStore(config.event_store_path)
    registry = load_registry(args.project)
    tool_registry = ToolRegistry()

    # Create session
    session_id = str(uuid.uuid4())
    store.create_session(session_id, "harness_run")

    # Load DAG from file if not provided directly
    if dag is None:
        with open(args.plan_file, "r") as f:
            plan_data = json.load(f)

        dag = DAG(reasoning=plan_data.get("reasoning", ""))
        for node_def in plan_data["nodes"]:
            dag.add_node(DAGNode(
                id=node_def["id"],
                agent_type=node_def["agent_type"],
                task_description=node_def["task"],
                success_criteria=node_def.get("success_criteria", []),
            ))
        for edge_def in plan_data.get("edges", []):
            dag.add_edge(edge_def["from"], edge_def["to"])

    # Store DAG in session for visualizer
    from core.models import EventType
    store.emit_event(
        session_id,
        EventType.SESSION_DAG,
        _serialize_dag(dag),
    )

    # Create guardrails (default: accept_edits)
    policy = GuardrailPolicy(
        mode=PermissionMode.ACCEPT_EDITS,
        auto_approve_read=True,
        max_iterations=args.max_iterations,
    )
    guardrails = Guardrails(policy, tool_registry)

    # M3.1: LLM router for multi-model support
    llm_router = None
    if config.model_routing.routing:
        from core.llm_router import LLMRouter
        llm_router = LLMRouter(config.model_routing, config.llm)

    # M3.2: Initialize memory manager
    memory_manager = None
    if config.memory.enabled:
        try:
            from memory.manager import MemoryManager
            memory_manager = MemoryManager(config.memory, session_store=store)
        except Exception:
            pass

    # M3.3: Initialize learning optimizer
    learning_optimizer = None
    if memory_manager:
        try:
            from learning.optimizer import LearningOptimizer
            learning_optimizer = LearningOptimizer(memory_manager)
        except Exception:
            pass

    # Create agent pool with guardrails + M3 integration
    pool = AgentPool(
        llm_config=config.llm,
        session_store=store,
        agent_registry=registry,
        tool_registry=tool_registry,
        guardrails=guardrails,
        max_iterations=args.max_iterations,
        timeout=config.agent_timeout,
        max_context_tokens=config.max_context_tokens,
        llm_router=llm_router,
        memory_manager=memory_manager,
    )

    # Create orchestrator for failure handling + M3 learning
    orchestrator = IntelligentOrchestrator(
        config.llm, store, registry,
        llm_router=llm_router,
        learning_optimizer=learning_optimizer,
    )

    # Create evaluator for quality gates
    from evaluator.engine import EvaluatorEngine
    evaluator = EvaluatorEngine(session_store=store)

    # Create DAG engine + M3 memory integration
    engine = DAGExecutionEngine(
        agent_executor=pool.get_executor(session_id),
        failure_handler=orchestrator.adapt_to_failure,
        max_parallel=args.max_parallel,
        evaluator=evaluator,
        artifact_path=config.artifact_path,
        memory_manager=memory_manager,
        session_id=session_id,
    )

    # ── Visualization setup ──────────────────────────────────
    bridge = None
    server_task = None
    cli_renderer = None

    if args.viz or args.visualize:
        from visualizer.cli_renderer import CLIDAGRenderer

        # CLI renderer (always enabled when --viz or --visualize)
        cli_renderer = CLIDAGRenderer()
        engine.on_event(cli_renderer.handle_event)
        cli_renderer.render_dag(dag)

        if args.visualize:
            from visualizer.event_bridge import WebSocketEventBridge
            import uvicorn
            from visualizer.server import app as viz_app

            bridge = WebSocketEventBridge()
            engine.on_event(bridge.handle_event)

            # Start web server in background
            server_cfg = uvicorn.Config(viz_app, host="0.0.0.0", port=8080, log_level="warning")
            server = uvicorn.Server(server_cfg)
            server_task = asyncio.create_task(server.serve())
            await asyncio.sleep(0.5)  # Let server start

            if not args.no_browser:
                webbrowser.open("http://127.0.0.1:8080")

        await bridge.broadcast_session_start(session_id, _serialize_dag(dag)) if bridge else None

    # Default console progress
    async def on_event(event):
        print(f"  [{event.event_type.upper()}] {event.node_id}: {event.details}")

    engine.on_event(on_event)

    print(f"Executing DAG with {len(dag.nodes)} nodes...")
    print(f"Levels: {dag.topological_levels()}")
    print()

    # Execute
    result_dag = await engine.execute(dag)

    # Summary
    summary = engine.get_execution_summary(result_dag)
    print(f"\nExecution complete:")
    print(f"  Total: {summary['total_nodes']}")
    print(f"  Success: {summary['success']}")
    print(f"  Failed: {summary['failed']}")
    print(f"  Skipped: {summary['skipped']}")
    print(f"  Session ID: {session_id}")

    # Broadcast completion to visualization clients
    if bridge:
        await bridge.broadcast_session_end(session_id, summary)

    # CLI renderer final summary
    if cli_renderer:
        cli_renderer.render_summary(result_dag)

    # Keep server alive briefly so clients can see final state
    if server_task and not server_task.done():
        await asyncio.sleep(2)
        server_task.cancel()
        try:
            await server_task
        except asyncio.CancelledError:
            pass

    return result_dag


async def cmd_run(args):
    """Plan + Execute in one command."""
    # Plan
    dag = await cmd_plan(args)

    # Pass DAG directly to avoid serialization round-trip
    exec_args = argparse.Namespace(
        plan_file="",  # not used when dag is provided
        project=args.project,
        max_parallel=args.max_parallel,
        max_iterations=args.max_iterations,
        viz=args.viz,
        visualize=args.visualize,
        no_browser=args.no_browser,
        template=getattr(args, "template", None),
        var=getattr(args, "var", []),
    )
    return await cmd_execute(exec_args, dag=dag)


async def cmd_viz(args):
    """Launch the visualizer web server."""
    from visualizer.server import run_server

    host = args.host
    port = args.port

    print(f"🚀 Starting Harness Visualizer at http://{host}:{port}")
    print("Press Ctrl+C to stop")

    if not args.no_browser:
        await asyncio.sleep(1)
        webbrowser.open(f"http://{host}:{port}")

    await run_server(host=host, port=port)


# =============================================================================
# Control-plane CLI commands
# =============================================================================


def _write_error(code: str, message: str) -> None:
    """Write a structured JSON error to stderr and exit with code 1."""
    sys.stderr.write(json.dumps({"error": message, "code": code}) + "\n")
    sys.exit(1)


def _make_repository() -> JobRepository:
    """Create a JobRepository with the default data path."""
    return JobRepository(base_path="./data/jobs")


def _make_run_service(repository: JobRepository, non_interactive: bool = False) -> RunService:
    """Create a RunService with LLM config from environment."""
    harness_config = HarnessConfig.from_env()
    approval_repo = ApprovalRepository()

    # M3.1: Create LLM router if model routing is configured
    llm_router = None
    if harness_config.model_routing.routing:
        from core.llm_router import LLMRouter
        llm_router = LLMRouter(harness_config.model_routing, harness_config.llm)

    service = RunService(
        repository=repository,
        llm_config=harness_config.llm,
        default_backend=harness_config.workspace_isolation,
        backend_base_path=harness_config.backend_base_path,
        approval_repo=approval_repo,
        non_interactive=non_interactive,
        approval_timeout_sec=harness_config.approval_timeout_sec,
    )
    if llm_router:
        service.llm_router = llm_router
    return service


async def cmd_submit(args):
    """Submit a new job to the control plane."""
    repository = _make_repository()
    service = _make_run_service(repository)

    try:
        job = await service.submit_job(
            requirement=args.requirement,
            project_path=args.project,
            timeout=args.timeout,
            max_attempts=args.max_attempts,
        )
    except Exception as exc:
        _write_error("E_SUBMIT_FAILED", f"Failed to submit job: {exc}")
        return

    print(json.dumps({
        "job_id": job.id,
        "status": job.status.value,
        "message": "Job submitted",
    }))


async def cmd_status(args):
    """Get the status of a job including its runs."""
    repository = _make_repository()

    try:
        job = repository.get_job(args.job_id)
        if job is None:
            _write_error("E_JOB_NOT_FOUND", f"Job not found: {args.job_id}")
            return
        runs = repository.list_runs_by_job(args.job_id)
        result = {
            "job_id": job.id,
            "status": job.status.value,
            "requirement": job.requirement,
            "project_path": job.project_path,
            "attempt": job.attempt,
            "last_error": job.last_error,
            "error_category": job.error_category,
            "created_at": str(job.created_at),
            "updated_at": str(job.updated_at),
            "runs": [
                {
                    "run_id": r.id,
                    "status": r.status.value,
                    "session_id": r.session_id,
                    "started_at": str(r.started_at),
                    "completed_at": str(r.completed_at) if r.completed_at else None,
                }
                for r in runs
            ],
        }
    except Exception as exc:
        _write_error("E_STATUS_FAILED", f"Failed to get job status: {exc}")
        return

    print(json.dumps(result, default=str))


async def cmd_list_jobs(args):
    """List jobs, optionally filtered by status."""
    repository = _make_repository()

    try:
        jobs = repository.list_jobs(status=JobStatus(args.status) if args.status else None)
    except Exception as exc:
        _write_error("E_LIST_FAILED", f"Failed to list jobs: {exc}")
        return

    output = []
    for job in jobs:
        output.append({
            "job_id": job.id,
            "status": job.status.value,
            "requirement": job.requirement,
            "created_at": str(job.created_at),
            "updated_at": str(job.updated_at),
            "attempt": job.attempt,
            "last_error": job.last_error,
        })

    print(json.dumps(output, default=str))


async def cmd_cancel(args):
    """Cancel a job."""
    repository = _make_repository()

    try:
        job = repository.transition_job_status(args.job_id, JobStatus.CANCELED)
    except ValueError as exc:
        _write_error("E_CANCEL_FAILED", str(exc))
        return
    except Exception as exc:
        _write_error("E_CANCEL_FAILED", f"Failed to cancel job: {exc}")
        return

    print(json.dumps({
        "job_id": job.id,
        "status": job.status.value,
        "message": "Job canceled",
    }))


async def cmd_worker(args):
    """Start a background worker that polls for and executes jobs."""
    repository = _make_repository()
    non_interactive = args.non_interactive or os.getenv(
        "HARNESS_NON_INTERACTIVE", ""
    ).lower() in ("true", "1", "yes")
    service = _make_run_service(repository, non_interactive=non_interactive)
    config = WorkerConfig(
        concurrency=args.concurrency,
        poll_interval_sec=args.poll_interval,
        non_interactive=non_interactive,
    )

    await run_worker(repository, service, config)


async def cmd_recover(args):
    """
    Manually trigger recovery of orphaned jobs.

    Scans all jobs in ``leased`` or ``running`` status with an expired lease,
    and recovers them to ``queued`` or marks them as ``failed``.
    """
    repository = _make_repository()
    recovered = repository.recover_orphan_jobs()

    result = {
        "recovered_count": len(recovered),
        "recovered_jobs": [
            {"job_id": j.id, "old_status": "leased|running", "new_status": j.status.value}
            for j in recovered
        ],
        "message": f"Recovered {len(recovered)} orphan jobs",
    }
    print(json.dumps(result, indent=2, default=str))


async def cmd_console(args):
    """Launch the Web Console (FastAPI server)."""
    from visualizer.server import run_server
    print(f"Harness Console: http://{args.host}:{args.port}/console")
    print(f"Visualizer: http://{args.host}:{args.port}/")
    await run_server(host=args.host, port=args.port)


# =============================================================================
# Approval ticket CLI commands
# =============================================================================


async def cmd_tickets(args):
    """List approval tickets."""
    repo = ApprovalRepository()

    # Expire old tickets first
    repo.expire_tickets()

    # Query
    status = TicketStatus(args.status) if args.status else None
    tickets = repo.list_tickets(status=status, job_id=args.job_id)

    # Format output
    result = {
        "tickets": [
            {
                "id": t.id,
                "job_id": t.job_id,
                "tool_name": t.tool_name,
                "status": t.status.value,
                "risk_level": t.risk_level,
                "args_preview": t.args_preview,
                "requested_at": t.requested_at.isoformat(),
                "expires_at": t.expires_at.isoformat() if t.expires_at else None,
            }
            for t in tickets
        ],
        "count": len(tickets),
        "stats": repo.get_stats(),
    }
    print(json.dumps(result, indent=2, default=str))


async def cmd_approve(args):
    """Approve an approval ticket."""
    repo = ApprovalRepository()
    job_repo = _make_repository()
    service = _make_run_service(job_repo, non_interactive=True)
    ticket = repo.get_ticket(args.ticket_id)

    if not ticket:
        sys.stderr.write(json.dumps({"error": f"Ticket {args.ticket_id} not found",
                                     "code": "E3001"}) + "\n")
        sys.exit(1)

    previous_status = ticket.status.value

    try:
        ticket = repo.approve_ticket(args.ticket_id, reason=args.reason or "")
        await service.resume_after_approval(ticket.job_id, ticket.id)
    except ValueError as e:
        sys.stderr.write(json.dumps({"error": str(e), "code": "E3002"}) + "\n")
        sys.exit(1)

    print(json.dumps({
        "ticket_id": ticket.id,
        "status": ticket.status.value,
        "previous_status": previous_status,
        "decided_by": ticket.decided_by,
        "reason": ticket.reason,
        "decided_at": ticket.decided_at.isoformat() if ticket.decided_at else None,
        "message": "Ticket approved",
    }, indent=2, default=str))


async def cmd_reject(args):
    """Reject an approval ticket."""
    repo = ApprovalRepository()
    job_repo = _make_repository()
    service = _make_run_service(job_repo, non_interactive=True)
    ticket = repo.get_ticket(args.ticket_id)

    if not ticket:
        sys.stderr.write(json.dumps({"error": f"Ticket {args.ticket_id} not found",
                                     "code": "E3001"}) + "\n")
        sys.exit(1)

    previous_status = ticket.status.value

    try:
        ticket = repo.reject_ticket(args.ticket_id, reason=args.reason or "")
        try:
            await service.abort_after_rejection(ticket.job_id, ticket.id, reason=args.reason or "")
        except ValueError as abort_error:
            # Ticket rejection must remain valid even if job record is gone.
            if "not found" not in str(abort_error):
                raise
    except ValueError as e:
        sys.stderr.write(json.dumps({"error": str(e), "code": "E3003"}) + "\n")
        sys.exit(1)

    print(json.dumps({
        "ticket_id": ticket.id,
        "status": ticket.status.value,
        "previous_status": previous_status,
        "decided_by": ticket.decided_by,
        "reason": ticket.reason,
        "decided_at": ticket.decided_at.isoformat() if ticket.decided_at else None,
        "message": "Ticket rejected",
    }, indent=2, default=str))


# =============================================================================
# Memory CLI commands (M3.2)
# =============================================================================


def _make_memory_manager():
    """Create a MemoryManager from config."""
    from memory.manager import MemoryManager
    config = HarnessConfig.from_env()
    return MemoryManager(config.memory)


async def cmd_memory_search(args):
    """Search agent memory."""
    from core.models import MemoryScope, MemoryType
    manager = _make_memory_manager()
    scope = MemoryScope(args.scope) if args.scope else None
    memory_type = MemoryType(args.type) if args.type else None

    entries = manager.store.search(
        query=args.query,
        scope=scope,
        agent_type=args.agent,
        memory_type=memory_type,
        limit=args.limit,
    )
    result = [
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
    ]
    print(json.dumps(result, indent=2, default=str))


async def cmd_memory_list(args):
    """List agent memory entries."""
    from core.models import MemoryScope, MemoryType
    manager = _make_memory_manager()
    scope = MemoryScope(args.scope) if args.scope else None
    memory_type = MemoryType(args.type) if args.type else None

    entries = manager.store.list_entries(
        scope=scope,
        agent_type=args.agent,
        memory_type=memory_type,
    )
    result = [
        {
            "id": e.id,
            "agent_type": e.agent_type,
            "scope": e.scope.value,
            "type": e.memory_type.value,
            "content": e.content[:200],
            "keywords": e.keywords,
            "relevance_score": e.relevance_score,
            "access_count": e.access_count,
            "created_at": e.created_at.isoformat(),
        }
        for e in entries
    ]
    print(json.dumps(result, indent=2, default=str))


async def cmd_memory_stats(args):
    """Show memory system statistics."""
    manager = _make_memory_manager()
    stats = manager.get_stats()
    print(json.dumps(stats, indent=2, default=str))


async def cmd_memory_add(args):
    """Add a manual memory entry."""
    from core.models import MemoryScope, MemoryType
    manager = _make_memory_manager()

    entry = manager.store_learning(
        agent_type=args.agent,
        content=args.content,
        memory_type=MemoryType(args.type),
        scope=MemoryScope(args.scope),
        keywords=args.keywords if args.keywords else None,
    )
    print(json.dumps({
        "id": entry.id,
        "agent_type": entry.agent_type,
        "scope": entry.scope.value,
        "type": entry.memory_type.value,
        "message": "Memory entry added",
    }, indent=2, default=str))


async def cmd_memory_cleanup(args):
    """Run memory maintenance."""
    manager = _make_memory_manager()
    result = manager.run_maintenance()
    print(json.dumps(result, indent=2, default=str))


# =============================================================================
# Learning CLI commands (M3.3)
# =============================================================================


def _make_learning_scheduler():
    """Create a LearningScheduler from config."""
    from learning.analyzer import LearningAnalyzer
    from learning.optimizer import LearningOptimizer
    from learning.scheduler import LearningScheduler
    from control_plane.repository import JobRepository
    from monitoring.metrics import MetricsCollector

    config = HarnessConfig.from_env()
    memory_manager = _make_memory_manager()
    job_repo = JobRepository()
    metrics_collector = MetricsCollector(job_repo)

    analyzer = LearningAnalyzer(metrics_collector, memory_manager)
    optimizer = LearningOptimizer(memory_manager)
    return LearningScheduler(config.learning, analyzer, optimizer)


async def cmd_learning_analyze(args):
    """Trigger a learning analysis run."""
    scheduler = _make_learning_scheduler()
    result = scheduler.run_analysis()
    print(json.dumps(result, indent=2, default=str))


async def cmd_learning_insights(args):
    """List stored learning insights."""
    manager = _make_memory_manager()
    # Learning insights are stored as FACT/EXPERIENCE memories with learning keywords
    entries = manager.store.search(
        query="recommendation pattern anti_pattern",
        limit=args.limit,
    )
    result = [
        {
            "id": e.id,
            "agent_type": e.agent_type,
            "scope": e.scope.value,
            "type": e.memory_type.value,
            "content": e.content,
            "keywords": e.keywords,
            "relevance_score": e.relevance_score,
            "created_at": e.created_at.isoformat(),
        }
        for e in entries
    ]
    print(json.dumps(result, indent=2, default=str))


async def cmd_learning_status(args):
    """Show learning system status."""
    scheduler = _make_learning_scheduler()
    status = scheduler.get_status()
    print(json.dumps(status, indent=2, default=str))


# =============================================================================
# Template CLI commands (M3.4)
# =============================================================================


async def cmd_templates(args):
    """List available DAG templates."""
    from templates.library import TemplateRegistry
    registry = TemplateRegistry()
    templates = registry.list_templates()

    if args.name:
        # Show details of a specific template
        tpl = registry.get_template(args.name)
        if tpl is None:
            _write_error("E_TEMPLATE_NOT_FOUND", f"Template not found: {args.name}")
            return
        result = {
            "name": tpl.name,
            "description": tpl.description,
            "version": tpl.version,
            "category": tpl.category,
            "variables": tpl.variables,
            "nodes": tpl.nodes,
            "edges": tpl.edges,
            "reasoning_template": tpl.reasoning_template,
        }
    else:
        # List all templates
        result = {
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
    print(json.dumps(result, indent=2, default=str))


# =============================================================================
# Impact Analysis CLI commands (M3.5) — deferred until analysis/ module lands
# =============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Harness - Intelligent Multi-Agent Orchestration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate execution plan
  python main.py plan "Build a REST API for user authentication"

  # Execute a saved plan
  python main.py execute ./data/plans/plan_123.json

  # Plan + Execute in one step
  python main.py run "Add OAuth2 support" --project ./my-project

  # Run with live visualization
  python main.py run "Build API" --viz
  python main.py run "Build API" --visualize --no-browser

  # Launch standalone visualizer
  python main.py viz --port 8080
        """,
    )

    parser.add_argument(
        "--project",
        help="Path to project directory (loads .harness/agents.yaml if exists)",
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=3,
        help="Max parallel agent executions (default: 3)",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=50,
        help="Max iterations per agent loop (default: 50)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command")

    # plan command
    plan_parser = subparsers.add_parser("plan", help="Generate execution plan")
    plan_parser.add_argument("requirement", help="User requirement")
    plan_parser.add_argument(
        "--template",
        help="Use a named DAG template instead of LLM planning",
    )
    plan_parser.add_argument(
        "--var", action="append", default=[], metavar="KEY=VALUE",
        help="Template variable substitution (repeatable)",
    )
    plan_parser.set_defaults(func=cmd_plan)

    # execute command
    exec_parser = subparsers.add_parser("execute", help="Execute a saved plan")
    exec_parser.add_argument("plan_file", help="Path to plan JSON file")
    exec_parser.add_argument("--viz", action="store_true", help="Enable CLI + Web visualization")
    exec_parser.add_argument("--visualize", action="store_true", help="Enable visualization and auto-open browser")
    exec_parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    exec_parser.set_defaults(func=cmd_execute)

    # run command (plan + execute)
    run_parser = subparsers.add_parser("run", help="Plan and execute in one step")
    run_parser.add_argument("requirement", help="User requirement")
    run_parser.add_argument(
        "--template",
        help="Use a named DAG template instead of LLM planning",
    )
    run_parser.add_argument(
        "--var", action="append", default=[], metavar="KEY=VALUE",
        help="Template variable substitution (repeatable)",
    )
    run_parser.add_argument("--viz", action="store_true",
                            help="Enable CLI + Web visualization")
    run_parser.add_argument(
        "--visualize", action="store_true",
        help="Enable visualization and auto-open browser")
    run_parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    run_parser.set_defaults(func=cmd_run)

    # viz command (standalone server)
    viz_parser = subparsers.add_parser("viz", help="Launch visualizer dashboard")
    viz_parser.add_argument("--host", default="0.0.0.0", help="Server host (default: 0.0.0.0)")
    viz_parser.add_argument("--port", type=int, default=8080, help="Server port (default: 8080)")
    viz_parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    viz_parser.set_defaults(func=cmd_viz)

    # ------------------------------------------------------------------
    # Control-plane commands
    # ------------------------------------------------------------------

    # submit command
    submit_parser = subparsers.add_parser("submit", help="Submit a new job")
    submit_parser.add_argument("requirement", help="Task requirement")
    submit_parser.add_argument("--project", help="Project path")
    submit_parser.add_argument("--timeout", type=int, default=600, help="Timeout in seconds (default: 600)")
    submit_parser.add_argument("--max-attempts", type=int, default=3, help="Max retry attempts (default: 3)")
    submit_parser.set_defaults(func=cmd_submit)

    # status command
    status_parser = subparsers.add_parser("status", help="Get job status")
    status_parser.add_argument("job_id", help="Job ID")
    status_parser.set_defaults(func=cmd_status)

    # list command
    list_parser = subparsers.add_parser("list", help="List jobs")
    list_parser.add_argument("--status", choices=[s.value for s in JobStatus], help="Filter by status")
    list_parser.set_defaults(func=cmd_list_jobs)

    # cancel command
    cancel_parser = subparsers.add_parser("cancel", help="Cancel a job")
    cancel_parser.add_argument("job_id", help="Job ID")
    cancel_parser.set_defaults(func=cmd_cancel)

    # worker command
    worker_parser = subparsers.add_parser("worker", help="Start worker")
    worker_parser.add_argument("--concurrency", type=int, default=1, help="Number of concurrent jobs (default: 1)")
    worker_parser.add_argument("--poll-interval", type=int, default=5, help="Poll interval in seconds (default: 5)")
    worker_parser.add_argument("--non-interactive", action="store_true",
                               help="Run in non-interactive mode (no stdin approval)")
    worker_parser.set_defaults(func=cmd_worker)

    # recover command
    recover_parser = subparsers.add_parser("recover", help="Recover orphaned jobs after restart")
    recover_parser.set_defaults(func=cmd_recover)

    # console command
    console_parser = subparsers.add_parser("console", help="Launch Web Console")
    console_parser.add_argument("--host", default="0.0.0.0", help="Host to bind")
    console_parser.add_argument("--port", type=int, default=8080, help="Port to listen")
    console_parser.set_defaults(func=cmd_console)

    # ------------------------------------------------------------------
    # Approval ticket commands
    # ------------------------------------------------------------------

    # tickets command
    tickets_parser = subparsers.add_parser("tickets", help="List approval tickets")
    tickets_parser.add_argument("--status", choices=[s.value for s in TicketStatus],
                                help="Filter by status")
    tickets_parser.add_argument("--job", dest="job_id", help="Filter by job ID")
    tickets_parser.set_defaults(func=cmd_tickets)

    # approve command
    approve_parser = subparsers.add_parser("approve", help="Approve a ticket")
    approve_parser.add_argument("ticket_id", help="Ticket ID")
    approve_parser.add_argument("--reason", default="", help="Approval reason")
    approve_parser.set_defaults(func=cmd_approve)

    # reject command
    reject_parser = subparsers.add_parser("reject", help="Reject a ticket")
    reject_parser.add_argument("ticket_id", help="Ticket ID")
    reject_parser.add_argument("--reason", default="", help="Rejection reason")
    reject_parser.set_defaults(func=cmd_reject)

    # ------------------------------------------------------------------
    # Memory commands (M3.2)
    # ------------------------------------------------------------------

    # memory-search command
    mem_search_parser = subparsers.add_parser("memory-search", help="Search agent memory")
    mem_search_parser.add_argument("query", help="Search query")
    mem_search_parser.add_argument("--agent", help="Filter by agent type")
    mem_search_parser.add_argument("--scope", choices=["private", "session", "global"])
    mem_search_parser.add_argument("--type", choices=["fact", "experience", "preference", "context"])
    mem_search_parser.add_argument("--limit", type=int, default=10)
    mem_search_parser.set_defaults(func=cmd_memory_search)

    # memory-list command
    mem_list_parser = subparsers.add_parser("memory-list", help="List agent memory entries")
    mem_list_parser.add_argument("--agent", help="Filter by agent type")
    mem_list_parser.add_argument("--scope", choices=["private", "session", "global"])
    mem_list_parser.add_argument("--type", choices=["fact", "experience", "preference", "context"])
    mem_list_parser.set_defaults(func=cmd_memory_list)

    # memory-stats command
    mem_stats_parser = subparsers.add_parser("memory-stats", help="Memory system statistics")
    mem_stats_parser.set_defaults(func=cmd_memory_stats)

    # memory-add command
    mem_add_parser = subparsers.add_parser("memory-add", help="Add a manual memory entry")
    mem_add_parser.add_argument("content", help="Memory content")
    mem_add_parser.add_argument("--type", choices=["fact", "experience", "preference", "context"], default="fact")
    mem_add_parser.add_argument("--scope", choices=["private", "session", "global"], default="global")
    mem_add_parser.add_argument("--agent", default="shared")
    mem_add_parser.add_argument("--keywords", nargs="+", default=[])
    mem_add_parser.set_defaults(func=cmd_memory_add)

    # memory-cleanup command
    mem_cleanup_parser = subparsers.add_parser("memory-cleanup", help="Run memory maintenance")
    mem_cleanup_parser.set_defaults(func=cmd_memory_cleanup)

    # ------------------------------------------------------------------
    # Learning commands (M3.3)
    # ------------------------------------------------------------------

    # learning-analyze command
    learn_analyze_parser = subparsers.add_parser("learning-analyze", help="Trigger learning analysis")
    learn_analyze_parser.set_defaults(func=cmd_learning_analyze)

    # learning-insights command
    learn_insights_parser = subparsers.add_parser("learning-insights", help="List learning insights")
    learn_insights_parser.add_argument("--limit", type=int, default=20)
    learn_insights_parser.set_defaults(func=cmd_learning_insights)

    # learning-status command
    learn_status_parser = subparsers.add_parser("learning-status", help="Learning system status")
    learn_status_parser.set_defaults(func=cmd_learning_status)

    # ------------------------------------------------------------------
    # Template commands (M3.4)
    # ------------------------------------------------------------------

    # templates command
    templates_parser = subparsers.add_parser("templates", help="List DAG templates")
    templates_parser.add_argument("--name", help="Show details of a specific template")
    templates_parser.set_defaults(func=cmd_templates)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Ensure API key (skip for commands that don't need LLM)
    _NO_API_KEY_COMMANDS = {
        "viz", "status", "list", "cancel", "recover", "console",
        "tickets", "approve", "reject",
        "memory-search", "memory-list", "memory-stats",
        "memory-add", "memory-cleanup",
        "learning-analyze", "learning-insights", "learning-status",
        "templates",
    }
    # Template-based plan/run doesn't need an LLM key
    if args.command in ("plan", "run") and getattr(args, "template", None):
        _NO_API_KEY_COMMANDS.add(args.command)
    if args.command not in _NO_API_KEY_COMMANDS:
        from core.config import _CLAUDE_ENV
        has_key = (
            os.getenv("ANTHROPIC_API_KEY")
            or os.getenv("ANTHROPIC_AUTH_TOKEN")
            or os.getenv("OPENAI_API_KEY")
            or _CLAUDE_ENV.get("ANTHROPIC_AUTH_TOKEN")
        )
        if not has_key:
            sys.stderr.write(json.dumps({
                "error": "ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or OPENAI_API_KEY must be set",
                "code": "E_NO_API_KEY",
            }) + "\n")
            sys.exit(1)

    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
