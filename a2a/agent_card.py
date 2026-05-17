"""Weave's A2A Agent Card definition.

Builds the default Agent Card that describes Weave's orchestration
capabilities — DAG generation, multi-agent coordination, planning,
code generation, and evaluation.

The card is served at ``/.well-known/agent-card.json`` when the
A2A server mode is enabled.
"""

from __future__ import annotations

import os

from a2a.models import (
    A2ACapabilities,
    A2ACard,
    A2AInterface,
    A2AProvider,
    A2ASkill,
)


# Weave's built-in A2A skills
_WEAVE_SKILLS: list[A2ASkill] = [
    A2ASkill(
        id="plan",
        name="Plan Task",
        description=(
            "Analyze a software requirement and generate an execution "
            "plan (DAG) with agent assignments and dependencies."
        ),
        tags=["planning", "dag", "orchestration"],
        examples=[
            "Build a REST API for todo items",
            "Add OAuth2 authentication to the existing API",
        ],
    ),
    A2ASkill(
        id="execute",
        name="Execute Plan",
        description=(
            "Execute a previously generated DAG plan with parallel "
            "agent coordination, watchdog monitoring, and quality gates."
        ),
        tags=["execution", "dag", "parallel"],
        examples=[
            "Execute the plan for building the REST API",
        ],
    ),
    A2ASkill(
        id="generate",
        name="Generate Code",
        description=(
            "Generate code for a specific task using the generator agent. "
            "Produces source files with appropriate structure and tests."
        ),
        tags=["code-generation", "implementation"],
        examples=[
            "Generate the user authentication module",
            "Write unit tests for the API endpoints",
        ],
    ),
    A2ASkill(
        id="evaluate",
        name="Evaluate Output",
        description=(
            "Run automated evaluation: tests, linting, and quality "
            "checks against generated code or artifacts."
        ),
        tags=["evaluation", "quality", "testing"],
        examples=[
            "Evaluate the generated API code",
            "Run tests and lint on the latest changes",
        ],
    ),
    A2ASkill(
        id="run",
        name="Run End-to-End",
        description=(
            "Plan and execute a task in one step: requirement analysis, "
            "DAG generation, parallel execution, and evaluation."
        ),
        tags=["full-pipeline", "orchestration"],
        examples=[
            "Build and test a REST API for user management",
            "Fix the authentication bug end-to-end",
        ],
    ),
]


def build_weave_agent_card(
    base_url: str | None = None,
    version: str | None = None,
) -> A2ACard:
    """Build Weave's default A2A Agent Card.

    Parameters
    ----------
    base_url:
        Base URL for the A2A endpoint. Defaults to
        ``WEAVE_A2A_BASE_URL`` env var or ``http://localhost:8080``.
    version:
        Weave version string. Defaults to ``WEAVE_VERSION`` env var
        or ``"0.1.0"``.
    """
    if base_url is None:
        base_url = os.environ.get(
            "WEAVE_A2A_BASE_URL", "http://localhost:8080"
        )
    if version is None:
        version = os.environ.get("WEAVE_VERSION", "0.1.0")

    return A2ACard(
        name="Weave",
        description=(
            "Self-hosted unattended software development system. "
            "Orchestrates multiple LLM agents (planner, generator, "
            "evaluator) to automate the full software development "
            "lifecycle via LLM-driven dynamic DAG generation and "
            "execution."
        ),
        version=version,
        provider=A2AProvider(
            name="Weave",
            url="https://github.com/yaogang1991/weave",
        ),
        supported_interfaces=[
            A2AInterface(
                url=f"{base_url}/a2a",
                protocol_binding="json-rpc",
                protocol_version="1.0",
            ),
        ],
        capabilities=A2ACapabilities(
            streaming=False,
            push_notifications=False,
            extended_agent_card=False,
        ),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain", "application/json"],
        skills=_WEAVE_SKILLS,
        metadata={
            "framework": "weave",
            "dag_execution": True,
            "multi_agent": True,
            "agents": ["planner", "generator", "evaluator"],
        },
    )
