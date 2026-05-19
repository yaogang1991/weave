"""Shared CLI argument definitions.

Eliminates duplicated add_argument() calls across subparsers (#497).
Each function adds a consistent set of arguments to a parser.
"""
from __future__ import annotations

import argparse


def add_project_arg(
    parser: argparse.ArgumentParser,
    *,
    default: str = argparse.SUPPRESS,
) -> None:
    """Add --project argument with consistent help text."""
    parser.add_argument(
        "--project",
        default=default,
        help="Path to project directory (overrides top-level --project)",
    )


def add_display_args(parser: argparse.ArgumentParser) -> None:
    """Add --viz, --visualize, --no-browser arguments."""
    parser.add_argument(
        "--viz", action="store_true",
        help="Enable CLI + Web visualization",
    )
    parser.add_argument(
        "--visualize", action="store_true",
        help="Enable visualization and auto-open browser",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="Don't auto-open browser",
    )


def add_template_args(parser: argparse.ArgumentParser) -> None:
    """Add --template and --var arguments."""
    parser.add_argument(
        "--template",
        help="Use a named DAG template instead of LLM planning",
    )
    parser.add_argument(
        "--var", action="append", default=[], metavar="KEY=VALUE",
        help="Template variable substitution (repeatable)",
    )


def add_execution_args(parser: argparse.ArgumentParser) -> None:
    """Add --max-parallel, --max-iterations, --non-interactive, --pass-threshold."""
    parser.add_argument(
        "--max-parallel", type=int, default=3,
        help="Max parallel agent executions (default: 3)",
    )
    parser.add_argument(
        "--max-iterations", type=int, default=50,
        help="Max iterations per agent loop (default: 50)",
    )
    parser.add_argument(
        "--non-interactive", action="store_true",
        help="Auto-approve all tool calls (no human approval needed)",
    )
    parser.add_argument(
        "--pass-threshold", type=float, default=None,
        help=(
            "Evaluation pass threshold >0-10 "
            "(default: 7.0 from config; lint-only failures downgrade to WARN)"
        ),
    )
    parser.add_argument(
        "--budget-tokens", type=int, default=None,
        help="Total token budget for this run (0 or unset = unlimited)",
    )
    parser.add_argument(
        "--backend", default=None,
        choices=["builtin", "claude_code"],
        help="Execution backend for agent nodes (default: builtin)",
    )



def add_self_modify_arg(parser: argparse.ArgumentParser) -> None:
    """Add --allow-self-modify argument."""
    parser.add_argument(
        "--allow-self-modify", action="store_true",
        help="Allow agents to modify the weave source tree (NOT recommended)",
    )


def add_requirement_arg(parser: argparse.ArgumentParser) -> None:
    """Add optional requirement positional arg + --file flag."""
    parser.add_argument(
        "--file", "-f", default=None,
        help="Read requirement from file (avoids shell escaping issues)",
    )
    parser.add_argument(
        "requirement", nargs="?", default=None,
        help="User requirement",
    )
