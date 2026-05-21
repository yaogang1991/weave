"""CLI execution commands — plan, execute, run, viz."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from core.config import WeaveConfig, _get_non_interactive_env
from core.models import DAG, DAGNode, EventType
from core.exceptions import PendingApprovalError
from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
from agent.agent_pool import AgentPool
from session.store import SessionStore
from tools.registry import ToolRegistry
from guardrails.policy import Guardrails, GuardrailPolicy, PermissionMode
from control_plane.approval import ApprovalRepository

from cli.utils import (
    _resolve_project_path,
    _check_dirty_workspace,
    _check_stdlib_shadowing,
    load_registry,
    _serialize_dag,
    _parse_template_vars,
)
from guardrails.injection import detect_injection  # noqa: E402 (#511)


async def cmd_plan(args):
    """Generate an execution plan (DAG) from requirements."""
    if getattr(args, "file", None):
        file_path = args.file
        if file_path == "-":
            args.requirement = sys.stdin.read().strip()
        else:
            args.requirement = Path(file_path).read_text(encoding="utf-8").strip()
    elif not args.requirement:
        print("Error: provide a requirement argument or use --file <path>", file=sys.stderr)
        sys.exit(1)

    config = WeaveConfig.from_env()
    store = SessionStore(config.event_store_path)
    registry = load_registry(args.project)

    # M3.6: Load skill registry for plan-influencing skills.
    skill_registry = None
    skills_dir = Path(args.project) / ".weave" / "skills" if args.project else None
    if skills_dir and skills_dir.is_dir():
        from skills.registry import SkillRegistry
        skill_registry = SkillRegistry(skills_dir=skills_dir)

    # Use template if specified (no LLM needed)
    if args.template:
        variables = _parse_template_vars(args.var)
        if args.requirement and "requirement" not in variables:
            variables["requirement"] = args.requirement
        print(f"Using template: {args.template} (vars: {variables})")
        from templates.library import TemplateRegistry
        tpl_registry = TemplateRegistry()
        dag = tpl_registry.instantiate(args.template, variables)
    else:
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
            skill_registry=skill_registry,
        )
        _inject_token_estimator(orchestrator, config.llm)

        print(f"Planning: {args.requirement}")
        print(f"Available agents: {[a.id for a in registry.list_agents()]}")

        # Input-layer injection detection (#511)
        injection_result = detect_injection(args.requirement)
        if injection_result.detected:
            print(
                f"\nWARNING: Potential prompt injection detected "
                f"(risk: {injection_result.risk_level}). "
                f"{injection_result.details}",
                file=sys.stderr,
            )
            if injection_result.risk_level == "high":
                print(
                    "High-risk injection patterns detected. "
                    "Proceeding with caution.",
                    file=sys.stderr,
                )

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

    with open(plan_file, "w", encoding="utf-8") as f:
        json.dump(plan_data, f, indent=2, default=str)

    print(f"\nPlan saved: {plan_file}")
    print(f"\nReasoning: {dag.reasoning}")
    print("\nExecution levels:")
    for i, level in enumerate(dag.topological_levels()):
        print(f"  Level {i}: {' → '.join(level)}")

    return dag


async def cmd_execute(args, dag: DAG | None = None):
    """Execute a saved plan (DAG). Accepts DAG directly to avoid re-serialization."""
    project = _resolve_project_path(
        args.project,
        allow_self_modify=getattr(args, "allow_self_modify", False),
    )
    args.project = project

    config = WeaveConfig.from_env()
    store = SessionStore(config.event_store_path)
    registry = load_registry(args.project)
    tool_registry = ToolRegistry(base_cwd=args.project) if args.project else ToolRegistry()

    # Create session
    session_id = str(uuid.uuid4())
    store.create_session(session_id, "weave_run")

    # Load DAG from file if not provided directly
    if dag is None:
        dag = _load_dag_from_file(args.plan_file)

    # Store DAG in session for visualizer
    store.emit_event(
        session_id,
        EventType.SESSION_DAG,
        _serialize_dag(dag),
    )

    guardrails = _build_guardrails(args, tool_registry)
    mcp_client = await _init_mcp_tools(config, tool_registry, guardrails)

    runtime = _build_runtime(
        args, config, store, registry, tool_registry, guardrails, session_id,
    )

    viz = _setup_visualization(args, runtime["engine"], dag, session_id)

    _attach_event_logger(runtime["engine"], dag, store, session_id)

    print(f"Executing DAG with {len(dag.nodes)} nodes...")
    print(f"Levels: {dag.topological_levels()}")
    print()
    sys.stdout.flush()

    # Execute
    result_dag = await _execute_with_error_handling(
        runtime["engine"], dag, store, session_id,
    )
    if result_dag is None:
        return None

    # Summary + cleanup
    await _finalize_execution(
        runtime["engine"], result_dag, store, session_id,
        viz, mcp_client,
    )

    return result_dag


def _load_dag_from_file(plan_file: str) -> DAG:
    """Load a DAG from a plan JSON file."""
    with open(plan_file, "r", encoding="utf-8") as f:
        plan_data = json.load(f)

    dag = DAG(reasoning=plan_data.get("reasoning", ""))
    for node_def in plan_data["nodes"]:
        dag.add_node(DAGNode(
            id=node_def["id"],
            agent_type=node_def["agent_type"],
            task_description=node_def["task"],
            success_criteria=node_def.get("success_criteria", []),
            estimated_tokens=node_def.get("estimated_tokens", 0),
            token_budget=node_def.get("token_budget", 8192),
            actual_tokens=node_def.get("actual_tokens", 0),
        ))
    for edge_def in plan_data.get("edges", []):
        dag.add_edge(edge_def["from"], edge_def["to"])
    return dag


def _build_guardrails(args, tool_registry: ToolRegistry) -> Guardrails:
    """Create guardrails based on CLI args."""
    non_interactive = (
        getattr(args, "non_interactive", False)
        or _get_non_interactive_env().lower() in ("true", "1", "yes")
    )
    if non_interactive:
        policy = GuardrailPolicy(
            mode=PermissionMode.DONT_ASK,
            auto_approve_read=True,
            allowed_tools=["read", "write", "edit", "bash", "glob", "grep", "git"],
            max_iterations=args.max_iterations,
        )
    else:
        policy = GuardrailPolicy(
            mode=PermissionMode.ACCEPT_EDITS,
            auto_approve_read=True,
            max_iterations=args.max_iterations,
        )
    project_path = getattr(args, "project", None)
    return Guardrails(
        policy, tool_registry,
        project_dir=project_path,
        interactive=not non_interactive,
    )


async def _init_mcp_tools(config, tool_registry, guardrails):
    """Connect to MCP servers and register their tools."""
    mcp_client = None
    if config.mcp.servers:
        try:
            from mcp.client import MCPClient
            from core.models import RiskLevel
            mcp_client = MCPClient(config.mcp)
            connected = await mcp_client.connect_all()
            if connected > 0:
                discovered = await mcp_client.discover_all_tools()
                if discovered:
                    tool_registry.register_mcp_tools(mcp_client, discovered)
                    guardrails.register_mcp_risk_map(
                        discovered, default_risk=RiskLevel.MEDIUM
                    )
                    print(f"Registered {len(discovered)} MCP tools from "
                          f"{connected} server(s)")
        except Exception as e:
            print(f"MCP initialization warning: {e}")
    return mcp_client


def _build_runtime(
    args, config, store, registry, tool_registry, guardrails, session_id,
) -> dict:
    """Build the runtime object graph: pool, orchestrator, evaluator, engine."""
    # Skill registry
    skill_registry = None
    skills_dir = Path(args.project) / ".weave" / "skills" if args.project else None
    if skills_dir and skills_dir.is_dir():
        from skills.registry import SkillRegistry
        skill_registry = SkillRegistry(skills_dir=skills_dir)

    # LLM router
    llm_router = None
    if config.model_routing.routing:
        from core.llm_router import LLMRouter
        llm_router = LLMRouter(config.model_routing, config.llm)

    # Memory manager
    memory_manager = None
    if config.memory.enabled:
        try:
            from memory.manager import MemoryManager
            memory_manager = MemoryManager(config.memory, session_store=store)
        except Exception:
            pass

    # Learning optimizer
    learning_optimizer = None
    if memory_manager:
        try:
            from learning.optimizer import LearningOptimizer
            learning_optimizer = LearningOptimizer(memory_manager)
        except Exception:
            pass

    # Agent pool
    approval_repo = ApprovalRepository()
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
        job_id=f"cli_{session_id}",
        approval_repo=approval_repo,
    )

    # Orchestrator
    orchestrator = IntelligentOrchestrator(
        config.llm, store, registry,
        llm_router=llm_router,
        learning_optimizer=learning_optimizer,
        skill_registry=skill_registry,
    )
    _inject_token_estimator(orchestrator, config.llm)

    # Evaluator
    from evaluator.engine import EvaluatorEngine
    cli_threshold = getattr(args, "pass_threshold", None)
    pass_threshold = cli_threshold if cli_threshold is not None else config.pass_threshold
    evaluator = EvaluatorEngine(
        session_store=store,
        pass_threshold=pass_threshold,
        auto_format_before_eval=config.auto_format_before_eval,
    )

    # DAG engine
    project_work_dir = str(Path(args.project).resolve())
    wd_cfg = config.watchdog
    from core.dag_engine import DAGExecutionEngine
    from agent.backends.registry import BackendRegistry
    backend_registry = BackendRegistry(pool=pool, session_id=session_id)

    # M4.1: Register ClaudeCodeBackend if enabled or requested
    backend_name = getattr(args, "backend", None)
    if backend_name == "claude_code" or config.claude_code.enabled:
        from agent.backends.claude_code import (
            ClaudeCodeBackend,
            ClaudeCodeConfig as RuntimeConfig,
        )
        cc_config = RuntimeConfig.from_core_config(config.claude_code)
        cc_backend = ClaudeCodeBackend(config=cc_config)
        backend_registry.register("claude_code", cc_backend)


    # M4.2: Budget manager from CLI args
    budget_manager = None
    budget_tokens = getattr(args, "budget_tokens", None)
    if budget_tokens and budget_tokens > 0:
        from core.budget_manager import BudgetManager
        from core.config import BudgetConfig
        budget_manager = BudgetManager(BudgetConfig(total_tokens=budget_tokens))

    engine = DAGExecutionEngine(
        agent_executor=pool.get_executor(session_id),
        failure_handler=orchestrator.adapt_to_failure,
        replan_handler=orchestrator.replan,
        max_parallel=args.max_parallel,
        evaluator=evaluator,
        artifact_path=config.artifact_path,
        work_dir=project_work_dir,
        memory_manager=memory_manager,
        session_id=session_id,
        heartbeat_interval_sec=wd_cfg.heartbeat_interval_sec,
        heartbeat_miss_threshold=wd_cfg.heartbeat_miss_threshold,
        enable_watchdog=wd_cfg.enabled,
        watchdog_overrides={
            agent_type: (ov.heartbeat_interval_sec, ov.heartbeat_miss_threshold)
            for agent_type, ov in wd_cfg.agent_overrides.items()
            if ov.heartbeat_interval_sec is not None
            and ov.heartbeat_miss_threshold is not None
        },
        alert_thresholds={
            agent_type: wd_cfg.alert_threshold_for(agent_type)
            for agent_type in wd_cfg.agent_overrides
        },
        node_timeout_config=config.node_timeout,
        backend_registry=backend_registry,
        budget_manager=budget_manager,
        node_timeout_config=config.node_timeout,
    )

    return {
        "engine": engine,
        "pool": pool,
        "orchestrator": orchestrator,
        "evaluator": evaluator,
    }


def _setup_visualization(args, engine, dag, session_id):
    """Set up CLI and web visualization if requested."""
    bridge = None
    server_task = None
    cli_renderer = None

    if args.viz or args.visualize:
        from visualizer.cli_renderer import CLIDAGRenderer

        cli_renderer = CLIDAGRenderer()
        engine.on_event(cli_renderer.handle_event)
        cli_renderer.render_dag(dag)

    return {
        "bridge": bridge,
        "server_task": server_task,
        "cli_renderer": cli_renderer,
    }


def _attach_event_logger(engine, dag, store, session_id):
    """Attach default console progress logger and session event bridge."""

    async def on_event(event):
        print(
            f"  [{event.event_type.upper()}] {event.node_id}: {event.details}",
            flush=True,
        )
        if event.event_type == "started":
            node = dag.nodes.get(event.node_id)
            store.emit_event(session_id, EventType.WORKFLOW_STAGE_START, {
                "node_id": event.node_id,
                "agent_type": node.agent_type if node else "",
                "task": node.task_description[:200] if node else "",
            })
        elif event.event_type == "completed":
            store.emit_event(session_id, EventType.WORKFLOW_STAGE_END, {
                "node_id": event.node_id,
            })
        elif event.event_type == "failed":
            store.emit_event(session_id, EventType.WORKFLOW_STAGE_ERROR, {
                "node_id": event.node_id,
                "error": event.details.get("reason", "failed"),
            })

    engine.on_event(on_event)


async def _execute_with_error_handling(engine, dag, store, session_id):
    """Execute DAG with error handling for cancellation, approval, and exceptions."""
    try:
        return await engine.execute(dag)
    except asyncio.CancelledError:
        store.emit_event(session_id, EventType.SESSION_ERROR, {
            "error": "Execution cancelled (timeout or external signal)",
        })
        print("Execution cancelled.", file=sys.stderr)
        sys.stderr.flush()
        return None
    except PendingApprovalError as exc:
        store.emit_event(session_id, EventType.SESSION_ERROR, {
            "error": f"Approval required: {exc.ticket_id}",
        })
        if exc.ticket_id:
            print("\nAgent requested approval for a high-risk operation.", flush=True)
            print(f"  Ticket ID: {exc.ticket_id}", flush=True)
            print(f"  Approve:   python main.py approve {exc.ticket_id}", flush=True)
            print(f"  Reject:    python main.py reject {exc.ticket_id}", flush=True)
            print("Then rerun to continue, or use worker mode for automatic poll.", flush=True)
        else:
            print("\nAgent requested approval but no ticket was created.", flush=True)
            print("This may be a configuration issue.", flush=True)
        print(
            "For local auto-approval: set WEAVE_NON_INTERACTIVE=true "
            "or use --non-interactive.",
            flush=True,
        )
        return None
    except Exception as exc:
        store.emit_event(session_id, EventType.SESSION_ERROR, {
            "error": f"{type(exc).__name__}: {exc}",
        })
        raise


async def _finalize_execution(engine, result_dag, store, session_id, viz, mcp_client):
    """Print summary, emit session end, clean up resources."""
    summary = engine.get_execution_summary(result_dag)
    print("\nExecution complete:")
    print(f"  Total: {summary['total_nodes']}")
    print(f"  Success: {summary['success']}")
    print(f"  Failed: {summary['failed']}")
    print(f"  Skipped: {summary['skipped']}")
    print(f"  Session ID: {session_id}")

    store.emit_event(
        session_id,
        EventType.SESSION_END,
        {"summary": summary},
    )

    bridge = viz.get("bridge")
    cli_renderer = viz.get("cli_renderer")
    server_task = viz.get("server_task")

    if bridge:
        await bridge.broadcast_session_end(session_id, summary)

    if cli_renderer:
        cli_renderer.render_summary(result_dag)

    if server_task and not server_task.done():
        await asyncio.sleep(2)
        server_task.cancel()
        try:
            await server_task
        except asyncio.CancelledError:
            pass

    if mcp_client:
        await mcp_client.disconnect_all()


async def cmd_run(args):
    """Plan + Execute in one command."""
    if getattr(args, "file", None):
        file_path = args.file
        if file_path == "-":
            requirement = sys.stdin.read().strip()
        else:
            requirement = Path(file_path).read_text(encoding="utf-8").strip()
        args.requirement = requirement
    elif not args.requirement:
        print("Error: provide a requirement argument or use --file <path>", file=sys.stderr)
        sys.exit(1)

    project = _resolve_project_path(
        args.project,
        allow_self_modify=getattr(args, "allow_self_modify", False),
    )
    args.project = project

    _check_dirty_workspace(
        args.project,
        non_interactive=getattr(args, "non_interactive", False),
    )

    _check_stdlib_shadowing(
        args.project,
        cleanup=getattr(args, "cleanup_stdlib_shadowing", False),
    )

    dag = await cmd_plan(args)

    exec_args = argparse.Namespace(
        plan_file="",
        project=args.project,
        max_parallel=args.max_parallel,
        max_iterations=args.max_iterations,
        pass_threshold=getattr(args, "pass_threshold", None),
        non_interactive=getattr(args, "non_interactive", False),
        allow_self_modify=getattr(args, "allow_self_modify", False),
        viz=args.viz,
        visualize=args.visualize,
        no_browser=args.no_browser,
        template=getattr(args, "template", None),
        var=getattr(args, "var", []),
        budget_tokens=getattr(args, "budget_tokens", None),
        backend=getattr(args, "backend", None),
    )
    return await cmd_execute(exec_args, dag=dag)


async def cmd_viz(args):
    """Launch the visualizer web server."""
    from visualizer.server import run_server

    host = args.host
    port = args.port

    print(f"🚀 Starting Weave Visualizer at http://{host}:{port}")
    print("Press Ctrl+C to stop")

    if not args.no_browser:
        await asyncio.sleep(1)
        webbrowser.open(f"http://{host}:{port}")

    await run_server(host=host, port=port)


async def cmd_serve(args):
    """Start MCP Server exposing Weave tools (#512)."""
    from mcp.server import MCPServer
    from cli.mcp_tools import register_weave_tools

    server = MCPServer(name="weave", version="0.1.0")
    register_weave_tools(server)

    print("Starting Weave MCP Server (stdio transport)", file=sys.stderr)
    print(
        f"Registered tools: {list(server._tools.keys())}",
        file=sys.stderr,
    )
    await server.run()


def _inject_token_estimator(orchestrator: IntelligentOrchestrator, llm_config) -> None:
    """Inject TokenEstimator into orchestrator for M4.6 token-aware planning (#671)."""
    try:
        from core.token_estimator import TokenEstimator
        from core.config import TokenEstimationConfig
        import anthropic as _anthropic

        cfg = TokenEstimationConfig()
        client = None
        if llm_config.api_key:
            try:
                client = _anthropic.AsyncAnthropic(api_key=llm_config.api_key)
            except Exception:
                pass
        orchestrator._token_estimator = TokenEstimator(
            config=cfg, client=client, model=llm_config.model,
        )
    except Exception:
        pass
