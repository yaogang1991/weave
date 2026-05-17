#!/usr/bin/env python3
"""
Weave CLI Entry Point: Intelligent Multi-Agent Orchestration.

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
from pathlib import Path

# Force UTF-8 mode on Windows (default is GBK/cp936).
os.environ.setdefault("PYTHONUTF8", "1")

# Ensure unbuffered output for real-time monitoring (#186).
os.environ.setdefault("PYTHONUNBUFFERED", "1")

# Ensure UTF-8 encoding on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

sys.path.insert(0, str(Path(__file__).parent))

from control_plane.models import JobStatus  # noqa: E402
from control_plane.approval import TicketStatus  # noqa: E402

# Import CLI command handlers from domain modules
from cli.execution import cmd_plan, cmd_execute, cmd_run, cmd_viz  # noqa: E402
from cli.utils import (  # noqa: E402, F401 — backward-compat re-exports used by tests
    _resolve_project_path,
    _check_dirty_workspace,
    _check_stdlib_shadowing,
    _parse_template_vars,
)
from cli.jobs import (  # noqa: E402
    cmd_submit, cmd_status, cmd_list_jobs, cmd_cancel,
    cmd_worker, cmd_recover, cmd_console,
)
from control_plane.approval import (  # noqa: E402, F401 — backward-compat re-export
    ApprovalRepository as ApprovalRepository,
)
from cli.approval import cmd_tickets, cmd_approve, cmd_reject  # noqa: E402
from cli.memory import (  # noqa: E402
    cmd_memory_search, cmd_memory_list, cmd_memory_stats,
    cmd_memory_add, cmd_memory_cleanup,
)
from cli.learning import (  # noqa: E402
    cmd_learning_analyze, cmd_learning_insights, cmd_learning_status,
)
from cli.skills import cmd_skills, cmd_skill, cmd_templates  # noqa: E402
from cli.impact import cmd_impact_predict, cmd_impact_graph, cmd_impact_history  # noqa: E402


def main():
    parser = argparse.ArgumentParser(
        description="Weave - Autonomous Multi-Agent Orchestration",
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
        help="Path to project directory (loads .weave/agents.yaml if exists)",
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
    plan_parser.add_argument(
        "--project", default=argparse.SUPPRESS,
        help="Path to project directory (overrides top-level --project)",
    )
    plan_parser.add_argument(
        "--file", "-f", default=None,
        help="Read requirement from file (avoids shell escaping issues)",
    )
    plan_parser.add_argument("requirement", nargs="?", default=None, help="User requirement")
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
    exec_parser.add_argument(
        "--project", default=argparse.SUPPRESS,
        help="Path to project directory (overrides top-level --project)",
    )
    exec_parser.add_argument("plan_file", help="Path to plan JSON file")
    exec_parser.add_argument("--viz", action="store_true", help="Enable CLI + Web visualization")
    exec_parser.add_argument(
        "--visualize", action="store_true",
        help="Enable visualization and auto-open browser",
    )
    exec_parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    exec_parser.add_argument(
        "--allow-self-modify", action="store_true",
        help="Allow agents to modify the weave source tree (NOT recommended)",
    )
    exec_parser.add_argument(
        "--max-parallel", type=int, default=3,
        help="Max parallel agent executions (default: 3)",
    )
    exec_parser.add_argument(
        "--max-iterations", type=int, default=50,
        help="Max iterations per agent loop (default: 50)",
    )
    exec_parser.add_argument(
        "--non-interactive", action="store_true",
        help="Auto-approve all tool calls (no human approval needed)",
    )
    exec_parser.add_argument(
        "--pass-threshold", type=float, default=None,
        help=(
            "Evaluation pass threshold >0-10 "
            "(default: 7.0 from config; lint-only failures downgrade to WARN)"
        ),
    )
    exec_parser.set_defaults(func=cmd_execute)

    # run command (plan + execute)
    run_parser = subparsers.add_parser("run", help="Plan and execute in one step")
    run_parser.add_argument(
        "--project", default=argparse.SUPPRESS,
        help="Path to project directory (overrides top-level --project)",
    )
    run_parser.add_argument(
        "--file", "-f", default=None,
        help="Read requirement from file (avoids shell escaping issues)",
    )
    run_parser.add_argument("requirement", nargs="?", default=None, help="User requirement")
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
    run_parser.add_argument(
        "--allow-self-modify", action="store_true",
        help="Allow agents to modify the weave source tree (NOT recommended)",
    )
    run_parser.add_argument(
        "--max-parallel", type=int, default=3,
        help="Max parallel agent executions (default: 3)",
    )
    run_parser.add_argument(
        "--max-iterations", type=int, default=50,
        help="Max iterations per agent loop (default: 50)",
    )
    run_parser.add_argument(
        "--pass-threshold", type=float, default=None,
        help=(
            "Evaluation pass threshold >0-10 "
            "(default: 7.0 from config; lint-only failures downgrade to WARN)"
        ),
    )
    run_parser.add_argument(
        "--non-interactive", action="store_true",
        help="Auto-approve all tool calls (no human approval needed)",
    )
    run_parser.add_argument(
        "--timeout", type=int, default=None,
        help="Per-run wall-clock timeout in seconds (default: 1800 from config)",
    )
    run_parser.add_argument(
        "--cleanup-stdlib-shadowing", action="store_true",
        help="Quarantine (not delete) leftover stdlib-shadowing directories to "
             ".weave/quarantine/ instead of aborting",
    )
    run_parser.set_defaults(func=cmd_run)

    # viz command (standalone server)
    viz_parser = subparsers.add_parser("viz", help="Launch visualizer dashboard")
    viz_parser.add_argument("--host", default="127.0.0.1", help="Server host (default: 127.0.0.1)")
    viz_parser.add_argument("--port", type=int, default=8080, help="Server port (default: 8080)")
    viz_parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    viz_parser.set_defaults(func=cmd_viz)

    # ------------------------------------------------------------------
    # Control-plane commands
    # ------------------------------------------------------------------

    submit_parser = subparsers.add_parser("submit", help="Submit a new job")
    submit_parser.add_argument("requirement", help="Task requirement")
    submit_parser.add_argument("--project", help="Project path")
    submit_parser.add_argument(
        "--timeout", type=int, default=1800, help="Timeout in seconds (default: 1800)",
    )
    submit_parser.add_argument(
        "--max-attempts", type=int, default=3, help="Max retry attempts (default: 3)",
    )
    submit_parser.add_argument(
        "--allow-self-modify", action="store_true",
        help="Allow agents to modify the weave source tree (NOT recommended)",
    )
    submit_parser.set_defaults(func=cmd_submit)

    status_parser = subparsers.add_parser("status", help="Get job status")
    status_parser.add_argument("job_id", help="Job ID")
    status_parser.set_defaults(func=cmd_status)

    list_parser = subparsers.add_parser("list", help="List jobs")
    list_parser.add_argument(
        "--status", choices=[s.value for s in JobStatus], help="Filter by status",
    )
    list_parser.set_defaults(func=cmd_list_jobs)

    cancel_parser = subparsers.add_parser("cancel", help="Cancel a job")
    cancel_parser.add_argument("job_id", help="Job ID")
    cancel_parser.set_defaults(func=cmd_cancel)

    worker_parser = subparsers.add_parser("worker", help="Start worker")
    worker_parser.add_argument(
        "--concurrency", type=int, default=1, help="Number of concurrent jobs (default: 1)",
    )
    worker_parser.add_argument(
        "--poll-interval", type=int, default=5, help="Poll interval in seconds (default: 5)",
    )
    worker_parser.add_argument("--non-interactive", action="store_true",
                               help="Run in non-interactive mode (no stdin approval)")
    worker_parser.set_defaults(func=cmd_worker)

    recover_parser = subparsers.add_parser("recover", help="Recover orphaned jobs after restart")
    recover_parser.set_defaults(func=cmd_recover)

    console_parser = subparsers.add_parser("console", help="Launch Web Console")
    console_parser.add_argument("--host", default="127.0.0.1", help="Host to bind")
    console_parser.add_argument("--port", type=int, default=8080, help="Port to listen")
    console_parser.set_defaults(func=cmd_console)

    # ------------------------------------------------------------------
    # Approval ticket commands
    # ------------------------------------------------------------------

    tickets_parser = subparsers.add_parser("tickets", help="List approval tickets")
    tickets_parser.add_argument("--status", choices=[s.value for s in TicketStatus],
                                help="Filter by status")
    tickets_parser.add_argument("--job", dest="job_id", help="Filter by job ID")
    tickets_parser.set_defaults(func=cmd_tickets)

    approve_parser = subparsers.add_parser("approve", help="Approve a ticket")
    approve_parser.add_argument("ticket_id", help="Ticket ID")
    approve_parser.add_argument("--reason", default="", help="Approval reason")
    approve_parser.set_defaults(func=cmd_approve)

    reject_parser = subparsers.add_parser("reject", help="Reject a ticket")
    reject_parser.add_argument("ticket_id", help="Ticket ID")
    reject_parser.add_argument("--reason", default="", help="Rejection reason")
    reject_parser.set_defaults(func=cmd_reject)

    # ------------------------------------------------------------------
    # Memory commands (M3.2)
    # ------------------------------------------------------------------

    mem_search_parser = subparsers.add_parser("memory-search", help="Search agent memory")
    mem_search_parser.add_argument("query", help="Search query")
    mem_search_parser.add_argument("--agent", help="Filter by agent type")
    mem_search_parser.add_argument("--scope", choices=["private", "session", "global"])
    mem_search_parser.add_argument(
        "--type", choices=["fact", "experience", "preference", "context"],
    )
    mem_search_parser.add_argument("--limit", type=int, default=10)
    mem_search_parser.set_defaults(func=cmd_memory_search)

    mem_list_parser = subparsers.add_parser("memory-list", help="List agent memory entries")
    mem_list_parser.add_argument("--agent", help="Filter by agent type")
    mem_list_parser.add_argument("--scope", choices=["private", "session", "global"])
    mem_list_parser.add_argument("--type", choices=["fact", "experience", "preference", "context"])
    mem_list_parser.set_defaults(func=cmd_memory_list)

    mem_stats_parser = subparsers.add_parser("memory-stats", help="Memory system statistics")
    mem_stats_parser.set_defaults(func=cmd_memory_stats)

    mem_add_parser = subparsers.add_parser("memory-add", help="Add a manual memory entry")
    mem_add_parser.add_argument("content", help="Memory content")
    mem_add_parser.add_argument(
        "--type", choices=["fact", "experience", "preference", "context"], default="fact",
    )
    mem_add_parser.add_argument(
        "--scope", choices=["private", "session", "global"], default="global",
    )
    mem_add_parser.add_argument("--agent", default="shared")
    mem_add_parser.add_argument("--keywords", nargs="+", default=[])
    mem_add_parser.set_defaults(func=cmd_memory_add)

    mem_cleanup_parser = subparsers.add_parser("memory-cleanup", help="Run memory maintenance")
    mem_cleanup_parser.set_defaults(func=cmd_memory_cleanup)

    # ------------------------------------------------------------------
    # Learning commands (M3.3)
    # ------------------------------------------------------------------

    learn_analyze_parser = subparsers.add_parser(
        "learning-analyze", help="Trigger learning analysis",
    )
    learn_analyze_parser.set_defaults(func=cmd_learning_analyze)

    learn_insights_parser = subparsers.add_parser(
        "learning-insights", help="List learning insights",
    )
    learn_insights_parser.add_argument("--limit", type=int, default=20)
    learn_insights_parser.set_defaults(func=cmd_learning_insights)

    learn_status_parser = subparsers.add_parser("learning-status", help="Learning system status")
    learn_status_parser.set_defaults(func=cmd_learning_status)

    # ------------------------------------------------------------------
    # Template commands (M3.4)
    # ------------------------------------------------------------------

    templates_parser = subparsers.add_parser("templates", help="List DAG templates")
    templates_parser.add_argument("--name", help="Show details of a specific template")
    templates_parser.set_defaults(func=cmd_templates)

    # Skills commands (M3.6)
    skills_parser = subparsers.add_parser("skills", help="List available skills")
    skills_parser.add_argument("--agent", help="Filter by agent type")
    skills_parser.add_argument("--project", default=".", help="Project path")
    skills_parser.set_defaults(func=cmd_skills)

    skill_parser = subparsers.add_parser("skill", help="Invoke a skill")
    skill_parser.add_argument("name", help="Skill name")
    skill_parser.add_argument(
        "--var", action="append", default=[], metavar="KEY=VALUE",
        help="Skill variable (repeatable)",
    )
    skill_parser.add_argument("--project", default=".", help="Project path")
    skill_parser.add_argument("--max-parallel", type=int, default=3)
    skill_parser.add_argument("--max-iterations", type=int, default=50)
    skill_parser.set_defaults(func=cmd_skill)

    # impact-predict command
    impact_predict_parser = subparsers.add_parser(
        "impact-predict", help="Predict impact of a change"
    )
    impact_predict_parser.add_argument("requirement", help="Change requirement text")
    impact_predict_parser.add_argument("--project", default=".", help="Project path")
    impact_predict_parser.set_defaults(func=cmd_impact_predict)

    # impact-graph command
    impact_graph_parser = subparsers.add_parser(
        "impact-graph", help="Show file dependency graph"
    )
    impact_graph_parser.add_argument("--project", default=".", help="Project path")
    impact_graph_parser.set_defaults(func=cmd_impact_graph)

    # impact-history command
    impact_history_parser = subparsers.add_parser(
        "impact-history", help="Show impact analysis history"
    )
    impact_history_parser.set_defaults(func=cmd_impact_history)

    args = parser.parse_args()

    # Validate --pass-threshold range (0, 10] early for clear error messages.
    _threshold = getattr(args, "pass_threshold", None)
    if _threshold is not None:
        if _threshold <= 0 or _threshold > 10:
            parser.error(
                f"--pass-threshold must be in range (0, 10], got {_threshold}"
            )

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
        "impact-predict", "impact-graph", "impact-history",
        "skills",
    }
    # Template-based plan doesn't need an LLM key (run still executes agents)
    if args.command == "plan" and getattr(args, "template", None):
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
