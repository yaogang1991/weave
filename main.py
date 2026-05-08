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
            }
            for n in dag.nodes.values()
        ],
        "edges": [{"from": e.from_node, "to": e.to_node} for e in dag.edges],
    }


async def cmd_plan(args):
    """Generate an execution plan (DAG) from requirements."""
    config = HarnessConfig.from_env()
    store = SessionStore(config.event_store_path)
    registry = load_registry(args.project)

    orchestrator = IntelligentOrchestrator(
        llm_config=config.llm,
        session_store=store,
        agent_registry=registry,
    )

    print(f"Planning: {args.requirement}")
    print(f"Available agents: {[a.id for a in registry.list_agents()]}")

    # Generate DAG
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
            ))
        for edge_def in plan_data.get("edges", []):
            dag.add_edge(edge_def["from"], edge_def["to"])

    # Create guardrails (default: accept_edits)
    policy = GuardrailPolicy(
        mode=PermissionMode.ACCEPT_EDITS,
        auto_approve_read=True,
        max_iterations=args.max_iterations,
    )
    guardrails = Guardrails(policy, tool_registry)

    # Create agent pool with guardrails
    pool = AgentPool(
        llm_config=config.llm,
        session_store=store,
        agent_registry=registry,
        tool_registry=tool_registry,
        guardrails=guardrails,
        max_iterations=args.max_iterations,
    )

    # Create orchestrator for failure handling
    orchestrator = IntelligentOrchestrator(config.llm, store, registry)

    # Create DAG engine
    engine = DAGExecutionEngine(
        agent_executor=pool.get_executor(session_id),
        failure_handler=orchestrator.adapt_to_failure,
        max_parallel=args.max_parallel,
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
    run_parser.add_argument("--viz", action="store_true", help="Enable CLI + Web visualization")
    run_parser.add_argument("--visualize", action="store_true", help="Enable visualization and auto-open browser")
    run_parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    run_parser.set_defaults(func=cmd_run)

    # viz command (standalone server)
    viz_parser = subparsers.add_parser("viz", help="Launch visualizer dashboard")
    viz_parser.add_argument("--host", default="0.0.0.0", help="Server host (default: 0.0.0.0)")
    viz_parser.add_argument("--port", type=int, default=8080, help="Server port (default: 8080)")
    viz_parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    viz_parser.set_defaults(func=cmd_viz)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Ensure API key (skip for viz command which doesn't need LLM)
    if args.command != "viz":
        if not os.getenv("ANTHROPIC_API_KEY") and not os.getenv("OPENAI_API_KEY"):
            print("Error: ANTHROPIC_API_KEY or OPENAI_API_KEY must be set")
            sys.exit(1)

    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
