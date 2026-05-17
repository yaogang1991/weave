"""SWE-bench task instance → Weave DAG converter.

Converts a SWE-bench task instance into a DAG plan that Weave can
execute. The generated DAG follows a standard pattern:

1. **setup** — Clone repo at base_commit, apply test_patch
2. **analyze** — Read problem statement + repo context, identify files to change
3. **generate** — Produce the patch for the identified files
4. **validate** — Run the fail_to_pass tests to verify the patch

Each node maps to a Weave agent with appropriate task description.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from benchmarks.models import SWEBenchTaskInstance
from core.dag_models import DAG, DAGEdge, DAGNode

logger = logging.getLogger(__name__)


def task_to_dag(
    instance: SWEBenchTaskInstance,
    timeout: int = 600,
) -> DAG:
    """Convert a SWE-bench task instance into a Weave DAG plan.

    Parameters
    ----------
    instance:
        The SWE-bench task instance to convert.
    timeout:
        Per-node timeout in seconds.

    Returns
    -------
    DAG ready for execution by the DAGExecutionEngine.
    """
    run_id = uuid.uuid4().hex[:8]
    prefix = f"swebench_{run_id}"

    # Node 1: Setup — clone and prepare repo
    setup_id = f"{prefix}_setup"
    setup_desc = (
        f"Setup repository {instance.repo} at commit {instance.base_commit}.\n"
        f"Clone the repo, checkout the base commit, and apply the test patch.\n"
        f"Repo: {instance.repo}\n"
        f"Base commit: {instance.base_commit}\n"
        f"Test patch to apply:\n```\n{instance.test_patch}\n```"
    )
    setup_node = DAGNode(
        id=setup_id,
        agent_type="generator",
        task_description=setup_desc,
        max_retries=1,
    )

    # Node 2: Analyze — understand the problem and identify target files
    analyze_id = f"{prefix}_analyze"
    analyze_desc = (
        f"Analyze the following issue in {instance.repo} and identify "
        f"which files need to be modified to fix it.\n\n"
        f"## Problem Statement\n{instance.problem_statement}\n\n"
    )
    if instance.hints_text:
        analyze_desc += f"## Hints\n{instance.hints_text}\n\n"
    analyze_desc += (
        "Provide a list of files that need to be changed and a brief "
        "explanation of what changes are needed."
    )
    analyze_node = DAGNode(
        id=analyze_id,
        agent_type="planner",
        task_description=analyze_desc,
        max_retries=1,
    )

    # Node 3: Generate — produce the patch
    generate_id = f"{prefix}_generate"
    generate_desc = (
        f"Generate a patch to fix the issue in {instance.repo}.\n\n"
        "## Problem Statement\n" + instance.problem_statement + "\n\n"
    )
    if instance.hints_text:
        generate_desc += f"## Hints\n{instance.hints_text}\n\n"
    generate_desc += (
        "Based on the analysis, modify the necessary files to resolve "
        "the issue. Produce a clean git diff patch.\n\n"
        "Tests that must pass after your change:\n"
    )
    for test in instance.fail_to_pass[:20]:
        generate_desc += f"- {test}\n"
    generate_node = DAGNode(
        id=generate_id,
        agent_type="generator",
        task_description=generate_desc,
        max_retries=2,
    )

    # Node 4: Validate — run tests
    validate_id = f"{prefix}_validate"
    validate_desc = (
        f"Run the following tests to validate the patch for "
        f"{instance.instance_id}:\n\n"
    )
    for test in instance.fail_to_pass[:30]:
        validate_desc += f"- {test}\n"
    validate_desc += "\nReport which tests pass and which fail."
    validate_node = DAGNode(
        id=validate_id,
        agent_type="evaluator",
        task_description=validate_desc,
        max_retries=1,
    )

    # Build DAG with linear dependency chain
    nodes = {
        setup_id: setup_node,
        analyze_id: analyze_node,
        generate_id: generate_node,
        validate_id: validate_node,
    }
    edges = [
        DAGEdge(from_node=setup_id, to_node=analyze_id),
        DAGEdge(from_node=analyze_id, to_node=generate_id),
        DAGEdge(from_node=generate_id, to_node=validate_id),
    ]

    dag = DAG(
        nodes=nodes,
        edges=edges,
        reasoning=(
            f"SWE-bench task {instance.instance_id}: "
            f"setup → analyze → generate → validate pipeline "
            f"for {instance.repo}"
        ),
    )

    logger.debug(
        "Created DAG for SWE-bench instance %s with %d nodes",
        instance.instance_id, len(nodes),
    )
    return dag


def dag_to_task_context(
    instance: SWEBenchTaskInstance,
) -> dict[str, Any]:
    """Build a context dict for orchestrator-based execution.

    When using the full IntelligentOrchestrator instead of the
    fixed DAG, this provides the task context.
    """
    context: dict[str, Any] = {
        "source": "swebench",
        "instance_id": instance.instance_id,
        "repo": instance.repo,
        "base_commit": instance.base_commit,
        "problem_statement": instance.problem_statement,
    }
    if instance.hints_text:
        context["hints"] = instance.hints_text
    if instance.fail_to_pass:
        context["target_tests"] = instance.fail_to_pass
    return context
